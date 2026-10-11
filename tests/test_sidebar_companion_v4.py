from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, load_config, write_config
from pudge.web_app import WebAppApi


def test_sidebar_interval_survives_config_roundtrip_and_missing_keys(tmp_path: Path) -> None:
    config = AppConfig()
    config.ui.sidebar_review_interval = "continuous"
    path = tmp_path / "config.toml"
    write_config(config, path)
    assert load_config(path).ui.sidebar_review_interval == "continuous"
    path.write_text('[ui]\nlanguage="ru"\n')
    assert load_config(path).ui.sidebar_review_interval == ""  # migrate the old window preference


def test_sidebar_preference_endpoint_saves_without_window_storage(tmp_path: Path) -> None:
    api = object.__new__(WebAppApi)
    api.config = AppConfig()
    api.config_path = tmp_path / "config.toml"
    assert api.sidebar_review_settings("continuous") == {"interval": "continuous"}
    assert load_config(api.config_path).ui.sidebar_review_interval == "continuous"
    restarted = object.__new__(WebAppApi)
    restarted.config = load_config(api.config_path)
    assert restarted.sidebar_review_settings() == {"interval": "continuous"}
    with pytest.raises(ValueError):
        api.sidebar_review_settings("bogus")
    assert load_config(api.config_path).ui.sidebar_review_interval == "continuous"


def test_focused_audio_state_avoids_inactive_books_and_transcripts() -> None:
    service = object.__new__(AudiobookService)
    service._lock = threading.RLock()
    service._players = {1: SimpleNamespace(poll=lambda: None), 2: SimpleNamespace(poll=lambda: 0)}
    calls = []

    def book(book_id, *, include_transcription):
        calls.append((book_id, include_transcription))
        return {"id": book_id, "playing": True}

    service.book = book
    assert service.sidebar_state() == {"books": [{"id": 1, "playing": True}]}
    assert calls == [(1, False)]


def test_reader_warmup_only_opens_metadata() -> None:
    api = object.__new__(WebAppApi)
    calls = []
    api.light_novels = SimpleNamespace(open_book=lambda book_id: calls.append(book_id) or {"id": book_id})
    assert api.sidebar_reader_book(7) == {"id": 7}
    assert calls == [7]


def test_paired_clock_runs_between_samples_without_rewinding_or_jumping(page) -> None:
    # The sidebar and the reader animate the narration between backend samples with this clock.
    result = page.run(
        r"""
        const clock = PudgePairedAudioClock.createClock(), out = {};
        clock.reset({position: 10, playing: true, speed: 1});
        await __tick(200); out.running = clock.current();
        clock.reconcile({position: 10.1, playing: true, speed: 1});
        await __tick(200); out.sampled = clock.current();
        clock.reconcile({position: 10.05, playing: true, speed: 1});   // an older sample (poll jitter)
        out.jitter = clock.current();
        clock.reconcile({position: 10.3, playing: false, speed: 1});
        await __tick(19600); out.paused = clock.current();
        clock.reset({position: 100, playing: true, speed: 2});
        await __tick(500); out.doubleSpeed = clock.current();
        const before = clock.current();
        clock.reconcile({position: before + .3, playing: true, speed: 2});   // the player is a bit ahead
        out.atReceipt = clock.current() - before;
        await __tick(100); out.caughtUp = clock.current() - before;
        return out;
        """
    )
    out = result.value
    assert out["running"] == pytest.approx(10.2) and out["sampled"] == pytest.approx(10.4)
    assert out["jitter"] == pytest.approx(10.4)  # never back in time on ordinary jitter
    assert out["paused"] == pytest.approx(10.3)  # a paused clock stands still
    assert out["doubleSpeed"] == pytest.approx(101)
    assert out["atReceipt"] == pytest.approx(
        0, abs=1e-9
    )  # a sample ahead is caught up over frames, not jumped to
    assert 0.2 < out["caughtUp"] < 0.31


def test_activity_clock_holds_the_word_during_silences(page) -> None:
    result = page.run(
        r"""
        const state = {anchor_window: {activity_clock: true, path: [{time: 0, offset: 0}, {time: 8, offset: 8}],
                                       activity: [{start: 0, end: 2}, {start: 6, end: 8}]}};
        const silent = {anchor_window: {...state.anchor_window, activity: []}};
        return {voiced: [1, 3, 5, 7].map(t => PudgePairedAudioClock.offsetAtTime(state, t)),
                noActivity: PudgePairedAudioClock.offsetAtTime(silent, 3)};
        """
    )
    # Speech in 0-2 s and 6-8 s carries the eight characters; 2-6 s is silence.
    assert result.value == {"voiced": [2, 4, 4, 6], "noActivity": 3}


