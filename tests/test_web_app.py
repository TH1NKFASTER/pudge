from __future__ import annotations

import urllib.request
from pathlib import Path

from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager_models import LibraryAnime
from anime_mpv.web_app import WebAppApi, _start_asset_server


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_get_state_reads_local_database_only(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=123,
            title="Cached Anime",
            status="PLANNING",
            episodes=12,
            media_status="FINISHED",
            mean_score=84,
        )
    )

    state = api.get_state()

    assert state["current"] == []
    assert state["planned"][0]["title"] == "Cached Anime"
    assert state["planned"][0]["media_status"] == "FINISHED"
    assert state["planned"][0]["finished"] is True
    assert state["planned"][0]["mean_score"] == 84


def test_save_settings_persists_api_keys(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    result = api.save_settings(
        {
            "library_root": str(tmp_path / "anime"),
            "anilist_token": "ani-token",
            "jimaku_api_key": "jimaku-key",
            "llm_api_key": "llm-key",
            "qbt_api_key": "qbt-key",
            "anilist_threshold": 83.3,
        }
    )

    assert result["ok"] is True
    assert api.config.anilist.access_token == "ani-token"
    assert api.config.jimaku.api_key == "jimaku-key"
    assert api.config.llm.api_key == "llm-key"
    assert api.config.qbittorrent.api_key == "qbt-key"
    assert api.config.ui.language == "en"


def test_asset_server_serves_web_ui(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    server, base_url = _start_asset_server(api)
    try:
        with urllib.request.urlopen(f"{base_url}/index.html", timeout=3) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    assert "Anime MPV" in html
    assert "plannedFilter" in html
    assert "-webkit-overflow-scrolling:touch" in html


def test_planned_filters_match_actual_anilist_media_status() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "const isFinished=a=>String(a.media_status||'').trim().toUpperCase()==='FINISHED'" in html
    assert "if(filter==='finished')items=items.filter(isFinished)" in html
    assert "if(filter==='unfinished')items=items.filter(a=>!isFinished(a))" in html


def test_qbt_test_uses_unsaved_form_values(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    captured = {}

    class FakeClient:
        def __init__(self, base_url, username, password, api_key, **kwargs):
            captured.update(base_url=base_url, username=username, password=password, api_key=api_key)
            self.base_url = "http://127.0.0.1:9191"

        def version(self):
            return "v5.2.0"

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.web_app.QBittorrentClient", FakeClient)
    result = api.test_qbittorrent(
        {
            "qbt_url": "http://localhost:9191",
            "qbt_user": "new-user",
            "qbt_password": "new-pass",
            "qbt_api_key": "new-key",
        }
    )

    assert captured == {
        "base_url": "http://localhost:9191",
        "username": "new-user",
        "password": "new-pass",
        "api_key": "new-key",
    }
    assert result["version"] == "v5.2.0"
    assert result["url"] == "http://127.0.0.1:9191"


def test_play_deduplicates_same_video(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.code = None

        def poll(self):
            return self.code

    process = FakeProcess()
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr("anime_mpv.web_app.subprocess.Popen", fake_popen)

    first = api.play(str(video))
    second = api.play(str(video))

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["status"] == "starting"
    assert len(calls) == 1
    assert api.play_status(str(video))["status"] == "starting"


def test_web_ui_has_play_progress_and_nyaa_socks_preset() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "play_status" in html
    assert "Запускаю…" in html
    assert "socks5://[::1]:1080" in html
    assert "Прокси применяется только к запросам Anime MPV в Nyaa" in html


def test_web_state_reports_ready_without_sidecar_as_embedded(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    video = tmp_path / "embedded.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Embedded",
            episode=1,
            video_path=video,
            subtitle_path=None,
            state="ready",
        )
    )

    episode = api.get_state()["episodes"][0]
    assert episode["subtitle"] is True
    assert episode["subtitle_source"] == "embedded"


def test_not_yet_released_cards_hide_progress_and_freshness() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "const unreleased=a.media_status==='NOT_YET_RELEASED'" in html
    assert "const progress=unreleased?'':`<div class=\"meta\">${t('label.watched')}:" in html
    assert "const freshness=planned||unreleased?'':a.outdated?" in html
    assert "<article class=\"anime-card ${a.outdated?'outdated':''}" in html


def test_refresh_local_does_not_sync_anilist(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls = {"local": 0, "anilist": 0}

    def local_refresh():
        calls["local"] += 1
        return {"library": 0}

    def anilist_refresh():
        calls["anilist"] += 1
        return {"anime": 0, "covers": 0}

    monkeypatch.setattr(api.manager, "run_interactive_refresh", local_refresh)
    monkeypatch.setattr(api.manager, "refresh_anilist_cache", anilist_refresh)

    api.refresh_local()

    assert calls == {"local": 1, "anilist": 0}


def test_startup_anilist_sync_runs_only_once_per_app_session(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls = 0

    def refresh():
        nonlocal calls
        calls += 1
        return {"anime": 3, "covers": 3}

    monkeypatch.setattr(api.manager, "refresh_anilist_cache", refresh)

    first = api.startup_sync_anilist()
    second = api.startup_sync_anilist()

    assert first["skipped"] is False
    assert second["skipped"] is True
    assert calls == 1


def test_manual_anilist_sync_can_run_again(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls = 0

    def refresh():
        nonlocal calls
        calls += 1
        return {"anime": 2, "covers": 2}

    monkeypatch.setattr(api.manager, "refresh_anilist_cache", refresh)

    api.sync_anilist()
    api.sync_anilist()

    assert calls == 2


def test_refresh_button_is_reset_after_each_local_refresh() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "localRefreshing:false" in html
    assert "ui.localRefreshing=false" in html
    assert "duePrioritySubtitleJobs().length>0" in html
    assert "status.subtitleCheckingBackground" in html


def test_watchable_anime_cards_have_polychrome_gloss() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "function isWatchable(a)" in html
    assert "polychrome-flow" in html
    assert "cover-shell.polychrome" in html
    assert "cover(a,'airing-cover',true)" in html
    assert ".anime-card.watchable" not in html


def test_not_yet_released_planned_card_has_no_download_button() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "a.media_status==='NOT_YET_RELEASED'?'':`<button data-action=\"release\"" in html


def test_web_ui_defaults_to_english_and_offers_russian() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in html
    assert "'nav.current':'Anime'" in html
    assert "'nav.current':'Аниме'" in html
    assert '<option value="ru">Русский</option>' in html


def test_web_ui_has_polychrome_a_logo_and_stronger_cover_foil() -> None:
    root = Path(__file__).parents[1] / "anime_mpv"
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert (root / "assets" / "app-icon.png").is_file()
    assert (root / "web" / "app-logo.png").is_file()
    assert 'src="app-logo.png"' in html
    assert 'opacity:.9' in html
    assert '0 0 34px rgba(255,84,208,.24)' in html


def test_startup_workflow_uses_adaptive_energy_efficient_polling() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "startup_maintenance" in html
    assert "poll_downloads_and_subtitles" in html
    assert "foregroundPollDelay" in html
    assert "document.hidden" in html
    assert "120000" in html
    assert "60000" in html
    assert "20000" in html
    assert "5000" in html
    assert "2000" in html
    assert "adaptiveDownloadPollDelay" in html
    assert "subtitle_jobs.some" not in html


def test_save_settings_persists_selected_language(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    result = api.save_settings({"language": "ru"})
    assert result["settings"]["language"] == "ru"
    assert api.config.ui.language == "ru"


def test_startup_maintenance_runs_once_and_refreshes_new_torrents(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.qbittorrent.enabled = True
    calls = {"run": 0, "sync": 0}

    def run_once():
        calls["run"] += 1
        return {"auto": 1}

    def sync_downloads():
        calls["sync"] += 1
        return 1

    monkeypatch.setattr(api.manager, "run_startup_once", run_once)
    monkeypatch.setattr(api.manager, "sync_downloads", sync_downloads)

    first = api.startup_maintenance()
    assert first["skipped"] is False
    assert first["running"] is True

    api._startup_maintenance_thread.join(timeout=3)
    status = api.startup_maintenance_status()
    second = api.startup_maintenance()

    assert status["done"] is True
    assert status["stats"]["downloads_after_auto"] == 1
    assert second["skipped"] is True
    assert calls == {"run": 1, "sync": 1}


def test_foreground_poll_checks_downloads_without_running_heavy_subtitles(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: calls.append("downloads") or 2)
    monkeypatch.setattr(
        api.manager,
        "process_subtitle_jobs",
        lambda limit=4: (_ for _ in ()).throw(AssertionError("foreground must not run subtitle jobs")),
    )

    result = api.poll_downloads_and_subtitles()

    assert calls == ["downloads"]
    assert result["stats"] == {"downloads": 2, "subs": 0}



def test_startup_status_uses_cached_storage_while_running(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    initial = api.get_state()["storage"]

    class RunningThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    api._startup_maintenance_thread = RunningThread()
    monkeypatch.setattr(
        api.manager,
        "storage_status",
        lambda: (_ for _ in ()).throw(AssertionError("startup poll must not rescan library storage")),
    )

    status = api.startup_maintenance_status()

    assert status["running"] is True
    assert status["state"]["storage"] == initial


def test_asset_server_serves_logo(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    server, base_url = _start_asset_server(api)
    try:
        with urllib.request.urlopen(f"{base_url}/app-logo.png", timeout=3) as response:
            content = response.read()
    finally:
        server.shutdown()
        server.server_close()
    assert content.startswith(b"\x89PNG")


def test_startup_syncs_anilist_before_background_maintenance() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    sequence = html[html.index("async function startupSequence"):html.index("async function loadState")]
    assert sequence.index("syncAniList(true)") < sequence.index("startup_maintenance")
    assert "setTimeout(pollStartupMaintenance,3000)" in html
    assert "setTimeout(pollForegroundWork,1000)" in sequence


def test_web_ui_exposes_timing_diagnostics() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="openDiagnosticsPage"' in html
    assert 'id="diagnostics" class="page"' in html
    assert "get_recent_logs" in html
    assert "open_log_folder" in html
    assert "duration_ms" in html


def test_runtime_icon_is_applied_to_cocoa_process() -> None:
    source = (Path(__file__).parents[1] / "anime_mpv" / "web_app.py").read_text(encoding="utf-8")
    assert "setApplicationIconImage_" in source
    assert "webview.start(on_started" in source
    assert 'assets" / "app-icon.png"' in source


def test_macos_runtime_identity_sets_process_name_in_source() -> None:
    source = Path(__file__).parents[1].joinpath("anime_mpv/web_app.py").read_text(encoding="utf-8")
    assert 'setProcessName_(APP_NAME)' in source
    assert 'setObject_forKey_(APP_NAME, "CFBundleDisplayName")' in source


def test_installer_sets_bundle_display_name() -> None:
    source = Path(__file__).parents[1].joinpath("install.sh").read_text(encoding="utf-8")
    assert "CFBundleDisplayName $APP_NAME" in source
    assert "CFBundleIdentifier $APP_BUNDLE_ID" in source
    assert "CFBundleExecutable $APP_NAME" in source


def test_play_uses_precomputed_subtitle_and_cached_media_hint(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.manager_models import LibraryAnime, LibraryEpisode

    api = make_api(tmp_path)
    video = tmp_path / "Otome Kaijuu Carameliser - 05.mkv"
    subtitle = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=204466,
            title="Otome Kaijuu Caraméliser",
            titles=["Otome Kaijuu Caraméliser", "KAIJU GIRL CARAMELISE"],
            synonyms=["Otome Kaijuu Carameliser"],
            episodes=12,
            format="TV",
        )
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=204466,
            title="Otome Kaijuu Caraméliser",
            episode=5,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )

    class FakeProcess:
        pid = 55

        def poll(self):
            return None

    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr("anime_mpv.web_app.subprocess.Popen", fake_popen)

    api.play(str(video))
    command = calls[0]
    assert "--fast-play" in command
    assert "--no-sync" in command
    assert "--fullscreen" in command
    assert command[command.index("--sub") + 1] == str(subtitle.resolve())
    assert command[command.index("--media-id") + 1] == "204466"
    assert command[command.index("--episode-hint") + 1] == "5"


def test_up_to_date_card_hides_missing_next_episode_message() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "!planned&&a.outdated?`<div class=\"subtle\">${t('label.localMissing'" in html


def test_marking_episode_watched_updates_cached_progress_and_list_status(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    video = (tmp_path / "episode-05.mkv").resolve()
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=501,
            title="Example",
            status="PLANNING",
            progress=4,
            episodes=12,
            next_airing_episode=6,
        )
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=501,
            title="Example",
            episode=5,
            video_path=video,
            state="ready",
        )
    )

    api.manager.db.schedule_cleanup(video, 24, list_status="CURRENT")

    anime = api.manager.db.get_anime(501)
    episode = api.manager.db.episode_by_path(video)
    assert anime is not None
    assert anime.progress == 5
    assert anime.status == "CURRENT"
    assert episode is not None
    assert episode.state == "watched"


def test_play_status_reports_watched_episode_and_cached_progress(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    video = (tmp_path / "episode-03.mkv").resolve()
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=700, title="Live Update", status="CURRENT", progress=2)
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=700,
            title="Live Update",
            episode=3,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.schedule_cleanup(video, 24, list_status="CURRENT")

    status = api.play_status(str(video))

    assert status["watched"] is True
    assert status["episode"] == 3
    assert status["anime_progress"] == 3
    assert status["list_status"] == "CURRENT"


def test_play_monitor_refreshes_cards_when_episode_is_counted() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "if(status.watched&&!watchedReported)" in html
    assert "ui.state=await pywebview.api.get_state();renderDataPages()" in html
    assert "status.episodeCounted" in html
    assert "Смотрю: серия {episode} засчитана" in html


def test_installer_builds_real_native_named_app_bundle() -> None:
    source = Path(__file__).parents[1].joinpath("install.sh").read_text(encoding="utf-8")
    assert '"$VENV_DIR/bin/python" -m PyInstaller' in source
    assert '--name "$APP_NAME"' in source
    assert 'CFBundleExecutable $APP_NAME' in source
    assert 'ANIME_MPV_PYTHON' in source
    assert "osacompile" not in source



def test_play_does_not_use_stale_subtitle_from_non_ready_episode(
    tmp_path: Path, monkeypatch
) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    video = tmp_path / "Mushoku Tensei III - 06.mkv"
    stale = tmp_path / "wrong-pack.srt"
    video.write_bytes(b"video")
    stale.write_text("subtitle", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178789,
            title="Mushoku Tensei III",
            episode=6,
            video_path=video.resolve(),
            subtitle_path=stale.resolve(),
            state="waiting_subtitles",
        )
    )

    class FakeProcess:
        pid = 56

        def poll(self):
            return None

    calls: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr("anime_mpv.web_app.subprocess.Popen", fake_popen)

    api.play(str(video))
    command = calls[0]
    assert "--sub" not in command


def test_home_state_groups_ready_downloaded_episodes(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=308, title="Odd Taxi", status="CURRENT", progress=1, episodes=13)
    )
    for episode, state in ((2, "ready"), (3, "ready"), (4, "waiting_subtitles"), (5, "watched")):
        video = tmp_path / f"Odd Taxi - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=308,
                title="Odd Taxi",
                episode=episode,
                video_path=video,
                state=state,
            )
        )

    downloaded = api.get_state()["downloaded"]

    assert len(downloaded) == 1
    assert downloaded[0]["title"] == "Odd Taxi"
    assert downloaded[0]["ready_episodes"] == [2, 3]
    assert downloaded[0]["ready_count"] == 2
    assert downloaded[0]["total_episodes"] == 13
    assert downloaded[0]["all_episodes_ready"] is False
    assert downloaded[0]["local"]["episode"] == 2


def test_current_page_uses_actionable_home_sections_without_all_anime() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "section.newReady':'Новые серии готовы" in html
    assert "section.completedReady':'Завершённые и готовые" in html
    assert "section.waitingPreparation':'Ждём подготовку" in html
    assert "section.caughtUp':'Всё просмотрено" in html
    assert "home.dropped||[]" in html
    assert "section.dropped" in html
    assert "section.allAnime" not in html
    assert "Все аниме" not in html

def test_home_state_marks_all_downloaded_episodes_ready(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=9001,
            title="Complete Show",
            status="CURRENT",
            progress=0,
            episodes=4,
        )
    )
    for episode in range(1, 5):
        video = tmp_path / f"Complete Show - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=9001,
                title="Complete Show",
                episode=episode,
                video_path=video,
                state="ready",
            )
        )

    downloaded = api.get_state()["downloaded"]

    assert downloaded[0]["ready_episodes"] == [1, 2, 3, 4]
    assert downloaded[0]["total_episodes"] == 4
    assert downloaded[0]["all_episodes_ready"] is True


