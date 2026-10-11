from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.reading_audio_alignment import light_novel_position_for_audio
from pudge.web_app import WebAppApi, _plain_anilist_description
from tests.i18n_support import en, has_text


def _audiobook_service(tmp_path: Path) -> tuple[AudiobookService, int]:
    database = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        database,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    book = service._upsert(
        path=tmp_path / "book.m4b",
        title="Book",
        duration=120.0,
        files=[
            {
                "index": 0,
                "path": str(tmp_path / "book.m4b"),
                "title": "Book",
                "duration": 120.0,
                "start": 0.0,
                "end": 120.0,
            }
        ],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 120.0}],
    )
    return service, int(book["id"])


def test_audio_position_exposes_exact_interpolation_window() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 2,
                "normalized_length": 100,
                "start": 10.0,
                "end": 30.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 40, "time": 18.0},
                    {"offset": 100, "time": 30.0},
                ],
            }
        ]
    }
    result = light_novel_position_for_audio(alignment, 14.0)
    assert result is not None
    assert result["chapter_char_offset_exact"] == 20.0
    assert result["anchor_window"] == {
        "left_time": 10.0,
        "left_offset": 0.0,
        "right_time": 18.0,
        "right_offset": 40.0,
        "path": [
            {"time": 10.0, "offset": 0.0},
            {"time": 18.0, "offset": 40.0},
        ],
    }


def test_live_audiobook_seek_and_slowdown_use_existing_mpv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, book_id = _audiobook_service(tmp_path)
    process = SimpleNamespace(poll=lambda: None)
    ipc_path = tmp_path / "book.sock"
    service._players[book_id] = process
    service._ipc_paths[book_id] = ipc_path
    live = {"speed": 3.0, "time-pos": 12.0, "playlist-pos": 0}
    commands: list[list[object]] = []

    def command(_path: Path, payload: list[object]) -> dict[str, str]:
        commands.append(payload)
        if payload[:2] == ["set_property", "speed"]:
            live["speed"] = float(payload[2])
        if payload[:1] == ["seek"]:
            live["time-pos"] = float(payload[1])
        return {"error": "success"}

    monkeypatch.setattr(service, "_ipc_command", command)
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: live.get(name))
    monkeypatch.setattr(
        service,
        "play",
        lambda *_args, **_kwargs: pytest.fail("live same-file controls must not restart mpv"),
    )

    service.seek_to(book_id, 54.0)
    service.set_speed(book_id, 1.0)

    assert ["seek", 54.0, "absolute", "exact"] in commands
    assert ["set_property", "speed", 1.0] in commands
    assert service.book(book_id)["position"] == 54.0
    assert service.book(book_id)["speed"] == 1.0


def test_planning_episode_uses_full_ranked_search_after_local_precheck() -> None:
    calls: list[dict[str, object]] = []
    anime = SimpleNamespace(
        media_status="RELEASING",
        released_episodes=4,
        episodes=12,
        next_airing_episode=5,
    )
    manager = SimpleNamespace(
        db=SimpleNamespace(get_anime=lambda _media_id: anime),
        search_and_add_best=lambda media_id, **kwargs: calls.append({"media_id": media_id, **kwargs}) or None,
    )
    api = object.__new__(WebAppApi)
    api.manager = manager
    api._local_episode_for_relative = lambda *_args: None

    result = api.add_best_planning_episode(99, 3)

    assert result == {
        "ok": False,
        "episode": 3,
        "local": False,
        "release": None,
    }
    assert calls == [{"media_id": 99, "episode": 3, "batch": False, "automatic": False}]


def test_planning_description_is_plain_text() -> None:
    value = "Story<br><br>1. <b>First</b> &amp; second<br/>2. <i>Next</i>"
    assert _plain_anilist_description(value) == "Story\n1. First & second\n2. Next"


