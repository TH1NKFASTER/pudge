"""A pause from outside Pudge (F8 / the mpv window) is noticed and resumed in place; context
furigana shows for every reading the reader renders (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pudge.audiobooks as audiobook_module
from pudge.audiobooks import AudiobookService
from t71g21_support import READER, computed_style, paired
from test_web_app import make_api
from tests.i18n_support import en, ru


def _service() -> AudiobookService:
    service = object.__new__(AudiobookService)
    service._lock = threading.Lock()
    process = Mock()
    process.poll.return_value = None
    service._players = {75: process}
    service._ipc_paths = {75: Path("/tmp/pudge-audiobook-test.sock")}
    service._last_positions = {75: 10.0}
    service._last_motion_at = {75: 100.0}
    return service


def test_f8_pause_falls_back_to_stalled_position_detection(monkeypatch) -> None:
    service = _service()
    service._ipc_get = Mock(side_effect=lambda _path, prop: False)
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 101.2)

    assert service.is_playback_active(75) is False


def test_advancing_position_refreshes_motion_clock(monkeypatch) -> None:
    service = _service()
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 105.0)

    service._record_playback_position(75, 10.5)

    assert service._last_positions[75] == 10.5
    assert service._last_motion_at[75] == 105.0


def test_resuming_externally_paused_mpv_uses_existing_process(monkeypatch) -> None:
    service = _service()
    service._ipc_commands_no_wait = Mock(return_value=True)
    monkeypatch.setattr(audiobook_module.time, "monotonic", lambda: 110.0)

    result = service.set_paused(75, False)

    assert result == {
        "ok": True,
        "book_id": 75,
        "playing": True,
        "player_running": True,
        "paused": False,
    }
    service._ipc_commands_no_wait.assert_called_once_with(
        Path("/tmp/pudge-audiobook-test.sock"),
        [["set_property", "pause", False]],
    )
    assert service._last_motion_at[75] == 110.0


def test_web_api_pauses_and_resumes_the_running_player(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    try:
        calls = []
        api.audiobooks.set_paused = lambda book_id, paused: (
            calls.append((book_id, paused)) or {"ok": True, "paused": paused}
        )
        assert api.audiobook_set_paused(75, True) == {"ok": True, "paused": True}
        assert api.audiobook_set_paused("75", 0) == {"ok": True, "paused": False}
        assert calls == [(75, True), (75, False)]
    finally:
        api.close()


def test_reader_notices_an_outside_pause_and_keeps_polling(page) -> None:
    # Polls every 250 ms while playing. F8 pauses mpv behind the reader's back: the next poll
    # stops the highlight where it is, the button offers Resume, and polling slows to 350 ms
    # while the player lives; 700 ms while the text is being aligned; none once idle.
    for lang in ("en", "ru"):
        result = page.run(
            READER
            + r"""
            await openReader();
            const polls = () => __apiCalls.filter(c => c.name === 'light_novel_paired_state').map(c => c.at);
            const gaps = list => list.slice(1).map((t, k) => Math.round(t - list[k]));
            const out = {labels: [t('ui.pause'), t('ui.resume')]};
            await __tick(1000);
            out.playing = {gaps: gaps(polls().slice(-4)), button: __text('#lnPairedAudio')};
            setAudio({playing: false});                       // F8
            await __tick(400);
            const pausedAt = shown(), count = polls().length;
            await __tick(1500);
            out.paused = {gaps: gaps(polls().slice(count)), held: [pausedAt, shown()], button: __text('#lnPairedAudio')};
            setAudio({running: false});                       // mpv quits; the text is still being aligned
            __api.light_novel_paired_state = async () => ({...pairedState(), alignment: {ready: false, status: 'aligning'}});
            await __tick(400);
            const aligning = polls().length;
            await __tick(2200);
            out.aligning = gaps(polls().slice(aligning));
            __api.light_novel_paired_state = async () => pairedState();
            await __tick(800);
            const idle = polls().length;
            await __tick(3000);
            out.idle = polls().length - idle;
            return out;
            """,
            lang=lang,
            **paired(playing=True),
        )
        out = result.value
        pause, resume = out["labels"]
        assert out["playing"] == {"gaps": [250, 250, 250], "button": pause}
        assert out["paused"]["gaps"] == [350, 350, 350]
        assert out["paused"]["held"][0] == out["paused"]["held"][1] and out["paused"]["held"][0] > 0
        assert out["paused"]["button"] == resume
        assert out["aligning"] == [700, 700]
        assert out["idle"] == 0
        assert not result.called("audiobook_set_paused") and not result.called("light_novel_play_paired")
    assert (en("ui.resume"), ru("ui.resume")) == ("Resume", "Продолжить")


def test_resume_of_a_live_player_does_not_wait_for_alignment(page) -> None:
    # The player is paused (F8) while the text is still being aligned: Resume unpauses that
    # player at once instead of starting the alignment or a new playback.
    result = page.run(
        READER
        + r"""
        __api.light_novel_paired_state = async () => ({...pairedState(), alignment: {ready: false, status: 'aligning'}});
        await openReader();
        const before = __text('#lnPairedAudio');
        await __click('#lnPairedAudio'); await __tick(300);
        return {before, after: __text('#lnPairedAudio'), labels: [t('ui.resume'), t('ui.pause')]};
        """,
        **paired(playing=True, extra="AUDIO.playing = false;"),
    )
    resume, pause = result.value["labels"]
    assert (result.value["before"], result.value["after"]) == (resume, pause)
    assert result.calls_of("audiobook_set_paused") == [[75, False]]
    assert not result.called("light_novel_prepare_audio_alignment") and not result.called(
        "light_novel_play_paired"
    )


# A chapter of two known words: 食べた (its furigana comes from the token's reading たべた,
# aligned to the inflection: 食 = た) and 今日 (explicit ruby from the parser).
TWO_READINGS = r"""
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['食べた今日'],
  tokens: [[{start: 0, end: 3, wordId: 300, readingIndex: 0, reading: 'たべた'},
            {start: 3, end: 5, wordId: 301, readingIndex: 0, rubies: [{start: 3, end: 5, text: 'きょう'}]}]],
  vocabulary: [{wordId: 300, readingIndex: 0, spelling: '食べる', reading: 'たべる', cardState: ['known']},
               {wordId: 301, readingIndex: 0, spelling: '今日', reading: 'きょう', cardState: ['known']}]});
"""


def test_context_furigana_reveals_readings_of_both_ruby_kinds(page) -> None:
    for vertical in (False, True):
        result = page.run(
            READER
            + computed_style("visibility", "display")
            + r"""
            await openReader();
            const furigana = () => wordEls().map(w => {
              const ruby = w.querySelector('.ln-furigana-ruby[data-ln-reading]');
              return VERTICAL ? __css(ruby.querySelector('rt'), 'visibility') : __css(ruby, 'display', 'after');
            });
            const readings = wordEls().map(w => w.querySelector('.ln-furigana-ruby')?.dataset.lnReading);
            const quiet = furigana();
            __hover(wordEls()[0]); const first = furigana();
            __hover(wordEls()[1]); const second = furigana();
            return {readings, quiet, first, second};
            """.replace("VERTICAL", str(vertical).lower()),
            **paired(playing=False, settings={"reader_vertical": vertical}, extra=TWO_READINGS),
        )
        shown, hidden = ("visible", "hidden") if vertical else ("block", "none")
        assert result.value["readings"] == ["た", "きょう"]
        assert result.value["quiet"] == [hidden, hidden]
        assert result.value["first"] == [shown, hidden]
        assert result.value["second"] == [hidden, shown]
