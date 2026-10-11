from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pudge.review_episode_due import build_text_due_review_cards
from pudge.player import build_mpv_command
from pudge.review_providers import credential_account_key
from test_review_gate_episode_due import _FakeJiten
from test_skip_segments_playback import LUA, MOCK, TRACKER_LUA
from test_v0730_content_power_lifecycle import _gate
from study_page_support import CARD
from t71g03_support import MANGA, READER
from t71g10_support import ENTRY


@pytest.mark.parametrize("legacy_empty_grant", [False, True])
def test_empty_content_result_is_rechecked_when_words_become_due(tmp_path, monkeypatch, legacy_empty_grant):
    gate, _, _ = _gate(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "pudge.content_review.build_text_due_review_cards", lambda *args: calls.append(1) or {"cards": []}
    )
    empty = gate.begin("ln", 1, 0)
    assert empty["checked"] and empty["reason"] == "no_due_cards"
    assert empty["granted"] and empty["required"] == 0
    identity, _ = gate.source("ln", 1, 0, prepare=True)
    account = credential_account_key("jiten", "synthetic-key")
    assert not gate.store.snapshot(account, identity)["granted"]
    if legacy_empty_grant:
        gate.store.grant(account, identity, required=0)
        gate.api.manager.db.set_state(f"content_review_targets:v1:{account}:{identity.logical_id}", "[]")
    monkeypatch.setattr(
        "pudge.content_review.build_text_due_review_cards",
        lambda *args: {"cards": [{"wordId": 3, "readingIndex": 0}]},
    )
    ready = gate.begin("ln", 1, 0)
    assert ready["blocking"] and not ready["granted"]
    assert ready["cards"] == [{"wordId": 3, "readingIndex": 0}]
    gate.review(ready["token"], 3, 0, "good", "synthetic-attempt")
    assert gate.begin("ln", 1, 0)["reason"] == "already_reviewed"


@pytest.mark.parametrize("invalid", ["missing_parse", "partial_parse", "stale_states"])
def test_content_preparation_cannot_use_unchecked_or_stale_empty_results(invalid):
    service = _FakeJiten()
    parse = service.jiten_preparse
    states = service.jiten_live_states
    if invalid == "missing_parse":
        service.jiten_preparse = lambda *args, **kwargs: {}
    elif invalid == "partial_parse":
        service.jiten_preparse = lambda *args, **kwargs: {**parse(*args, **kwargs), "tokens": [[]]}
    else:
        service.jiten_live_states = lambda *args, **kwargs: [
            {**row, "stale": True} for row in states(*args, **kwargs)
        ]
    with pytest.raises(ValueError, match="complete content parse|states are stale"):
        build_text_due_review_cards(service, "猫が走る。\n犬も走る。", "synthetic")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_owned_anime_session_disables_idle_but_respects_explicit_keep_open(monkeypatch, platform):
    monkeypatch.setattr("pudge.player.sys.platform", platform)
    command = build_mpv_command(
        "mpv", Path("synthetic.mkv"), None, None, ["--idle=yes", "--keep-open=always"]
    )
    assert command.index("--idle=no") > command.index("--idle=yes")
    assert "--keep-open=always" in command
    assert "--keep-open=no" not in command


@pytest.mark.skipif(LUA is None, reason="no lua/luajit interpreter")
def test_mpv_close_quits_and_explain_only_uses_the_shortcut(tmp_path):
    scenario = tmp_path / "close-and-explain.lua"
    scenario.write_text(
        """
sim.set('osd-dimensions', {w=1920,h=1080})
sim.move(1800,40)
assert(not sim.overlay():find('Explain current subtitle'))
assert(not sim.bindings['pudge-explain-subtitle-button'])
assert(not sim.bindings['pudge_explain_click'])
sim.set('sub-text','架空の台詞')
assert(sim.press('Ctrl+Shift+t'))
assert(#sim.commands('pudge.mpv_assistant_request')==1)
local _, close = sim.binding_for_key('CLOSE_WIN')
assert(close.forced)
assert(sim.press('CLOSE_WIN'))
assert(sim.props['user-data/pudge/closing']==true)
assert(sim.press('Meta+w'))
assert(#sim.commands('quit')==2)
""",
        encoding="utf-8",
    )
    env = dict(os.environ, PUDGE_LLM_CONFIGURED="1", PUDGE_PLAYBACK_VIDEO="synthetic.mkv")
    subprocess.run(
        [LUA, str(MOCK), str(scenario), str(TRACKER_LUA)], check=True, capture_output=True, text=True, env=env
    )


# A content review before reading: book or volume 7 needs one review (Escape cancels the
# gate); every other book opens at once. before= code (after ENTRY / MANGA).
BLOCK_SEVEN = (
    CARD
    + r"""
__api.content_review_begin = async (kind, id) => Number(id) === 7
  ? {blocking: true, granted: false, required: 1, completed: 0, remaining: 1, token: 'tok-7', cards: [reviewCard(1, '猫', 'ねこ')]}
  : {granted: true, blocking: false};
"""
)
GATE_OPEN = "Boolean(document.getElementById('pudgeReviewGate')?.classList.contains('open'))"