def test_downloaded_episode_text_uses_ranges_and_all_ready_label() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "function formatEpisodeRanges(values)" in html
    assert "`${start}-${end}`" in html
    assert "ranges.join(',')" in html
    assert "if(a.all_episodes_ready)return `${t('label.readyAll')}${finalSuffix}`" in html
    assert "'label.readyAll':'Готово всё'" in html

def test_ready_home_cards_do_not_duplicate_title_on_hover() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    ready_template = html.split("function readyHomeCard(a){", 1)[1].split(
        "function waitingHomeCard", 1
    )[0]
    assert 'downloaded-card" title=' not in ready_template
    assert '<div class="hover-title">${escapeHtml(a.title)}</div>' not in ready_template
    assert '<strong data-full-title="${escapeHtml(a.title)}" title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong>' in ready_template



def test_home_sections_group_airing_finished_waiting_and_caught_up(tmp_path: Path) -> None:
    from datetime import date, timedelta

    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    today = date.today()
    anime_items = [
        LibraryAnime(
            media_id=1,
            title="Airing Ready",
            status="CURRENT",
            progress=1,
            episodes=12,
            media_status="RELEASING",
            next_airing_episode=3,
            next_airing_at=2_000_000_000,
        ),
        LibraryAnime(
            media_id=2,
            title="Recent Finale",
            status="CURRENT",
            progress=11,
            episodes=12,
            media_status="FINISHED",
            end_date=today.isoformat(),
        ),
        LibraryAnime(
            media_id=3,
            title="Old Finished",
            status="CURRENT",
            progress=3,
            episodes=12,
            media_status="FINISHED",
            end_date=(today - timedelta(days=30)).isoformat(),
        ),
        LibraryAnime(
            media_id=4,
            title="Airing Waiting",
            status="CURRENT",
            progress=2,
            episodes=12,
            media_status="RELEASING",
            next_airing_episode=5,
            next_airing_at=2_000_000_000,
        ),
        LibraryAnime(
            media_id=5,
            title="Caught Up",
            status="CURRENT",
            progress=4,
            episodes=12,
            media_status="RELEASING",
            next_airing_episode=5,
            next_airing_at=2_000_000_000,
        ),
    ]
    for anime in anime_items:
        api.manager.db.upsert_anime(anime)

    for media_id, title, episode in (
        (1, "Airing Ready", 2),
        (2, "Recent Finale", 12),
        (3, "Old Finished", 4),
    ):
        video = tmp_path / f"{title} - {episode}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=title,
                episode=episode,
                video_path=video,
                state="ready",
            )
        )

    waiting_video = tmp_path / "Airing Waiting - 3.mkv"
    waiting_video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=4,
            title="Airing Waiting",
            episode=3,
            video_path=waiting_video,
            state="waiting_subtitles",
        )
    )

    home = api.get_state()["home"]

    assert [item["title"] for item in home["new_ready"]] == [
        "Airing Ready",
        "Recent Finale",
    ]
    assert [item["title"] for item in home["completed_ready"]] == ["Old Finished"]
    assert [item["title"] for item in home["waiting"]] == ["Airing Waiting"]
    assert home["waiting"][0]["local"]["state"] == "waiting_subtitles"
    assert [item["title"] for item in home["caught_up"]] == ["Caught Up"]