# Light novel 6 read together with audiobook 75 (alignment ready, playing). Paired state
# polls answer window.__state; with window.__slow they never answer (a poll still in flight).
PAIRED = """
window.__state = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: {ready: true, status: 'ready'}, alignment_mode: 'stt', linked: true,
  playing: true, player_running: true, paused: false, position: 100, speed: 1, duration: 3600, chapter_char_offset_exact: 0, chapter_char_offset: 0,
  anchor_window: {left_time: 100, left_offset: 0, right_time: 104, right_offset: 6, path: [{time: 100, offset: 0}, {time: 104, offset: 6}]}};
window.__slow = false;
window.__polls = [];
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true}, book: {id: 75, playing: true, player_running: true, paused: false}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫が好きです。'],
  tokens: [[{start: 0, end: 1, surface: '猫'}, {start: 1, end: 2, surface: 'が'}, {start: 2, end: 4, surface: '好き'}, {start: 4, end: 6, surface: 'です'}]]});
__api.light_novel_paired_state = () => window.__slow ? new Promise(resolve => window.__polls.push(resolve)) : Promise.resolve({...window.__state});
__api.audiobook_seek = async () => ({ok: true});
__api.light_novel_play_paired_at_offset = async (_book, _chapter, offset) => {
  window.__state = {...window.__state, position: 100 + offset * 4 / 6, chapter_char_offset_exact: offset, chapter_char_offset: offset,
                    anchor_window: {...window.__state.anchor_window, path: [{time: 100 + offset * 4 / 6, offset}, {time: 104, offset: 6}]}};
  return {...window.__state};
};
"""
CURRENT = "const current = () => [...document.querySelectorAll('#lnReader .ln-paired-word-current')].map(node => node.textContent);"
LN = {
    "state": {"settings": {"review_gate_ln_enabled": False}},
    "api": {"startup_maintenance": {"running": False}},
    "before": PAIRED,
}


def test_listen_together_seeks_15_seconds_from_the_tray_and_5_or_15_with_arrows(page) -> None:
    assert en("ui.playFromHere") == "Play from here"
    result = page.run(
        """
        await __settle(openLightNovel(6)); await __tick(300);
        const seeks = [...document.querySelectorAll('#lnPairedTray [data-ln-audio-seek]')].map(b => [Number(b.dataset.lnAudioSeek), b.textContent, b.title]);
        await __click('[data-ln-audio-seek="15"]'); await __tick(50);
        await __click('[data-ln-audio-seek="-15"]'); await __tick(50);
        for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) { await __key(key, {}, 'body'); await __tick(50); }
        await __key('ArrowRight', {metaKey: true}, 'body'); await __tick(50);          // not with modifiers
        return {seeks, titles: [t('ui.back15Seconds'), t('ui.forward15Seconds')]};
        """,
        **LN,
    )
    seeks = result.value["seeks"]
    assert seeks == [[-15, "↶15", result.value["titles"][0]], [15, "15↷", result.value["titles"][1]]]
    assert result.calls_of("audiobook_seek") == [[75, 15], [75, -15], [75, -5], [75, 5], [75, -15], [75, 15]]


def test_play_from_here_starts_the_audiobook_at_the_clicked_word(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            CURRENT
            + """
            await __settle(openLightNovel(6)); await __tick(300);
            const before = current();
            window.__slow = true; await __tick(1100);                // a poll is now in flight ...
            const old = {...window.__state};                         // ... and will answer the old position
            await __click('#lnReader [data-ln-token="0:3"]'); await __tick(100);
            const actions = [...document.querySelectorAll('[data-pudge-study-extra-action]')].map(b => [b.dataset.pudgeStudyExtraAction, b.textContent]);
            await __click('[data-pudge-study-extra-action="paired-audio-here"]'); await __tick(20);
            const jumped = current();
            window.__polls[0](old); await __tick(20);
            return {before, actions, jumped, afterStalePoll: current(), labels: [t('ui.playFromHere'), t('ui.readUpToHere')]};
            """,
            lang=lang,
            **LN,
        )
        out = result.value
        assert out["before"] == ["猫"]
        assert out["actions"] == [
            ["paired-audio-here", out["labels"][0]],
            ["bookmark-here", out["labels"][1]],
        ]
        assert result.calls_of("light_novel_play_paired_at_offset") == [[6, 0, 4, 1]]
        assert out["jumped"] == ["です"]
        assert out["afterStalePoll"] == ["です"]  # the answer of a poll sent before the jump is dropped


