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


@pytest.mark.parametrize("legacy_empty_grant", [False, True])
def test_empty_content_result_is_rechecked_when_words_become_due(tmp_path, monkeypatch, legacy_empty_grant):
    gate, _, _ = _gate(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("pudge.content_review.build_text_due_review_cards", lambda *args: calls.append(1) or {"cards": []})
    empty = gate.begin("ln", 1, 0)
    assert empty["checked"] and empty["reason"] == "no_due_cards"
    assert empty["granted"] and empty["required"] == 0
    identity, _ = gate.source("ln", 1, 0, prepare=True)
    account = credential_account_key("jiten", "synthetic-key")
    assert not gate.store.snapshot(account, identity)["granted"]
    if legacy_empty_grant:
        gate.store.grant(account, identity, required=0)
        gate.api.manager.db.set_state(f"content_review_targets:v1:{account}:{identity.logical_id}", "[]")
    monkeypatch.setattr("pudge.content_review.build_text_due_review_cards", lambda *args: {"cards": [{"wordId": 3, "readingIndex": 0}]})
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
        service.jiten_live_states = lambda *args, **kwargs: [{**row, "stale": True} for row in states(*args, **kwargs)]
    with pytest.raises(ValueError, match="complete content parse|states are stale"):
        build_text_due_review_cards(service, "猫が走る。\n犬も走る。", "synthetic")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_owned_anime_session_disables_idle_but_respects_explicit_keep_open(monkeypatch, platform):
    monkeypatch.setattr("pudge.player.sys.platform", platform)
    command = build_mpv_command("mpv", Path("synthetic.mkv"), None, None, ["--idle=yes", "--keep-open=always"])
    assert command.index("--idle=no") > command.index("--idle=yes")
    assert "--keep-open=always" in command
    assert "--keep-open=no" not in command


@pytest.mark.skipif(LUA is None, reason="no lua/luajit interpreter")
def test_mpv_close_quits_and_explain_only_uses_the_shortcut(tmp_path):
    scenario = tmp_path / "close-and-explain.lua"
    scenario.write_text("""
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
""", encoding="utf-8")
    env = dict(os.environ, PUDGE_LLM_CONFIGURED="1", PUDGE_PLAYBACK_VIDEO="synthetic.mkv")
    subprocess.run([LUA, str(MOCK), str(scenario), str(TRACKER_LUA)], check=True, capture_output=True, text=True, env=env)


def test_ln_entry_preserves_previous_screen_while_review_is_loading_and_cancelled():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", str(root / "tests/js/content_entry_cancel.cjs"), str(root / "pudge/web/index.html"), str(root / "pudge/web/manga_reader_v2.js")], check=True, capture_output=True, text=True)


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


@pytest.mark.parametrize("reply", [
    b'{"request_id":73,"error":"success","data":false}\n',
    b'{"request_id":73,"error":"property unavailable"}\n',
    b'[]\n',
    b'invalid\n',
])
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