def test_recent_finale_requires_only_last_episode_remaining(tmp_path: Path) -> None:
    from datetime import date

    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=88,
        title="Finished but far behind",
        status="CURRENT",
        progress=8,
        episodes=12,
        media_status="FINISHED",
        end_date=date.today().isoformat(),
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Finished but far behind - 09.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=88,
            title=anime.title,
            episode=9,
            video_path=video,
            state="ready",
        )
    )

    home = api.get_state()["home"]

    assert home["new_ready"] == []
    assert [item["title"] for item in home["completed_ready"]] == [anime.title]


def test_onboarding_can_be_completed_and_skipped(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.get_state()["settings"]["onboarding_completed"] is False

    completed = api.complete_onboarding(
        {
            "language": "ru",
            "jimaku_api_key": "jimaku",
            "anilist_enabled": False,
            "llm_enabled": False,
            "qbt_enabled": False,
        }
    )
    assert completed["settings"]["onboarding_completed"] is True
    assert completed["settings"]["language"] == "ru"
    assert completed["settings"]["jimaku_api_key"] == "jimaku"

    api.config.ui.onboarding_completed = False
    write_config(api.config, api.config_path)
    skipped = api.skip_onboarding()
    assert skipped["settings"]["onboarding_completed"] is True


def test_web_settings_prioritize_integrations_and_offer_rerunnable_guide() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "section.essential" in html
    assert "section.additional" in html
    assert "runSetupGuide" in html
    assert "onboardingSkip" in html
    assert "https://jimaku.cc/api/docs" in html
    assert "https://anilist.co/settings/developer" in html
    assert "if(ui.state.settings.onboarding_completed)void startupSequence();else showOnboarding(false);" in html


def test_final_pipeline_cache_schema_bumped_for_cold_open_fix() -> None:
    from anime_mpv import pipeline_cache

    assert pipeline_cache._CACHE_SCHEMA == "final-pipeline-v9"



def test_library_payload_groups_episodes_and_uses_cover(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    cover = api.config.library.cover_cache_dir / "123.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"image")
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=123,
            title="Grouped Anime",
            status="CURRENT",
            episodes=12,
            cover_url="https://example.test/cover.jpg",
        )
    )
    # cached_cover_path uses the media id extension chosen by the manager; patch it
    api.asset_base = "http://127.0.0.1:1234"
    api.manager.cached_cover_path = lambda anime: cover  # type: ignore[method-assign]
    for episode in (1, 2, 3):
        video = tmp_path / f"Grouped Anime - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(123, "Grouped Anime", episode, video, state="ready")
        )

    state = api.get_state()

    assert len(state["library"]) == 1
    group = state["library"][0]
    assert group["title"] == "Grouped Anime"
    assert group["episode_count"] == 3
    assert [item["episode"] for item in group["episodes"]] == [1, 2, 3]
    assert group["cover"].endswith("/covers/123.jpg")
    assert group["episodes"][0]["filename"] == "Grouped Anime - 01.mkv"


