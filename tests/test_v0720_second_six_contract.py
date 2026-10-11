from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from test_v0722_unified_audiobook_controls import AUDIO_PAGE, paired_ln
from tests.i18n_support import en, has_text, ru


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.nyaa.max_auto_download_per_anime = 2
    cfg.qbittorrent.enabled = True
    return AnimeManager(cfg, log=lambda _message: None)


def _release() -> NyaaRelease:
    return NyaaRelease(
        title="[SubsPlease] Sayonara Lara - 07 (1080p)",
        link="https://nyaa.si/view/7",
        torrent_url="https://nyaa.si/download/7.torrent",
        info_hash="7" * 40,
        size_text="1.2 GiB",
        size_bytes=1200 * 1024 * 1024,
        seeders=100,
        leechers=10,
        downloads=200,
        trusted=True,
        remake=False,
        group="SubsPlease",
        score=200.0,
        reasons=["exact-title-phrase", "single-episode=7", "1080p", "size-floor-ok"],
    )


def test_auto_search_skips_owned_episodes_without_consuming_search_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    media_id = 7007
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=media_id,
            title="Sayonara Lara",
            status="CURRENT",
            progress=1,
            episodes=7,
        )
    )
    for episode in range(2, 7):
        path = manager.config.library.root_dir / f"Sayonara Lara - {episode:02d}.mkv"
        path.write_bytes(b"video")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title="Sayonara Lara",
                episode=episode,
                video_path=path,
            )
        )

    release = _release()
    searched: list[int] = []
    added: list[int] = []

    def fake_search(_media_id: int, *, episode: int | None, batch: bool, automatic: bool = False):
        assert batch is False
        assert automatic is True
        searched.append(int(episode or 0))
        return [release]

    def fake_add(_media_id: int, _item: NyaaRelease, *, episode: int | None, batch: bool):
        assert batch is False
        added.append(int(episode or 0))
        return release

    monkeypatch.setattr(manager, "search_releases", fake_search)
    monkeypatch.setattr(manager, "add_release", fake_add)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)

    assert manager.auto_search_current() == 1
    assert searched == [7]
    assert added == [7]


def test_queue_next_is_removed_but_franchise_queue_remains(page) -> None:
    show = {
        "media_id": 7,
        "title": "Show",
        "next_episode": 3,
        "site_url": "https://anilist.co/anime/7",
        "episodes": 12,
        "media_status": "RELEASING",
        "queue_next_count": 3,
        "queue_franchise_available": True,
    }
    assert not has_text("Queue next") and not has_text("Добавить следующие", "ru")
    for lang in ("en", "ru"):
        result = page.run(
            """
            await __fire('#currentContent [data-media-id="7"]', 'contextmenu', {clientX: 10, clientY: 10}, 'MouseEvent');
            const items = [...document.querySelectorAll('#contextMenu [data-context-action]')].map(b => b.innerText);
            const franchise = t('action.createFranchiseQueue');
            await __click('#contextMenu [data-context-action="queue-franchise"]'); await __tick(10);
            return {items, franchise};
            """,
            lang=lang,
            state={"current": [show], "home": {"waiting": [show]}},
            api={"create_franchise_queue": {"ok": True}},
        )
        assert result.value["franchise"] in result.value["items"]
        assert result.calls_of("create_franchise_queue") == [[7]]
        assert not result.called("create_next_episodes_queue")


