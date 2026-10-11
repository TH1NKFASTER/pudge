"""Accumulated 0.7.0 fixes, batch 2 (behaviour; audit 71Б rewrite). Window checks run the real
index.html (tests/page_harness.py)."""

from __future__ import annotations
from pathlib import Path
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_unnumbered_ln_defaults_to_one_and_groups(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    a = tmp_path / "あそびのかんけい.txt"
    b = tmp_path / "あそびのかんけい２.txt"
    a.write_text("一", encoding="utf-8")
    b.write_text("二", encoding="utf-8")
    first = service.import_file(a)
    second = service.import_file(b)
    assert first["volume"] == 1
    assert second["volume"] == 2
    rows = service.books()
    assert len({row["series_key"] for row in rows}) == 1


def test_ln_delete_keeps_source(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    source = tmp_path / "book.txt"
    source.write_text("本文", encoding="utf-8")
    book = service.import_file(source)
    managed = Path(book["file_path"])
    service.delete_book(book["id"], delete_file=False)
    assert managed.is_file()
    assert not service.books()


def test_folder_audiobook_cumulative_chapters(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    folder = tmp_path / "Audio Book"
    folder.mkdir()
    (folder / "01 Intro.mp3").write_bytes(b"a")
    (folder / "02 Chapter.mp3").write_bytes(b"b")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda p: ((10.0 if p.name.startswith("01") else 20.0), []))
    book = service.import_folder(folder)
    assert book["multi_file"] is True
    assert book["file_count"] == 2
    assert book["duration"] == 30.0
    assert [c["start"] for c in book["chapters"]] == [0.0, 10.0]


def _show(media_id: int, title: str, **extra) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "local": {"video_path": f"/v/{media_id}.mkv", "state": "waiting_subtitles"},
        "ready_episodes": [1],
        **extra,
    }


def test_home_shows_ready_then_needs_action_then_waiting(page) -> None:
    home = {
        "waiting": [_show(3, "Waiting")],
        "needs_action": [_show(2, "Action", action_code="configure_jimaku")],
        "new_ready": [_show(1, "Ready")],
    }
    result = page.run(
        "return [...document.querySelectorAll('#currentContent [data-home-section]')].map(s => s.dataset.homeSection);",
        state={"home": home},
    )
    assert result.value == ["section.newReady", "section.needsAction", "section.waitingPreparation"]


def test_ln_card_keeps_anilist_details_in_its_context_menu(page) -> None:
    book = {
        "id": 4,
        "title": "Novel",
        "volume": 1,
        "anilist_id": 12345,
        "anilist_status": "CURRENT",
        "anilist_site_url": "https://anilist.co/manga/12345",
        "character_count": 100,
        "reading_progress": 0.1,
    }
    result = page.run(
        r"""
        await __click('.nav button[data-page="lightnovels"]'); await __tick(100);
        const text = __text('#lightNovelsContent [data-ln-book="4"]');
        await __fire('#lightNovelsContent [data-ln-book="4"]', 'contextmenu', {bubbles: true, cancelable: true, clientX: 20, clientY: 20}, 'MouseEvent');
        await __tick(20);
        const action = document.querySelector('#contextMenu [data-ln-context-action="open-anilist"]');
        const label = action?.innerText;
        await __click('#contextMenu [data-ln-context-action="open-anilist"]'); await __tick(20);
        return {text, label, expected: t('action.openAniList')};
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}},
    )
    out = result.value
    # The card stays clean (no "AniList #12345", no list status); AniList is one click away.
    assert "12345" not in out["text"] and "CURRENT" not in out["text"] and "AniList" not in out["text"]
    assert out["label"] == out["expected"]
    assert result.calls_of("open_url") == [["https://anilist.co/manga/12345"]]


def test_agent_settings_live_in_the_advanced_tab(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const agent = $('s_agent_enabled').closest('.setting-block');
        const onFirstTab = agent.checkVisibility();
        await __click('#settingsContent .settings-categories [data-settings-category="advanced"]'); await __tick(20);
        return {onFirstTab, onAdvanced: agent.checkVisibility(), playbackSwitch: Boolean($('s_playback_enabled'))};
        """
    )
    assert result.value == {"onFirstTab": False, "onAdvanced": True, "playbackSwitch": False}


def test_debug_overlay_asks_for_a_fresh_subtitle_selection(page) -> None:
    result = page.run(
        r"""
        await __settle(window.PudgeDebug.open({media_id: 7, title: 'Show'}));
        await __click('[data-debug-tab="subtitles"]'); await __tick(20);
        await __click('[data-debug-action="fresh-subtitles"]'); await __tick(20);
        const asked = $('pudgeConfirmMessage').textContent;
        await __click('[data-pudge-confirm="ok"]'); await __tick(20);
        return asked;
        """,
        api={
            "anime_debug_snapshot": {
                "selected_local_episode": {"video_path": "/v/7.mkv"},
                "selected_episode": 3,
            },
            "debug_reselect_subtitles": {"ok": True},
        },
    )
    assert result.value
    assert result.calls_of("debug_reselect_subtitles") == [["/v/7.mkv"]]


def test_fresh_subtitle_selection_searches_and_syncs_from_scratch(tmp_path: Path, monkeypatch) -> None:
    manager = AnimeManager(_cfg(tmp_path), log=lambda _message: None)
    video = (tmp_path / "Show - 03.mkv").resolve()
    video.write_bytes(b"video")
    cached = manager.config.paths.cache_dir / "prepared" / "Show - 03.srt"
    cached.parent.mkdir(parents=True)
    cached.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    manager.db.upsert_anime(LibraryAnime(media_id=7, title="Show", episodes=12, format="TV"))
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=7, title="Show", episode=3, video_path=video, subtitle_path=cached, state="ready"
        )
    )
    commands: list[list[str]] = []

    class Process:
        def __init__(self, command, **_kwargs):
            commands.append(command)
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return f"PREPARED_SUBTITLE={cached}\nPREPARE_STATUS=ready\n", ""

    monkeypatch.setattr("pudge.manager.subprocess.Popen", Process)
    assert manager.force_fresh_subtitle_selection(video)["ok"] is True
    # The cached selection is gone and the episode is queued again ...
    assert not cached.exists() and manager.db.episode_by_path(video).subtitle_path is None
    manager.process_subtitle_jobs(limit=1)
    # ... and is prepared with a full new search and a new sync.
    command = next(command for command in commands if "--media-id" in command)
    assert "--force-search" in command and "--resync" in command