def test_library_page_is_removed_from_web_ui() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-page="library"' not in html
    assert '<section id="library"' not in html
    assert 'function renderLibrary()' not in html


def test_settings_are_flat_and_optional_fields_are_conditional() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'class="settings-flat"' in html
    assert 'id="settings-anilist-fields"' in html
    assert 'id="settings-llm-fields"' in html
    assert 'id="settings-qbt-fields"' in html
    assert "syncConditionalSettings()" in html
    assert '<details class="advanced-settings">' not in html


def test_onboarding_uses_left_aligned_toggles_and_recommends_anilist() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "Connect AniList (strongly advised)" in html
    assert 'class="onboarding-toggle"' in html
    assert 'id="o_anilist_fields"' in html


def test_web_ui_final_rating_context_menu_and_compact_navigation() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "label.final" in html
    assert "showScoreModal" in html
    assert "data-context-action=\"drop\"" in html
    assert "data-context-action=\"watching\"" in html
    assert 'data-page="downloads"' not in html
    assert 'id="settingsMaintenance"' in html
    assert "openDiagnosticsPage" in html
    assert 'data-page="diagnostics"' not in html
    assert html.count('id="refreshAll"') == 1
    assert 'id="pageRefresh"' not in html


def test_sidebar_status_is_hidden_and_refresh_button_shows_progress() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="status" class="status" hidden' in html
    assert "'action.refreshing':'Refreshing…'" in html
    assert "'action.refreshing':'Обновление…'" in html
    assert 'button.disabled=active' in html
    assert 'button.innerHTML=`<span class=\"spinner\" aria-hidden=\"true\"></span><span>${label}</span>`' in html
    assert 'button.title=t(statusKey)' in html
    assert "setLocalRefreshUi(true,'status.refreshing')" in html