def test_listen_together_highlight_moves_between_polls(page) -> None:
    # Polls come once a second (here: never answer); the highlight follows the audio clock
    # along the alignment in between.
    result = page.run(
        CURRENT
        + """
        await __settle(openLightNovel(6)); await __tick(300);
        const out = {start: current()};
        window.__slow = true;
        await __tick(1600); out.later = current();
        await __tick(1500); out.end = current();
        return out;
        """,
        **LN,
    )
    assert result.value == {"start": ["猫"], "later": ["好き"], "end": ["です"]}


def test_planning_release_dialog_offers_episode_by_episode_download_when_no_pack_fits(page) -> None:
    anime = {
        "media_id": 9,
        "title": "Planned",
        "episodes": 3,
        "media_status": "FINISHED",
        "status": "PLANNING",
        "progress": 0,
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('planned', true); await __tick(20);
            await openRelease(9, null, true); await __tick(20);
            const out = {body: __text('#modalBody'), expected: [t('label.noReleases'), PudgeI18n.t('ui.noSuitableSeriesPack'),
                         PudgeI18n.t('ui.chooseSeparateEpisodes'), PudgeI18n.t('ui.downloadReleasedAutomatically')]};
            await __click('#modalBody [data-action="planning-episodes-manual"]'); await __tick(20);
            out.chooser = [...document.querySelectorAll('#modalBody [data-action="planning-episode-release"]')].map(b => [Number(b.dataset.episode), b.disabled]);
            out.local = PudgeI18n.t('ui.alreadyLocal'); out.firstText = __text('#modalBody [data-episode="1"]');
            await __click('#modalBody [data-action="planning-episodes-auto"]'); await __tick(20);
            out.started = __text('#modalTitle'); out.searching = PudgeI18n.t('ui.searchingReleasedEpisodes');
            return out;
            """,
            lang=lang,
            state={"planned": [anime]},
            api={
                "search_releases": [],
                "planning_episode_download_preview": {"total": 3, "title": "Planned", "local_episodes": [1]},
                "start_planning_episode_download": {
                    "running": True,
                    "title": "Planned",
                    "current": 0,
                    "total": 3,
                    "episodes": [],
                },
            },
        )
        out = result.value
        assert all(text in out["body"] for text in out["expected"])
        assert out["chooser"] == [[1, True], [2, False], [3, False]] and out["local"] in out["firstText"]
        assert out["started"].startswith(out["searching"])
        assert result.calls_of("search_releases") == [[9, None, True]]
        assert result.calls_of("start_planning_episode_download") == [[9]]


def test_planning_suggestions_have_no_click_the_card_hint(page) -> None:
    assert not has_text("Click the card to open AniList")
    assert not has_text("Нажмите карточку, чтобы открыть AniList", "ru")
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('planned', true); await __tick(20);
            renderPlannedSuggestions([{media_id: 9, media_kind: 'anime', title: 'Suggested', site_url: 'https://anilist.co/anime/9'}], 'sug');
            const card = document.querySelector('.planned-suggestion-grid article');
            await __click('.planned-suggestion-grid article');
            return {text: __text('#plannedSuggestions'), title: card.title, expected: PudgeI18n.t('ui.openOnAnilist')};
            """,
            lang=lang,
        )
        out = result.value
        assert "Click the card" not in out["text"] and "Нажмите карточку" not in out["text"]
        assert out["title"] == out["expected"]  # the hint is the card's tooltip
        assert result.calls_of("open_url") == [["https://anilist.co/anime/9"]]


def test_audiobook_speed_is_saved_by_the_player_not_the_window(page) -> None:
    book = {"id": 5, "title": "Book", "duration": 600, "position": 30, "speed": 1}
    result = page.run(
        """
        setPage('audiobooks', true); await __tick(50);
        await __input('select[data-audio-speed][data-id="5"]', '1.5'); await __tick(20);
        const keys = Array.from({length: localStorage.length}, (_, index) => localStorage.key(index));
        return {stored: keys.filter(key => key.includes('speed')),
                selected: document.querySelector('select[data-audio-speed][data-id="5"]').value};
        """,
        api={"audiobook_state": {"books": [book]}, "audiobook_set_speed": {"book": {**book, "speed": 1.5}}},
    )
    assert result.calls_of("audiobook_set_speed") == [[5, 1.5]]
    assert result.value == {"stored": [], "selected": "1.5"}