# Audiobook 5 narrates light novel 7 and plays (audio time 100 at page start, one text
# character per second from there); the sidebar shows it with the narrated text. The
# reader's bookmark is in chapter 0, the narration is in chapter 2. AUDIO.hold = true keeps
# further state requests unanswered. sidebar_reader_book answers after half a second.
HANDOFF = r"""
window.WORDS = ['今日', '明日', '昨日', '話し', '先日', '毎日', '本日', '平日'];
window.AUDIO = {start: performance.now(), hold: false};
window.audioPosition = () => 100 + (performance.now() - AUDIO.start) / 1000;
window.pairedState = () => ({audiobook_id: 5, ln_book_id: 7, ln_chapter_index: 2, linked: true, playing: true, player_running: true,
  paused: false, speed: 1, duration: 600, position: audioPosition(), alignment: {ready: true, status: 'ready'}, alignment_mode: 'stt',
  anchor_window: {path: [{time: 100, offset: 0}, {time: 116, offset: 16}], left_time: 100, right_time: 116}});
__api.sidebar_audio_state = async () => ({books: [{id: 5, title: 'Audio', playing: true, player_running: true, position: audioPosition(),
  duration: 600, speed: 1, chapters: [{start: 0, end: 600, title: 'One'}], linked_light_novel: {id: 7, title: 'Novel'}}]});
__api.light_novel_paired_state = () => AUDIO.hold ? new Promise(() => {}) : Promise.resolve(pairedState());
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: [WORDS.join('')],
  tokens: [WORDS.map((w, i) => ({start: 2 * i, end: 2 * i + 2, wordId: 200 + i, readingIndex: 0}))],
  vocabulary: WORDS.map((w, i) => ({wordId: 200 + i, readingIndex: 0, spelling: w, cardState: ['known']}))});
const BOOK = {id: 7, title: 'Novel', current_chapter: 0, current_offset: .4, chapters: [0, 1, 2, 3].map(i => ({chapter_index: i, title: `Chapter ${i}`})),
  paired_audio: {ln_book_id: 7, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'}, book: {id: 5, playing: true, player_running: true, paused: false}}};
__api.sidebar_reader_book = id => new Promise(resolve => setTimeout(() => resolve({...BOOK, id}), 500));
__api.audiobook_review_context = async () => ({ok: true});
__api.audiobook_reader_context = async () => ({ok: true});
"""
TRANSPORT = (
    "audiobook_seek",
    "audiobook_seek_to",
    "audiobook_set_paused",
    "audiobook_stop",
    "audiobook_play",
    "light_novel_play_paired",
    "light_novel_play_paired_at_offset",
    "light_novel_stop_paired",
)


def test_go_to_reading_from_the_sidebar_continues_the_running_narration_in_the_reader(page) -> None:
    result = page.run(
        r"""
        const wordEls = () => [...document.querySelectorAll('#lnReader .ln-word')];
        const shown = () => {
          const w = wordEls().find(w => w.classList.contains('ln-paired-word-current')); if (!w) return null;
          const start = Number(w.dataset.lnAudioStart), end = Number(w.dataset.lnAudioEnd);
          return start + (end - start) * parseFloat(w.style.getPropertyValue('--ln-paired-word-progress') || '0') / 100;
        };
        const expected = () => PudgePairedAudioClock.offsetAtTime(pairedState(), audioPosition());
        await __tick(3000);                                   // the sidebar follows the narration
        const out = {sidebar: __visible('[data-sc-book="5"] [data-sc-live-text]')};
        AUDIO.hold = true;                                    // only the handoff can place the reader's highlight now
        await __fire('.sidebar-audio', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
        await __click('#sidebarAudioMenu [data-sc-menu="read"]');
        await __tick(700);
        out.reader = {open: $('lnReaderShell').classList.contains('open'), title: __text('#lnReaderTitle'),
                      text: __text('#lnReader'), chapter: $('lnChapterSelect').value,
                      controls: __visible('#lnPairedTime'), button: __text('#lnPairedAudio'), pause: t('ui.pause')};
        out.highlight = {shown: shown(), expected: expected()};
        await __tick(1000);
        out.later = {shown: shown(), expected: expected()};
        return out;
        """,
        before=HANDOFF,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    out = result.value
    assert out["sidebar"] is True
    reader = out["reader"]
    assert reader["open"] and reader["title"] == "Novel"
    # The reader opens where the narration is (chapter 2), not at the bookmark (chapter 0) ...
    assert reader["chapter"] == "2" and "今日明日" in reader["text"]
    assert [7, 0] not in result.calls_of("light_novel_chapter")
    # ... with the chapter the sidebar had already loaded and the book's metadata fetched at the click.
    assert result.calls_of("light_novel_chapter").count([7, 2]) == 1
    assert result.calls_of("sidebar_reader_book") == [[7]] and not result.called("light_novel_open")
    # The narration goes on: the transport is shown as playing and nothing restarts or seeks it,
    assert reader["controls"] is True and reader["button"] == reader["pause"]
    assert not any(result.called(name) for name in TRANSPORT)
    # and the highlight is where the audio is now, the half second of opening included.
    assert out["highlight"]["shown"] == pytest.approx(out["highlight"]["expected"], abs=0.15)
    assert out["later"]["shown"] == pytest.approx(out["later"]["expected"], abs=0.15)
    # The reader takes the reviews of this audiobook over and tells the backend what it shows.
    assert result.calls_of("audiobook_review_context")[-1] == [5, "ln"]
    [announced] = [args[0] for args in result.calls_of("audiobook_reader_context") if args[0]["open"]]
    assert (announced["ln_book_id"], announced["audiobook_id"]) == (7, 5)