def test_drop_group_contains_only_local_pending_files(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=77,
        title="Dropped Show",
        status="DROPPED",
        progress=3,
        episodes=12,
        site_url="https://anilist.co/anime/77",
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Dropped Show - 04.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=77, title=anime.title, episode=4, video_path=video, state="ready")
    )
    api.manager.db.schedule_anime_cleanup(77, 2)

    state = api.get_state()
    assert [item["title"] for item in state["home"]["dropped"]] == ["Dropped Show"]
    assert state["home"]["dropped"][0]["local_count"] == 1
    assert state["home"]["dropped"][0]["delete_remaining_seconds"] is not None


def test_play_status_requests_final_rating_once(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    anime = LibraryAnime(media_id=88, title="Final Show", status="COMPLETED", progress=12, episodes=12)
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Final Show - 12.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=88, title=anime.title, episode=12, video_path=video, state="watched")
    )

    first = api.play_status(str(video))
    assert first["final_episode"] is True
    assert first["rating_prompted"] is False
    api.skip_rating(88, 12)
    second = api.play_status(str(video))
    assert second["rating_prompted"] is True


def test_ready_sections_are_responsive_and_statuses_are_compact() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'class="ready-sections"' in html
    assert ".ready-sections { display:grid" in html
    assert "'label.waitingSubs':'Waiting for subs'" in html
    assert "'label.waitingSubs':'Ждём субтитры'" in html
    assert "white-space:nowrap; overflow:hidden; text-overflow:ellipsis" in html