def test_light_novel_toolbar_uses_import_only_and_common_refresh_scans_local_first(
    page, tmp_path, monkeypatch
) -> None:
    assert en("uiB.import") == "Import" and ru("uiB.import") == "Импорт"
    assert not has_text("Import EPUB/TXT")
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('lightnovels', true); await __tick(100);
            const out = {import: [__visible('#pageImportButton'), __text('#pageImportButton'), t('uiB.import')],
                         ownImport: __visible('#lnImport'), ownRefresh: Boolean($('lnRefresh')),
                         buttons: [...document.querySelectorAll('#lightnovels button, #pageImportButton')].map(b => b.textContent)};
            await __click('#pageImportButton'); await __tick(10);
            await __click('#refreshAll'); await __tick(50);
            return out;
            """,
            lang=lang,
            api={"choose_light_novel_file": {"cancelled": True}, "refresh_local": {"state": {}, "stats": {}}},
        )
        out = result.value
        assert out["import"] == [True, out["import"][2], out["import"][2]]
        assert out["ownImport"] is False and out["ownRefresh"] is False
        assert not [
            text for text in out["buttons"] if "EPUB" in text
        ]  # one short "Import", no "Import EPUB/TXT"
        assert result.called("choose_light_novel_file")  # the page Import opens the LN file chooser
        assert result.called("refresh_local") and not result.called("light_novel_refresh")
    # The common Refresh registers downloaded light novels before the slower network refresh.
    from test_web_app import make_api

    api = make_api(tmp_path)
    order = []
    monkeypatch.setattr(api.light_novels, "scan_downloaded", lambda: order.append("light novels") or 0)
    monkeypatch.setattr(
        api.manager, "run_interactive_refresh", lambda **_kwargs: order.append("network") or {}
    )
    try:
        api.refresh_local()
    finally:
        api.close()
    assert order.index("light novels") < order.index("network")


def test_audiobook_tempo_uses_explicit_quality_preserving_wsola(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    expected = ["--audio-pitch-correction=yes"]
    assert service._tempo_filter_args() == expected
    assert service._tempo_filter_args() == expected


def test_tempo_filter_is_kept_for_runtime_speed_changes(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from pudge import audiobooks

    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    service.book = lambda book: {
        "id": book,
        "position": 0,
        "duration": 10,
        "path": str(tmp_path / "audio.wav"),
    }
    service._file_rows = lambda book: [
        {"file_index": 0, "path": str(tmp_path / "audio.wav"), "start": 0, "end": 10}
    ]
    service._tracked_thread = lambda **_kwargs: None
    commands: list[list[str]] = []
    process = type(
        "Process",
        (),
        {
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "kill": lambda self: None,
            "wait": lambda self, timeout=None: 0,
        },
    )()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **_kwargs: commands.append(command) or process)
    service.play(7, start=0, speed=1.5)
    [command] = commands
    assert "--audio-pitch-correction=yes" in command and "--speed=1.500" in command
    # A speed change while playing goes to the running mpv; pitch correction stays on.
    sent = []
    monkeypatch.setattr(
        service, "_ipc_command", lambda _path, payload: sent.append(payload) or {"error": "success"}
    )
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: 1.25 if name == "speed" else None)
    monkeypatch.setattr(audiobooks.time, "sleep", lambda _seconds: None)
    service.set_speed(7, 1.25)
    assert sent[0] == ["set_property", "speed", 1.25]
    assert len(commands) == 1  # no restart: the tempo filter of the running player applies


def test_speed_controls_blur_so_space_returns_to_transport(page) -> None:
    # After choosing a speed, Space plays/pauses again instead of reopening the speed list.
    audio = page.run(
        """
        setPage('audiobooks', true); await __tick(100);
        const shell = document.querySelector('[data-audiobook-id="3"] [data-audio-speed]').closest('.pudge-select');
        shell.querySelector('.pudge-select-button').focus();
        await __click('[data-audiobook-id="3"] .pudge-select:has([data-audio-speed]) .pudge-select-button');
        const option = [...document.querySelectorAll('.pudge-select-option')].find(b => b.checkVisibility() && b.textContent === '1.5×');
        let answer; __api.audiobook_set_speed = () => new Promise(resolve => { answer = resolve; });   // still saving
        option.click(); await __tick(10);
        const focused = document.activeElement?.closest?.('[data-audiobook-id]') ? document.activeElement.className : null;
        await __key(' ', {code: 'Space'}); await __tick(10);
        const menuOpen = [...document.querySelectorAll('.pudge-select-menu.open')].length > 0;
        window.__release(); answer({ok: true}); await __tick(100);
        return {focused, menuOpen};
        """,
        before=AUDIO_PAGE,
    )
    assert audio.value == {"focused": None, "menuOpen": False}
    assert audio.calls_of("audiobook_set_speed") == [[3, 1.5]]
    assert [call[0] for call in audio.calls_of("audiobook_play")] == [3]
    reader = page.run(
        """
        await __settle(openLightNovel(6)); await __tick(300);
        $('lnPairedSpeed').focus();
        await __input('#lnPairedSpeed', '1.5'); await __tick(10);
        const focused = document.activeElement?.id;
        await __key(' ', {code: 'Space'}); await __tick(10); window.__release(); await __tick(300);
        return {focused};
        """,
        **paired_ln(True),
    )
    assert reader.value["focused"] != "lnPairedSpeed"
    assert reader.calls_of("audiobook_set_speed") == [[75, 1.5]]
    assert reader.calls_of("audiobook_set_paused") == [[75, True]]