@pytest.mark.parametrize("reading", [False, True])
def test_ln_entry_keeps_the_previous_screen_while_its_review_loads_and_after_it_is_cancelled(
    page, reading
) -> None:
    # Old bug: the reader opened (or switched books) underneath the review gate, and a
    # cancelled review left the new book half-entered.
    result = page.run(
        r"""
        const screen = () => ({reader: $('lnReaderShell').classList.contains('open'), title: __text('#lnReaderTitle'),
                               page: document.querySelector('.page.active')?.id, gate: GATE_OPEN});
        if (READING) { await __settle(openLightNovel(90)); await __tick(100); }
        const out = {before: screen()};
        window.HOLD.light_novel_open = true;
        const entry = openLightNovel(7, {pageOnOpen: 'lightnovels'}); await __tick(50);
        out.loading = screen();                       // the book's metadata is on its way
        window.HOLD.light_novel_open = false; window.release('light_novel_open'); await __tick(50);
        out.reviewing = screen();                     // the review gate asks first
        await __key('Escape'); await __settle(entry); await __tick(50);
        out.cancelled = screen();
        if (READING) { await __input('#lnChapterSelect', '2'); await __tick(100); }
        return out;
        """.replace("READING", "true" if reading else "false").replace("GATE_OPEN", GATE_OPEN),
        before=ENTRY + BLOCK_SEVEN,
    )
    out = result.value
    before = out["before"]
    assert before["reader"] is reading and before["page"] == "current" and before["gate"] is False
    assert out["loading"] == before
    assert out["reviewing"] == {**before, "gate": True}
    assert out["cancelled"] == before
    assert ["ln", 7, 0] in result.calls_of("content_review_begin")
    # Nothing of book 7 was loaded; an open reader goes on with its own book.
    assert not any(call[0] == 7 for call in result.calls_of("light_novel_chapter"))
    if reading:
        assert before["title"] == "Book 90" and [90, 2] in result.calls_of("light_novel_chapter")


def test_manga_entry_keeps_the_open_volume_while_its_review_loads_and_after_it_is_cancelled(page) -> None:
    result = page.run(
        READER
        + r"""
        window.MANGA.books.push({id: 7, title: 'Other 1', series_title: 'Other', volume: 1, page_count: 6, position: 0, read_pages: 0, ocr_complete: true});
        const screen = () => ({reader: reader()?.classList.contains('open'), title: __text('#mangaV2Title'),
                               label: __text('#mangaV2PageLabel'), gate: GATE_OPEN});
        await open(3);
        const out = {before: screen()};
        const entry = PudgeMangaReaderV2.openBook(7); await __tick(50);
        out.reviewing = screen();
        await __key('Escape'); await __settle(entry); await __tick(50);
        out.cancelled = screen();
        await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(200);
        out.turned = __text('#mangaV2PageLabel');
        return out;
        """.replace("GATE_OPEN", GATE_OPEN),
        before=MANGA + BLOCK_SEVEN,
    )
    out = result.value
    assert (
        out["before"]["reader"] is True
        and out["before"]["label"] == "1 / 6"
        and out["before"]["gate"] is False
    )
    assert out["reviewing"] == {**out["before"], "gate": True}
    assert out["cancelled"] == out["before"]
    assert result.calls_of("content_review_begin")[-1] == ["manga", 7, 0]
    # The reader goes on with volume 3: no page of volume 7 is ever fetched.
    assert out["turned"] == "2 / 6"
    assert {call[0] for call in result.calls_of("manga_page")} == {3}


def test_close_intent_ends_an_owned_player_that_ignores_ipc_quit(monkeypatch):
    import json
    import sys
    import time

    from pudge.playback_process import wait_playback_process

    commands = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            assert path == "synthetic.sock"

        def sendall(self, message):
            row = json.loads(message)
            commands.append(row["command"])

        def recv(self, size):
            return b'{"request_id":73,"error":"success","data":true}\n'

    monkeypatch.setattr("pudge.playback_process.socket.socket", lambda *args: Client())
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    started = time.monotonic()
    try:
        assert wait_playback_process(process, ipc_socket="synthetic.sock") != 0
        assert time.monotonic() - started < 5
        assert ["get_property", "user-data/pudge/closing"] in commands
        assert ["quit"] in commands
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


@pytest.mark.parametrize(
    "reply",
    [
        b'{"request_id":73,"error":"success","data":false}\n',
        b'{"request_id":73,"error":"property unavailable"}\n',
        b"[]\n",
        b"invalid\n",
    ],
)
def test_close_guard_ignores_missing_false_or_malformed_intent(monkeypatch, reply):
    from pudge.playback_process import _close_requested

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, message):
            pass

        def recv(self, size):
            nonlocal reply
            result, reply = reply, b""
            return result

    monkeypatch.setattr("pudge.playback_process.socket.socket", lambda *args: Client())
    assert not _close_requested("synthetic.sock")
    assert not _close_requested(None)