def test_library_sort_controls_are_removed_with_library_page() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="librarySort"' not in html
    assert 'ui.librarySort' not in html


def test_escape_fullscreen_is_dispatched_to_cocoa_main_thread() -> None:
    source = (Path(__file__).parents[1] / "anime_mpv" / "web_app.py").read_text(encoding="utf-8")
    assert "AppHelper.callAfter(self._exit_fullscreen_on_main_thread)" in source
    assert "self._fullscreen_exit_pending" in source
    assert "native_window.toggleFullScreen_(None)" in source


def test_overflow_titles_use_custom_webview_tooltip() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="titleTooltip"' in html
    assert "function titleIsClipped(el)" in html
    assert "probe=el.cloneNode(true)" in html
    assert "function showTitleTooltip(el)" in html
    assert "document.addEventListener('mouseover'" in html
    assert "document.addEventListener('mouseout'" in html
    assert "el.dataset.titleClipped=clipped?'1':'0'" in html


def test_score_modal_uses_weighted_one_second_fortune_wheel() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "const SCORE_WHEEL_WEIGHTS=[1,2,4,7,10,10,7,4,2,1]" in html
    assert "function chooseWheelResult(previous=null)" in html
    assert "function spinScoreWheel(c)" in html
    assert 'data-action="reroll-score"' in html
    assert 'data-action="score-anime"' in html
    assert "setTimeout(()=>{" in html
    assert "},1000);" in html
    assert "Array.from({length:10}" not in html