def test_audiobook_cards_stop_and_remove_and_folders_come_from_the_import_menu(page) -> None:
    books = [
        {
            "id": 5,
            "title": "Book Five",
            "playing": True,
            "duration": 3600,
            "position": 60,
            "chapters": [],
            "bookmarks": [],
        },
        {
            "id": 6,
            "title": "Book Six",
            "playing": False,
            "duration": 1800,
            "position": 0,
            "chapters": [],
            "bookmarks": [],
        },
    ]
    result = page.run(
        r"""
        await __click('.nav button[data-page="audiobooks"]'); await __tick(100);
        await __click('.audiobook-card[data-audiobook-id="5"] [data-media-action="stop-audio"]'); await __tick(50);
        const out = {toasts: __toasts(), stopped: t('media.stoppedPositionSaved')};
        await __click('.audiobook-card[data-audiobook-id="6"] [data-media-action="delete-audio"]'); await __tick(20);
        out.asked = $('pudgeConfirmMessage').textContent; out.expectedQuestion = t('media.removeThisAudiobookFromPudge');
        await __click('[data-pudge-confirm="ok"]'); await __tick(50);
        // Folders are imported from the page's Import menu (no separate folder button).
        await __click('#pageImportButton'); await __tick(20);
        out.menu = [...document.querySelectorAll('#audiobookImportMenu [data-audio-import-kind]')].map(button => button.innerText);
        out.expectedMenu = [t('media.file'), t('media.folder')];
        await __click('#audiobookImportMenu [data-audio-import-kind="folder"]'); await __tick(50);
        return out;
        """,
        api={
            "audiobook_state": {"books": books},
            "audiobook_stop": {"ok": True, "books": books},
            "audiobook_delete": {"ok": True, "books": books[:1]},
            "choose_audiobook_folder": {"cancelled": True},
        },
    )
    out = result.value
    assert out["stopped"] in out["toasts"] and result.calls_of("audiobook_stop") == [[5]]
    assert out["asked"] == out["expectedQuestion"] and result.calls_of("audiobook_delete") == [[6]]
    assert out["menu"] == out["expectedMenu"] and result.calls_of("choose_audiobook_folder") == [[]]


# The installer keeps the user's torrent backend choice on update (it used to switch aria2 on):
# test_install_script.py::test_update_keeps_the_torrent_backend_choice_and_refreshes_tool_paths.