def test_unreleased_planning_menu_hides_move_to_watching() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "const unreleased=String(a.media_status||'').trim().toUpperCase()==='NOT_YET_RELEASED'" in html
    assert "const watchingAction=unreleased?'':" in html


def test_external_subtitle_reveals_file_in_finder(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    subtitle = tmp_path / "prepared subtitles.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr("anime_mpv.web_app.subprocess.Popen", lambda command, *args, **kwargs: calls.append(command))

    result = api.reveal_subtitle_file(str(subtitle))

    assert result["ok"] is True
    assert calls == [["open", "-R", str(subtitle.resolve())]]


def test_library_external_subtitle_has_filename_tooltip_and_reveal_action() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-action="reveal-subtitle"' in html
    assert "e.subtitle_filename||revealPath" in html
    assert "pywebview.api.reveal_subtitle_file(target.dataset.path)" in html
    assert ".library-subtitle.external-link" in html


def test_score_wheel_has_no_extra_post_result_cooldown() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "rerollReadyAt" not in html
    assert "scheduleScoreRerollUnlock" not in html
    assert "c.generatedScore=result.score;c.lastScore=result.score;c.spinning=false" in html


def test_save_settings_persists_notifications_toggle(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    result = api.save_settings({"notifications_enabled": False})

    assert result["settings"]["notifications_enabled"] is False
    assert api.config.ui.notifications_enabled is False


def test_settings_ui_contains_notifications_toggle() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "s_notifications" in html
    assert "notifications_enabled:c('s_notifications')" in html


def test_home_moves_resumable_ready_anime_to_continue_watching(tmp_path: Path) -> None:
    from anime_mpv.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=551,
        title="One Card Only",
        status="CURRENT",
        progress=0,
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=2,
        next_airing_at=2_000_000_000,
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "One Card Only - 01.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=1,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.record_playback(video, 120, 1400)

    home = api.get_state()["home"]

    assert home["new_ready"] == []
    assert [item["media_id"] for item in home["continue_watching"]] == [anime.media_id]


def test_continue_watching_renders_before_ready_sections() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text()
    render_start = html.index("function renderCurrent()")
    render_end = html.index("function buildPlannedOnce()", render_start)
    render_current = html[render_start:render_end]

    assert render_current.index("section.continueWatching") < render_current.index("readySections?")
