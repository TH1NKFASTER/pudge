"""The window's backend (WebAppApi) and the anime pages of the window (behaviour; audit 71Б
rewrite: window checks run the real index.html in node, tests/page_harness.py fixture `page`)."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

from css_support import css_rules
from desktop_isolation import patch_webapp_popen

from pudge import web_app
from pudge.branding import APP_BUNDLE_ID, APP_NAME
from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi, _start_asset_server
from tests.i18n_support import catalog, en, has_text, ru


def _downloaded(api):
    """The Ready grouping behind Home (no longer sent to the window as "downloaded")."""
    return api._downloaded_payloads({anime.media_id: anime for anime in api.manager.db.anime_list()})


def _write_config(tmp_path: Path) -> Path:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return cfg.config_path


def make_api(tmp_path: Path) -> WebAppApi:
    return WebAppApi(_write_config(tmp_path))


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


def test_changed_anilist_credentials_refresh_automatically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = make_api(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        AnimeManager,
        "refresh_anilist_cache",
        lambda self: calls.append(self.config.anilist.access_token) or {"anime": 3, "covers": 3},
    )

    first = api.save_settings(
        {
            "anilist_enabled": True,
            "anilist_client_id": "12345",
            "anilist_token": "new-token",
        }
    )
    second = api.save_settings(
        {
            "anilist_enabled": True,
            "anilist_client_id": "12345",
            "anilist_token": "new-token",
        }
    )

    assert first["anilist_refresh_error"] == ""
    assert first["reconcile"]["anilist"] == 3
    assert second["reconcile"].get("anilist") is None
    assert calls == ["new-token"]


def test_asset_server_serves_web_ui(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    server, base_url = _start_asset_server(api)
    try:
        with urllib.request.urlopen(f"{base_url}/index.html", timeout=3) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    # The window page, with the app name and the session values filled in.
    assert 'id="plannedFilter"' in html
    assert "__APP_NAME__" not in html and "__PUDGE_HTTP_TOKEN__" not in html
    # Pages scroll with momentum.
    assert css_rules("index.html").get(".page-scroll", "-webkit-overflow-scrolling") == "touch"


# Planning: AniList's media_status comes as it is ("FINISHED", sometimes padded or lower case).
PLANNED = [
    {
        "media_id": 1,
        "title": "Finished Show",
        "media_status": " finished ",
        "episodes": 12,
        "progress": 0,
        "released_episodes": 12,
    },
    {
        "media_id": 2,
        "title": "Airing Behind",
        "media_status": "RELEASING",
        "episodes": 12,
        "progress": 0,
        "released_episodes": 4,
        "outdated": True,
        "next_episode": 1,
        "missing_count": 4,
    },
    {
        "media_id": 3,
        "title": "Announced",
        "media_status": "NOT_YET_RELEASED",
        "episodes": 12,
        "progress": 0,
        "released_episodes": 2,
        "next_airing_episode": 3,
        "remaining_seconds": 3600,
    },
]
PLANNING_PAGE = r"""
setPage('planned', true); await __tick(50);
const card = id => document.querySelector(`#plannedContent .anime-card[data-media-id="${id}"]`);
const shown = () => [...document.querySelectorAll('#plannedContent .anime-card')].filter(node => node.checkVisibility()).map(node => node.dataset.title);
const menu = async id => {
  await __fire(`#plannedContent .anime-card[data-media-id="${id}"] .anime-title`, 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent');
  await __tick(10);
  const actions = [...document.querySelectorAll('#contextMenu.open [data-context-action]')].map(node => node.dataset.contextAction);
  hideContextMenu();
  return actions;
};
const out = {
  all: shown(),
  text: Object.fromEntries([1, 2, 3].map(id => [id, card(id).innerText])),
  download: Object.fromEntries([1, 2, 3].map(id => [id, Boolean(card(id).querySelector('[data-action="planning-episodes-auto-card"]'))])),
  outdated: [1, 2, 3].filter(id => card(id).classList.contains('outdated')),
  menus: {released: await menu(1), announced: await menu(3)},
  texts: {watched: t('label.watched'), missing: t('label.localMissing', {episode: 1}), upToDate: t('label.upToDate')},
};
await __input('#plannedFilter', 'finished'); out.finished = shown();
await __input('#plannedFilter', 'unfinished'); out.unfinished = shown();
await __input('#plannedFilter', 'all');
await __fire('#plannedContent .anime-card[data-media-id="1"] .anime-title', 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent');
await __click('#contextMenu [data-context-action="watching"]'); await __tick(20);
return out;
"""


def test_planning_filters_and_unreleased_cards(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            PLANNING_PAGE,
            lang=lang,
            state={"planned": PLANNED},
            api={"move_planned_to_watching": {"ok": True, "download_pending": False}},
        )
        out, texts = result.value, result.value["texts"]
        assert out["all"] == ["Airing Behind", "Announced", "Finished Show"]
        # "Finished only" follows AniList's status whatever its spelling.
        assert out["finished"] == ["Finished Show"]
        assert out["unfinished"] == ["Airing Behind", "Announced"]
        # An announced anime has nothing to watch or download yet: no progress line, no
        # download button, no "move to Watching" / "download without watching".
        assert texts["watched"] in out["text"]["1"] and texts["watched"] not in out["text"]["3"]
        assert out["download"] == {"1": True, "2": True, "3": False}
        assert {"watching", "download-only"} <= set(out["menus"]["released"])
        assert not {"watching", "download-only"} & set(out["menus"]["announced"])
        # A Planning card is not a library card: no "next episode has no local file", no freshness line.
        assert out["outdated"] == [2]
        assert not any(texts["missing"] in text or texts["upToDate"] in text for text in out["text"].values())
        assert result.calls_of("move_planned_to_watching") == [[1]]
    assert css_rules("index.html").get(".anime-card.outdated .stripe", "background") == "var(--pink)"


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

    monkeypatch.setattr("pudge.web_app.QBittorrentClient", FakeClient)
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

    patch_webapp_popen(monkeypatch, fake_popen)

    first = api.play(str(video))
    second = api.play(str(video))

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["status"] == "starting"
    assert len(calls) == 1
    assert api.play_status(str(video))["status"] == "starting"


# A ready final episode on Home. The player starts, runs, counts the episode, then closes.
SHOW = {
    "media_id": 9,
    "title": "Show",
    "site_url": "https://anilist.co/anime/9",
    "ready_episodes": [12],
    "is_final_episode": True,
    "local": {"video_path": "/v/Show - 12.mkv", "episode": 12, "state": "ready"},
}
PLAYER = r"""
const base = __api.get_state;
window.__counted = false;
__api.get_state = __api.get_state_fast = () => {
  const state = base();
  if (window.__counted) state.home = {...state.home, new_ready: [], caught_up: [{...SHOW, progress: 12}]};
  return state;
};
const counted = {status: 'running', watched: true, episode: 12, final_episode: true, rating_prompted: false, media_id: 9, title: 'Show'};
const answers = [{status: 'starting'}, {status: 'running'}, counted, {...counted, status: 'exited'}];
let polls = 0;
__api.play_status = async () => { const answer = answers[Math.min(polls++, answers.length - 1)]; if (answer.watched) window.__counted = true; return answer; };
""".replace("SHOW", json.dumps(SHOW))


def test_playing_shows_progress_counts_the_episode_and_asks_for_the_final_rating(page) -> None:
    assert ru("action.starting") == "Запускаю…"
    assert ru("status.episodeCounted", episode=5) == "Серия 5 засчитана"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const card = () => document.querySelector('#currentContent [data-path="/v/Show - 12.mkv"]');
            const look = () => ({starting: card()?.classList.contains('play-starting') ?? null, running: card()?.classList.contains('play-running') ?? null,
                                 label: card()?.dataset.startingLabel ?? null});
            await __click('#currentContent .downloaded-card');
            await __tick(10);
            const out = {clicked: look()};
            await __tick(900); out.starting = look();
            await __tick(900); out.running = look();
            await __tick(2000);
            out.counted = {status: __text('#status'), home: __text('#currentContent'), modal: __visible('#modalBackdrop')};
            await __tick(2000);
            out.closed = {modal: __visible('#modalBackdrop'), title: __text('#modalTitle')};
            out.texts = {starting: t('action.starting'), counted: t('status.episodeCounted', {episode: 12}), caughtUp: t('section.caughtUp'),
                         rate: t('modal.rateFinal', {title: 'Show'})};
            return out;
            """,
            lang=lang,
            state={"home": {"new_ready": [SHOW]}},
            api={"play": {"ok": True}, "final_sequel_option": {"ok": True, "sequel": None}},
            before=PLAYER,
        )
        out, texts = result.value, result.value["texts"]
        assert result.calls_of("play") == [["/v/Show - 12.mkv", False, False, False]]
        # The card says "Starting…" over the cover until the player runs.
        assert out["clicked"] == {"starting": True, "running": False, "label": texts["starting"]}
        assert out["starting"]["starting"] is True
        assert out["running"]["running"] is True and out["running"]["starting"] is False
        # Once the episode counts, Home is reloaded at once (the show moved to Caught up) and the status says so.
        assert out["counted"]["status"] == texts["counted"]
        assert texts["caughtUp"] in out["counted"]["home"] and out["counted"]["modal"] is False
        # The final episode's rating opens when the player closes.
        assert out["closed"] == {"modal": True, "title": texts["rate"]}
        assert len(result.calls_of("play_status")) == 4
    assert (
        css_rules("index.html").get(".airing-card.play-starting::after", "content")
        == "attr(data-starting-label)"
    )


# The proxy fields of Settings: only Nyaa searches use them; one button fills in a local SOCKS proxy.
NYAA_SETTINGS = r"""
setPage('settings'); await __tick(20);
const block = $('presetNyaaSocks').closest('.setting-block');
const out = {hint: block.innerText.includes(t('help.nyaa'))};
await __click('#presetNyaaSocks'); await __tick(10);
out.fields = {mode: $('s_proxy_mode').value, url: $('s_proxy_url').value};
out.toast = __toasts().at(-1); out.expected = t('toast.socksPreset');
await __input('#s_notifications', false);
await __click('#saveSettings'); await __tick(50);
return out;
"""


def test_settings_offer_a_local_socks_proxy_for_nyaa_and_save_notifications(page) -> None:
    # The hint is the effective text of the old duplicated 'help.nyaa' entry.
    assert en("help.nyaa") == "Only Nyaa searches use the proxy selected here."
    assert "только для поиска на Nyaa" in ru("help.nyaa")
    for lang in ("en", "ru"):
        result = page.run(
            NYAA_SETTINGS,
            lang=lang,
            state={"settings": {"nyaa_enabled": True, "proxy_mode": "direct", "proxy_url": ""}},
            api={"save_settings": {"ok": True}},
        )
        out = result.value
        assert out["hint"] is True
        assert out["fields"] == {"mode": "proxy_only", "url": "socks5://[::1]:1080"}
        assert out["toast"] == out["expected"]
        [[saved]] = result.calls_of("save_settings")
        assert (saved["proxy_mode"], saved["proxy_url"], saved["nyaa_enabled"]) == (
            "proxy_only",
            "socks5://[::1]:1080",
            True,
        )
        assert saved["notifications_enabled"] is False


def test_web_state_reports_ready_without_sidecar_as_embedded(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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

    # Episodes reach the window once, inside the library groups.
    episode = api.get_state()["library"][0]["episodes"][0]
    assert episode["subtitle_source"] == "embedded"


def test_refresh_local_does_not_sync_anilist(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls = {"local": 0, "anilist": 0}

    def local_refresh(**_kwargs):
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


# Refresh can run again after each refresh, and a queued priority subtitle check shows
# "checking subtitles in the background": test_v0666_single_refresh_subtitle_queue.py
# (test_ui_keeps_background_subtitle_status_without_locking_refresh).
# The one-shot foil animation of watchable covers: test_v0655_requested_features.py.


LANGUAGE = r"""
const out = {lang: document.documentElement.lang, nav: __text('.nav [data-page="current"]'),
             logo: {src: document.querySelector('.sidebar .logo img')?.getAttribute('src'), shown: __visible('.sidebar .logo img')}};
if (SWITCH) {
  setPage('settings'); await __tick(20);
  out.languages = [...$('s_language').options].map(option => [option.value, option.textContent]);
  await __input('#s_language', 'ru');
  await __click('#saveSettings'); await __tick(50);
  out.saved = {lang: document.documentElement.lang, nav: __text('.nav [data-page="current"]')};
}
return out;
"""


def test_window_starts_in_english_and_switches_to_russian(page) -> None:
    assert en("nav.current") == "Anime"
    assert ru("nav.current") == "Аниме"
    root = Path(__file__).parents[1] / "pudge"
    assert (root / "assets" / "app-icon.png").is_file() and (root / "web" / "app-logo.png").is_file()
    # Before the backend answers, the window is English, with the Pudge logo.
    before = page.run(LANGUAGE.replace("SWITCH", "false"), ready=False).value
    assert before == {"lang": "en", "nav": "Anime", "logo": {"src": "app-logo.png", "shown": True}}
    result = page.run(
        LANGUAGE.replace("SWITCH", "true"),
        lang="en",
        before="__api.save_settings = async values => ({settings: {...__api.get_state().settings, language: values.language}});",
    )
    out = result.value
    assert out["languages"] == [["en", "English"], ["ru", "Русский"]]
    assert result.calls_of("save_settings")[0][0]["language"] == "ru"
    assert out["saved"] == {"lang": "ru", "nav": "Аниме"}
    assert page.run(LANGUAGE.replace("SWITCH", "false"), lang="ru").value["nav"] == "Аниме"


def test_foil_covers_shine_stronger() -> None:
    rules = css_rules("index.html")
    assert rules.get(".cover-shell.polychrome::before", "opacity") == ".9"
    assert "0 0 34px rgba(255,84,208,.24)" in rules.get(".cover-shell.polychrome", "box-shadow")


# Energy-efficient polling (15 s in front, 2 min behind, adaptive while downloading, no fast
# polling for subtitle jobs that are not due): test_v0560_inactive_window_energy.py,
# test_v0643_adaptive_download_polling.py.


def test_save_settings_persists_selected_language(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    result = api.save_settings({"language": "ru"})
    assert result["settings"]["language"] == "ru"
    assert api.config.ui.language == "ru"


def test_startup_maintenance_runs_once_and_refreshes_new_torrents(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.qbittorrent.enabled = True
    api.config.nyaa.torrents_enabled = True
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


def test_startup_maintenance_keeps_download_sync_idle_when_torrents_off(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.qbittorrent.enabled = True
    assert api.config.nyaa.torrents_enabled is False
    calls = {"sync": 0}
    monkeypatch.setattr(api.manager, "run_startup_once", lambda: {"auto": 1})

    def sync_downloads():
        calls["sync"] += 1
        return 1

    monkeypatch.setattr(api.manager, "sync_downloads", sync_downloads)

    api.startup_maintenance()
    api._startup_maintenance_thread.join(timeout=3)
    status = api.startup_maintenance_status()

    assert status["done"] is True
    assert "downloads_after_auto" not in status["stats"]
    assert calls["sync"] == 0


def test_foreground_poll_checks_downloads_without_running_heavy_subtitles(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    calls = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: calls.append("downloads") or 2)
    monkeypatch.setattr(
        api.manager,
        "process_subtitle_jobs",
        lambda limit=4: (_ for _ in ()).throw(AssertionError("foreground must not run subtitle jobs")),
    )

    result = api._poll_downloads_and_subtitles_sync()

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


# The AniList sync answers when the test says so; the background maintenance status fails
# once (busy), then runs, then finishes.
STARTUP = """
__api.startup_sync_anilist = () => new Promise(resolve => { window.__finishSync = () => resolve({skipped: true}); });
__api.startup_maintenance = async () => ({running: true, state: __api.get_state()});
let checks = 0;
__api.startup_maintenance_status = async () => {
  if (++checks === 1) throw new Error('busy');
  return {running: checks < 3, error: '', state: __api.get_state()};
};
"""
STARTUP_CALLS = (
    "startup_sync_anilist",
    "startup_maintenance",
    "startup_maintenance_status",
    "poll_downloads_and_subtitles",
)


def test_startup_syncs_anilist_before_the_background_maintenance(page) -> None:
    result = page.run(
        """
        await __tick(2000);                      // AniList is slow today
        const waiting = __apiCalls.filter(call => call.name === 'startup_maintenance').length;
        window.__finishSync();
        await __tick(10000);
        return {waiting, calls: __apiCalls.filter(call => NAMES.includes(call.name)).map(call => [call.name, call.at])};
        """.replace("NAMES", json.dumps(STARTUP_CALLS)),
        before=STARTUP,
    )
    assert result.value["waiting"] == 0
    assert result.value["calls"] == [
        ["startup_sync_anilist", 0],
        ["startup_maintenance", 2000],  # only after the sync finished
        ["startup_maintenance_status", 2500],  # fails ...
        ["poll_downloads_and_subtitles", 3000],  # the foreground work starts a second after the maintenance
        ["startup_maintenance_status", 5500],  # ... so the next check waits 3 s
        ["startup_maintenance_status", 6500],
    ]


def test_settings_open_the_diagnostics_page_with_recent_logs(page) -> None:
    assert "duration_ms" in en("help.diagnostics") and "duration_ms" in ru("help.diagnostics")
    lines = ["12:00:01 START step=nyaa.search", "12:00:02 DONE step=nyaa.search duration_ms=812"]
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            const out = {inMaintenance: Boolean(document.querySelector('#settingsMaintenance #openDiagnosticsPage')),
                         nav: [...document.querySelectorAll('.nav [data-page]')].map(button => button.dataset.page)};
            await __click('#openDiagnosticsPage'); await __tick(20);
            Object.assign(out, {page: ui.page, shown: __visible('#diagnostics'), settings: __visible('#settings'),
                                help: __text('#diagnostics p.subtle'), path: __text('#logPath'), log: $('logOutput').textContent,
                                expectedHelp: t('help.diagnostics')});
            await __click('#refreshLogs'); await __tick(20);
            await __click('#openLogs'); await __tick(20);
            return out;
            """,
            lang=lang,
            api={
                "get_recent_logs": {"path": "/Users/me/Library/Logs/pudge/pudge.log", "lines": lines},
                "open_log_folder": {"ok": True},
            },
        )
        out = result.value
        # Reached from Settings → Maintenance only; the sidebar has no Diagnostics page.
        assert out["inMaintenance"] is True and "diagnostics" not in out["nav"]
        assert (out["page"], out["shown"], out["settings"]) == ("diagnostics", True, False)
        assert out["help"] == out["expectedHelp"]
        assert out["path"] == "/Users/me/Library/Logs/pudge/pudge.log"
        assert out["log"] == "\n".join(lines)
        assert result.calls_of("get_recent_logs") == [[400], [400]]
        assert result.calls_of("open_log_folder") == [[]]


class _Cocoa:
    """Stand-ins for AppKit / Foundation / PyObjCTools that record what Pudge asks Cocoa to do."""

    def __init__(self, monkeypatch) -> None:
        self.events: list[tuple] = []
        self.info: dict[str, str] = {}
        self.main_thread_queue: list = []
        self.fullscreen = True
        self.window = SimpleNamespace(
            styleMask=lambda: (1 << 14) if self.fullscreen else 0,
            toggleFullScreen_=lambda sender: self.events.append(("toggle fullscreen", _on_main_thread())),
        )
        application = SimpleNamespace(
            setActivationPolicy_=lambda policy: self.events.append(("activation policy", policy)),
            setApplicationIconImage_=lambda image: self.events.append(
                ("icon", image.path, _on_main_thread())
            ),
            keyWindow=lambda: self.window,
            mainWindow=lambda: None,
        )
        appkit = ModuleType("AppKit")
        appkit.NSApplicationActivationPolicyRegular = 0
        appkit.NSWindowStyleMaskFullScreen = 1 << 14
        appkit.NSApplication = SimpleNamespace(
            sharedApplication=lambda: self.events.append(("NSApplication",)) or application
        )
        appkit.NSImage = SimpleNamespace(
            alloc=lambda: SimpleNamespace(initWithContentsOfFile_=lambda path: SimpleNamespace(path=path))
        )
        foundation = ModuleType("Foundation")
        foundation.NSProcessInfo = SimpleNamespace(
            processInfo=lambda: SimpleNamespace(
                setProcessName_=lambda name: self.events.append(("process name", name))
            )
        )
        foundation.NSBundle = SimpleNamespace(
            mainBundle=lambda: SimpleNamespace(
                infoDictionary=lambda: SimpleNamespace(
                    setObject_forKey_=lambda value, key: self.info.__setitem__(key, value)
                )
            )
        )
        pyobjctools = ModuleType("PyObjCTools")
        pyobjctools.AppHelper = SimpleNamespace(
            callAfter=lambda function, *args: self.main_thread_queue.append((function, args))
        )
        for name, module in (("AppKit", appkit), ("Foundation", foundation), ("PyObjCTools", pyobjctools)):
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(sys, "platform", "darwin")

    def run_main_thread_queue(self) -> None:
        queue, self.main_thread_queue = self.main_thread_queue, []
        for function, args in queue:
            function(*args)


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _in_worker_thread(function):
    """Call `function` the way pywebview calls the API: from a worker thread."""
    result: list = []
    worker = threading.Thread(target=lambda: result.append(function()))
    worker.start()
    worker.join(5)
    return result[0]


RUNTIME_ICON = Path(web_app.__file__).resolve().parent / "assets" / "app-icon.png"


def test_macos_runtime_identity_names_the_process_and_sets_the_icon(monkeypatch) -> None:
    # Homebrew's interpreter would show up as "Python" with a rocket icon in the Dock and Cmd+Tab.
    cocoa = _Cocoa(monkeypatch)
    assert RUNTIME_ICON.is_file()
    assert web_app._set_macos_runtime_identity() is True
    assert cocoa.events == [
        ("process name", APP_NAME),
        ("NSApplication",),
        ("activation policy", 0),
        ("icon", str(RUNTIME_ICON), True),
    ]
    assert cocoa.info == {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
    }
    # From another thread, the icon is handed to Cocoa's main thread.
    cocoa.events.clear()
    assert _in_worker_thread(web_app._set_macos_runtime_identity) is True
    assert ("icon", str(RUNTIME_ICON), False) not in cocoa.events and len(cocoa.main_thread_queue) == 1
    cocoa.run_main_thread_queue()
    assert cocoa.events[-1] == ("icon", str(RUNTIME_ICON), True)


class _Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def test_launch_starts_pywebview_with_the_pudge_icon_and_renames_after_start(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge import logging_utils, single_instance

    events: list = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(web_app, "_INSTANCE_LOCK", None)
    monkeypatch.setattr(logging_utils, "set_log_rotation_owner", lambda owner: None)
    monkeypatch.setattr(web_app, "_set_macos_runtime_identity", lambda: events.append("identity") or True)
    monkeypatch.setattr(
        web_app, "request_notification_permission", lambda timeout: {"supported": False, "granted": False}
    )
    monkeypatch.setattr(
        single_instance, "acquire", lambda: SimpleNamespace(serve_focus_requests=lambda callback: None)
    )
    monkeypatch.setattr(
        web_app,
        "_start_asset_server",
        lambda api: (SimpleNamespace(shutdown=lambda: None, server_close=lambda: None), "http://127.0.0.1:9"),
    )
    monkeypatch.setattr(web_app, "_install_macos_app_delegate_proxy", lambda api, lifecycle: True)

    def create_window(title, **kwargs):
        events.append("window")
        return SimpleNamespace(
            uid="main",
            native=None,
            events=SimpleNamespace(closing=_Event(), shown=_Event(), loaded=_Event(), before_show=_Event()),
        )

    def start(on_started, **kwargs):
        events.append(("start", kwargs))
        on_started()  # pywebview has created NSApplication

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    assert web_app.launch_web_app(_write_config(tmp_path)) == 0
    # Named before Cocoa starts, again once the API exists, and again after pywebview's start.
    assert events == [
        "identity",
        "identity",
        "window",
        ("start", {"gui": "cocoa", "debug": False, "icon": str(RUNTIME_ICON)}),
        "identity",
    ]


def test_escape_leaves_fullscreen_on_the_cocoa_main_thread(monkeypatch) -> None:
    # AppKit's toggleFullScreen_ from pywebview's worker thread crashed macOS
    # ("Must only be used from the main thread").
    cocoa = _Cocoa(monkeypatch)
    timers: list = []

    class Timer:
        def __init__(self, delay, function):
            self.delay, self.function, self.daemon = delay, function, False
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(web_app.threading, "Timer", Timer)
    api = WebAppApi.__new__(WebAppApi)
    api._fullscreen_exit_lock = threading.Lock()
    api._fullscreen_exit_pending = False
    api.logger = SimpleNamespace(exception=lambda *args, **kwargs: None)

    assert _in_worker_thread(api.exit_fullscreen) == {"ok": True, "exited": True, "scheduled": True}
    # A second Escape during the transition does not start another one.
    assert _in_worker_thread(api.exit_fullscreen)["pending"] is True
    assert cocoa.events == [] and len(cocoa.main_thread_queue) == 1
    cocoa.run_main_thread_queue()
    assert cocoa.events == [("NSApplication",), ("toggle fullscreen", True)]
    # The guard is released 1.5 s later; a window that is no longer fullscreen is left alone.
    assert timers[-1].delay == 1.5
    timers[-1].function()
    cocoa.events.clear()
    cocoa.fullscreen = False
    assert _in_worker_thread(api.exit_fullscreen)["scheduled"] is True
    cocoa.run_main_thread_queue()
    assert ("toggle fullscreen", True) not in cocoa.events


def test_play_uses_precomputed_subtitle_and_cached_media_hint(tmp_path: Path, monkeypatch) -> None:
    from pudge.manager_models import LibraryAnime, LibraryEpisode

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

    patch_webapp_popen(monkeypatch, fake_popen)

    api.play(str(video))
    command = calls[0]
    assert "--fast-play" in command
    assert "--no-sync" in command
    assert "--fullscreen" in command
    assert command[command.index("--sub") + 1] == str(subtitle.resolve())
    assert command[command.index("--media-id") + 1] == "204466"
    assert command[command.index("--episode-hint") + 1] == "5"


def test_marking_episode_watched_updates_cached_progress_and_list_status(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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
    from pudge.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    video = (tmp_path / "episode-03.mkv").resolve()
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(LibraryAnime(media_id=700, title="Live Update", status="CURRENT", progress=2))
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


def test_installed_app_bundle_is_named_pudge_and_tells_pudge_its_python() -> None:
    # install.sh runs with stand-ins for the macOS tools (tests/t71g01_support.py). The launcher
    # it builds with clang embeds Python (Py_InitializeFromConfig / Py_RunMain, run_module
    # pudge.app_entry, no exec of python, no PyInstaller/osacompile app):
    # test_v074_thin_launcher_runtime_paths.py, test_v0731_ui_cache_native_identity.py.
    from t71g01_support import launcher_main, plist, release_history

    sim = release_history().sim
    info = plist(sim.app / "Contents/Info.plist")
    assert (
        info["CFBundleName"],
        info["CFBundleDisplayName"],
        info["CFBundleExecutable"],
        info["CFBundleIdentifier"],
    ) == (APP_NAME, APP_NAME, APP_NAME, APP_BUNDLE_ID)
    assert (sim.app / "Contents/MacOS" / APP_NAME).is_file()
    # Pudge runs inside the app executable, so helpers it starts need the managed venv's Python.
    main = launcher_main(sim)
    assert f'const char *python = "{sim.venv}/bin/python";' in main
    assert 'setenv("PUDGE_PYTHON", python, 1);' in main


def test_play_does_not_use_stale_subtitle_from_non_ready_episode(tmp_path: Path, monkeypatch) -> None:
    from pudge.manager_models import LibraryEpisode

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

    patch_webapp_popen(monkeypatch, fake_popen)

    api.play(str(video))
    command = calls[0]
    assert "--sub" not in command


def test_home_state_groups_ready_downloaded_episodes(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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

    downloaded = _downloaded(api)

    assert len(downloaded) == 1
    assert downloaded[0]["title"] == "Odd Taxi"
    assert downloaded[0]["ready_episodes"] == [2, 3]
    assert downloaded[0]["ready_count"] == 2
    assert downloaded[0]["total_episodes"] == 13
    assert downloaded[0]["all_episodes_ready"] is False
    assert downloaded[0]["local"]["episode"] == 2


def test_anilist_progress_hides_late_download_from_ready_and_continue(
    tmp_path: Path,
) -> None:
    from pudge.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=200637,
            title="Hyakkano 3",
            status="CURRENT",
            progress=6,
            episodes=12,
            format="TV",
            media_status="RELEASING",
            next_airing_episode=8,
        )
    )
    videos: dict[int, Path] = {}
    for episode in (6, 7):
        video = (tmp_path / f"Hyakkano 3 - {episode:02d}.mkv").resolve()
        video.write_bytes(b"video")
        videos[episode] = video
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=200637,
                title="Hyakkano 3",
                episode=episode,
                video_path=video,
                state="ready",
                torrent_hash=f"hash-{episode}",
            )
        )
    api.manager.db.record_playback(videos[6], 600, 1_400, 60)

    state = api.get_state()
    downloaded = next(item for item in _downloaded(api) if item["media_id"] == 200637)
    library = next(item for item in state["library"] if item["media_id"] == 200637)
    library_states = {item["episode"]: item["state"] for item in library["episodes"]}

    assert downloaded["ready_episodes"] == [7]
    assert all(item["episode"] != 6 for item in state["home"]["continue_watching"])
    assert library_states == {6: "watched", 7: "ready"}
    assert library["ready_count"] == 1
    assert library["watched_count"] == 1
    assert [item["episode"] for item in api._ready_queue_items([200637])] == [7]

    persisted = api.manager.db.episode_by_path(videos[6])
    assert persisted is not None
    assert persisted.state == "ready"
    assert persisted.watched_at is None
    assert persisted.delete_after is None

    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=200637,
            title="Hyakkano 3",
            status="CURRENT",
            progress=5,
            episodes=12,
            format="TV",
            media_status="RELEASING",
            next_airing_episode=8,
        )
    )
    rolled_back = api.get_state()
    downloaded_after_rollback = next(item for item in _downloaded(api) if item["media_id"] == 200637)
    assert downloaded_after_rollback["ready_episodes"] == [6, 7]
    assert any(item["episode"] == 6 for item in rolled_back["home"]["continue_watching"])


def _home_library(tmp_path: Path) -> dict:
    """get_state() of a library with something in every Home section (the real payload)."""
    api = make_api(tmp_path)
    db = api.manager.db
    later = 2_000_000_000

    def anime(media_id: int, title: str, **fields) -> None:
        fields = {"status": "CURRENT", "episodes": 12, **fields}
        db.upsert_anime(LibraryAnime(media_id=media_id, title=title, **fields))

    def episode(media_id: int, title: str, number: int, state: str = "ready") -> Path:
        video = tmp_path / f"{title} - {number:02d}.mkv"
        video.write_bytes(b"video")
        db.upsert_episode(
            LibraryEpisode(media_id=media_id, title=title, episode=number, video_path=video, state=state)
        )
        return video

    anime(
        1, "Airing Ranges", progress=1, media_status="RELEASING", next_airing_episode=7, next_airing_at=later
    )
    for number in (2, 3, 5):
        episode(1, "Airing Ranges", number)
    anime(
        2,
        "Complete Show",
        progress=0,
        episodes=4,
        media_status="FINISHED",
        end_date=(date.today() - timedelta(days=60)).isoformat(),
    )
    for number in (1, 2, 3, 4):
        episode(2, "Complete Show", number)
    anime(3, "Finale", progress=11, media_status="FINISHED", end_date=date.today().isoformat())
    episode(3, "Finale", 12)
    anime(
        4, "Airing Waiting", progress=2, media_status="RELEASING", next_airing_episode=5, next_airing_at=later
    )
    episode(4, "Airing Waiting", 3, "waiting_subtitles")
    anime(5, "Caught Up", progress=4, media_status="RELEASING", next_airing_episode=5, next_airing_at=later)
    anime(6, "Resumed", progress=0, media_status="RELEASING", next_airing_episode=3, next_airing_at=later)
    db.record_playback(episode(6, "Resumed", 1), 120, 1400)
    anime(7, "Dropped Show", status="DROPPED", progress=3)
    episode(7, "Dropped Show", 4)
    db.schedule_anime_cleanup(7, 2)
    return json.loads(json.dumps(api.get_state(), default=str))


HOME = r"""
setPage('current', true); renderCurrent();
await __tick(600);                                  // the overflow-title pass runs at 120 and 500 ms
const root = $('currentContent');
const card = title => root.querySelector(`article[data-title="${title}"]`);
const status = title => card(title).querySelector('.airing-state').innerText;
return {
  order: [...root.children].map(node => node.dataset.homeSection || node.dataset.homeGroup),
  readyGroup: [...root.querySelectorAll('[data-home-group="ready"] > [data-home-section]')].map(node => node.dataset.homeSection),
  headings: [...root.querySelectorAll('[data-home-section] > h2')].map(node => node.innerText),
  cards: Object.fromEntries([...root.querySelectorAll('[data-home-section]')].map(section =>
    [section.dataset.homeSection, [...section.querySelectorAll('article')].map(node => node.dataset.title)])),
  status: {ranges: status('Airing Ranges'), all: status('Complete Show'), finale: status('Finale'), waiting: status('Airing Waiting')},
  foil: Object.fromEntries([...root.querySelectorAll('article')].map(node => [node.dataset.title, Boolean(node.querySelector('.cover-shell.polychrome'))])),
  readyTitles: [...root.querySelectorAll('.downloaded-card')].map(node => ({
    card: node.getAttribute('title'), hover: node.querySelectorAll('.hover-title').length,
    full: node.querySelector('strong').dataset.fullTitle, native: node.querySelector('strong').getAttribute('title'), title: node.dataset.title})),
  page: __text('#current'),
  expected: {
    headings: ['section.continueWatching', 'section.newReady', 'section.completedReady', 'section.waitingPreparation',
               'section.caughtUp', 'section.deletingSoon'].map(key => t(key)),
    ranges: t('label.readyEpisodes', {episodes: '2-3,5'}), all: t('label.readyAll'),
    finale: `${t('label.episode')} 12 • ${t('label.final')}`, waiting: t('label.waitingSubs'),
  },
};
"""


def test_home_shows_actionable_sections_with_compact_ready_cards(tmp_path: Path, page) -> None:
    for key, text in (
        ("section.newReady", "Новые серии готовы"),
        ("section.completedReady", "Завершённые и готовые"),
        ("section.waitingPreparation", "Ждём подготовку"),
        ("section.caughtUp", "Всё просмотрено"),
    ):
        assert ru(key) == text
    assert ru("label.readyAll") == "Готово всё"
    assert en("label.waitingSubs") == "Waiting for subs" and ru("label.waitingSubs") == "Ждём субтитры"
    # The old "All anime" list is gone from Home and from the catalogs.
    assert "section.allAnime" not in catalog("en") and not has_text("Все аниме", "ru")
    state = _home_library(tmp_path)
    for lang in ("en", "ru"):
        out = page.run(
            HOME, lang=lang, state={**state, "settings": {**state["settings"], "language": lang}}
        ).value
        # Continue watching first, the two ready sections side by side in one group, then the rest.
        assert out["order"] == [
            "section.continueWatching",
            "ready",
            "section.waitingPreparation",
            "section.caughtUp",
            "section.deletingSoon",
        ]
        assert out["readyGroup"] == ["section.newReady", "section.completedReady"]
        assert out["headings"] == out["expected"]["headings"]
        assert out["cards"] == {
            "section.continueWatching": ["Resumed"],
            "section.newReady": ["Airing Ranges", "Finale"],
            "section.completedReady": ["Complete Show"],
            "section.waitingPreparation": ["Airing Waiting"],
            "section.caughtUp": ["Caught Up"],
            "section.deletingSoon": ["Dropped Show"],
        }
        # Ready episodes as ranges, "all ready" for a complete season, the final episode marked.
        expected = out["expected"]
        assert out["status"] == {
            "ranges": expected["ranges"],
            "all": expected["all"],
            "finale": expected["finale"],
            "waiting": expected["waiting"],
        }
        # Only what can be watched now has the foil cover.
        assert out["foil"] == {
            "Resumed": True,
            "Airing Ranges": True,
            "Finale": True,
            "Complete Show": True,
            "Airing Waiting": False,
            "Caught Up": False,
            "Dropped Show": False,
        }
        # One title, no second hover copy: no native tooltip on the card or its title, no hover-title overlay.
        for ready in out["readyTitles"]:
            assert ready == {
                "card": None,
                "hover": 0,
                "full": ready["title"],
                "native": None,
                "title": ready["title"],
            }
        assert "Все аниме" not in out["page"]
    # Two ready columns when the window is wide, one when narrow; one-line statuses.
    rules = css_rules("index.html")
    assert rules.get(".ready-sections", "display") == "grid"
    assert (
        rules.get(".ready-sections", "grid-template-columns")
        == "repeat(auto-fit,minmax(min(560px,100%),1fr))"
    )
    status_line = rules.declarations(".airing-state")
    assert (status_line["white-space"], status_line["overflow"], status_line["text-overflow"]) == (
        "nowrap",
        "hidden",
        "ellipsis",
    )
    # The foil is on the cover only, not on the whole card.
    assert not [selector for selector in rules.selectors() if ".watchable" in selector]


def test_home_state_marks_all_downloaded_episodes_ready(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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

    downloaded = _downloaded(api)

    assert downloaded[0]["ready_episodes"] == [1, 2, 3, 4]
    assert downloaded[0]["total_episodes"] == 4
    assert downloaded[0]["all_episodes_ready"] is True


def test_home_sections_group_airing_finished_waiting_and_caught_up(tmp_path: Path) -> None:
    from datetime import date, timedelta

    from pudge.manager_models import LibraryEpisode

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

    from pudge.manager_models import LibraryEpisode

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
    assert completed["settings"]["jimaku_api_key"] == "••••••••"

    api.config.ui.onboarding_completed = False
    write_config(api.config, api.config_path)
    skipped = api.skip_onboarding()
    assert skipped["settings"]["onboarding_completed"] is True


FIRST_START = {
    "state": {"settings": {"onboarding_completed": False}},
    "api": {
        "first_experience_dependencies": {"mpv": {"installed": True}, "ffmpeg": {"installed": True}},
        "open_url": {"ok": True},
    },
    "before": "__api.skip_onboarding = async () => ({settings: {...__api.get_state().settings, onboarding_completed: true}});",
}


def test_first_start_opens_the_setup_guide_which_recommends_anilist_and_can_be_skipped(page) -> None:
    assert en("section.essential") == "Essential setup" and ru("section.essential") == "Основная настройка"
    assert (
        en("section.additional") == "Additional settings"
        and ru("section.additional") == "Дополнительные настройки"
    )
    assert en("onboarding.anilistTitle") == "Connect AniList (strongly advised)"
    for lang in ("en", "ru"):
        result = page.run(
            """
            await __tick(50);
            const out = {open: $('onboardingBackdrop').classList.contains('open'),
                         started: __apiCalls.filter(call => ['startup_sync_anilist', 'startup_maintenance'].includes(call.name)).length};
            while (ui.onboardingStep < 3) { await __click('#onboardingNext'); await __tick(20); }
            out.heading = __text('#onboardingBody h3'); out.expected = t('onboarding.anilistTitle');
            out.toggle = $('o_anilist_enabled').closest('label').matches('.onboarding-toggle');
            await __input('#o_anilist_enabled', true);
            await __click('#o_anilist_flow [data-url="https://anilist.co/settings/developer"]'); await __tick(10);
            out.skipLabel = __text('#onboardingSkip'); out.expectedSkip = t('action.skipSetup');
            await __click('#onboardingSkip'); await __tick(50);
            out.closed = !$('onboardingBackdrop').classList.contains('open');
            out.toasts = __toasts(); out.skipped = t('onboarding.skipped');
            return out;
            """,
            lang=lang,
            **FIRST_START,
        )
        out = result.value
        # The first start shows the guide and does nothing else until it is finished or skipped.
        assert out["open"] is True and out["started"] == 0
        assert out["heading"] == out["expected"]
        assert out["toggle"] is True
        assert result.calls_of("open_url") == [["https://anilist.co/settings/developer"]]
        assert out["skipLabel"] == out["expectedSkip"]
        assert out["closed"] is True and out["skipped"] in out["toasts"]
        names = [call["name"] for call in result.calls]
        assert (
            names.index("skip_onboarding")
            < names.index("startup_sync_anilist")
            < names.index("startup_maintenance")
        )
    # Switches sit on the left of their labels.
    assert css_rules("index.html").get(".onboarding-toggle", "justify-content") == "flex-start"
    # Re-running the guide from Settings: test_patch_e_ui.py (runSetupGuide); the Jimaku key page
    # (jimaku.cc/account, not /profile or /api/docs): test_v072_followup_ln_credentials_progress.py.


def test_final_pipeline_cache_schema_bumped_for_cold_open_fix(tmp_path: Path, monkeypatch) -> None:
    from pudge import pipeline_cache

    video = tmp_path / "video.mkv"
    video.write_bytes(b"fixture")
    config = AppConfig(config_path=tmp_path / "config.toml")
    config.paths.cache_dir = tmp_path / "cache"
    current = pipeline_cache._CACHE_SCHEMA
    monkeypatch.setattr(pipeline_cache, "_CACHE_SCHEMA", "final-pipeline-v9")
    pipeline_cache.save_final_pipeline_result(video, config, subtitle=None, subtitle_id=2, source="embedded")
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None
    monkeypatch.setattr(pipeline_cache, "_CACHE_SCHEMA", current)
    assert pipeline_cache.load_final_pipeline_result(video, config) is None


def test_library_payload_groups_episodes_and_uses_cover(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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
        api.manager.db.upsert_episode(LibraryEpisode(123, "Grouped Anime", episode, video, state="ready"))

    state = api.get_state()

    assert len(state["library"]) == 1
    group = state["library"][0]
    assert group["title"] == "Grouped Anime"
    assert group["episode_count"] == 3
    assert [item["episode"] for item in group["episodes"]] == [1, 2, 3]
    assert group["cover"].endswith("/covers/123.jpg")
    assert group["episodes"][0]["filename"] == "Grouped Anime - 01.mkv"


def test_optional_integration_fields_follow_their_switches(page) -> None:
    # Settings are one flat list (no "Additional settings" wrapper): test_settings_flatten_additional.py;
    # the LLM fields and the "why disabled" hover: test_v0104_settings_ux_cleanup.py.
    for lang in ("en", "ru"):
        out = page.run(
            """
            setPage('settings'); await __tick(20);
            const reason = id => $(id).closest('[data-tooltip]')?.dataset.tooltip || '';
            const look = () => ({anilist: [$('s_anilist_client').disabled, reason('s_anilist_client')],
                                 qbt: [$('s_qbt_url').disabled, reason('s_qbt_url')]});
            const out = {off: look(), texts: {anilist: PudgeI18n.t('ui.enableAnilistFirst'), qbt: PudgeI18n.t('ui.enableQbittorrentFirst')}};
            await __input('#s_anilist_enabled', true);
            await __input('#s_qbt_enabled', true);
            out.on = look();
            return out;
            """,
            lang=lang,
            state={"settings": {"anilist_enabled": False, "qbt_enabled": False}},
        ).value
        assert out["off"] == {"anilist": [True, out["texts"]["anilist"]], "qbt": [True, out["texts"]["qbt"]]}
        assert out["on"] == {"anilist": [False, ""], "qbt": [False, ""]}


SIDEBAR = r"""
await __tick(100);                                   // the start-up refresh is over
let finish;
__api.refresh_local = () => new Promise(resolve => { finish = () => resolve({state: __api.get_state(), stats: {}}); });
const button = () => ({disabled: $('refreshAll').disabled, label: __text('#refreshAll'),
                       spinner: Boolean($('refreshAll').querySelector('.spinner')), title: $('refreshAll').getAttribute('title')});
const out = {
  pages: [...document.querySelectorAll('.nav [data-page]')].map(node => node.dataset.page),
  removed: ['library', 'downloads', 'diagnostics', 'librarySort', 'pageRefresh'].filter(id => document.getElementById(id) && !document.getElementById(id).matches('#diagnostics.page')),
  refreshButtons: document.querySelectorAll('#refreshAll').length,
  idle: {...button(), status: __visible('#status')},
};
await __click('#refreshAll'); await __tick(10);
out.refreshing = {...button(), status: __visible('#status'), statusText: __text('#status')};
finish(); await __tick(50);
out.done = {...button(), status: __visible('#status')};
out.texts = {refresh: t('action.refresh'), refreshing: t('action.refreshing'), status: t('status.refreshing')};
return out;
"""


def test_sidebar_has_one_refresh_button_that_shows_progress(page) -> None:
    assert en("action.refreshing") == "Refreshing…"
    assert ru("action.refreshing") == "Обновление…"
    for lang in ("en", "ru"):
        out = page.run(SIDEBAR, lang=lang).value
        texts = out["texts"]
        # No Library, Downloads or Diagnostics pages in the sidebar (Diagnostics opens from Settings),
        # no Library sort control, no second refresh button on the pages.
        assert not {"library", "downloads", "diagnostics"} & set(out["pages"])
        assert out["removed"] == [] and out["refreshButtons"] == 1
        # Idle: no status line under the buttons.
        assert out["idle"] == {
            "disabled": False,
            "label": texts["refresh"],
            "spinner": False,
            "title": None,
            "status": False,
        }
        # While refreshing: disabled, a spinner with "Refreshing…", the status as its tooltip and below it.
        assert out["refreshing"] == {
            "disabled": True,
            "label": texts["refreshing"],
            "spinner": True,
            "title": texts["status"],
            "status": True,
            "statusText": texts["status"],
        }
        assert out["done"] == out["idle"]


WATCHING_MENU = r"""
setPage('current', true); renderCurrent(); await __tick(20);
await __fire('#currentContent .waiting-card strong', 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent');
await __tick(10);
const out = {actions: [...document.querySelectorAll('#contextMenu.open [data-context-action]')].map(node => node.dataset.contextAction)};
await __click('#contextMenu [data-context-action="score"]'); await __tick(20);
out.scoreTitle = __text('#modalTitle'); out.expectedScore = t('modal.setScore');
await __click('#modalBackdrop [data-action="close-modal"]'); await __tick(20);
await __fire('#currentContent .waiting-card strong', 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent');
await __click('#contextMenu [data-context-action="delete-files"]'); await __tick(20);
out.confirm = __text('#pudgeConfirmMessage'); out.expectedConfirm = t('confirm.deleteAnimeFiles') + '\n' + t('confirm.deleteAnimeFilesNoRedownload');
await __click('[data-pudge-confirm="ok"]'); await __tick(50);
out.toasts = __toasts(); out.dropped = t('toast.animeFilesDeleted', {status: t('anilistStatus.current')});
return out;
"""


def test_watching_card_menu_scores_and_deletes_the_anime(page) -> None:
    show = {
        "media_id": 4,
        "list_status": "CURRENT",
        "title": "Airing Waiting",
        "site_url": "https://anilist.co/anime/4",
        "next_episode": 3,
        "local": {"video_path": "/v/Airing Waiting - 03.mkv", "episode": 3, "state": "waiting_subtitles"},
    }
    for lang in ("en", "ru"):
        result = page.run(
            WATCHING_MENU,
            lang=lang,
            state={"current": [show], "home": {"waiting": [show]}},
            api={
                "drop_anime": {"ok": True, "state": {"home": {}}},
                "final_sequel_option": {"ok": True, "sequel": None},
            },
        )
        out = result.value
        # Watching titles can be scored and (with a downloaded episode) deleted;
        # "move to Watching" is for Planning only.
        assert {"score", "delete-files"} <= set(out["actions"]) and "watching" not in out["actions"]
        assert out["scoreTitle"] == out["expectedScore"]
        assert out["confirm"] == out["expectedConfirm"]
        assert result.calls_of("drop_anime") == [[4, "CURRENT"]]
        assert out["dropped"] in out["toasts"]


def test_drop_group_contains_only_local_pending_files(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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
    from pudge.manager_models import LibraryEpisode

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


def test_only_cut_off_titles_show_the_full_title_on_hover(page) -> None:
    # WKWebView shows native `title` tooltips late and for every title; Pudge removes them and
    # shows its own tooltip, only for titles that do not fit.
    planned = [
        {
            "media_id": 1,
            "title": "A title far too long for its card",
            "media_status": "FINISHED",
            "episodes": 12,
            "progress": 0,
        },
        {"media_id": 2, "title": "Short", "media_status": "FINISHED", "episodes": 12, "progress": 0},
    ]
    result = page.run(
        """
        setPage('planned', true); await __tick(50);
        const title = id => `#plannedContent .anime-card[data-media-id="${id}"] .anime-title`;
        Object.assign(document.querySelector(title(1)), {clientWidth: 120, clientHeight: 20, scrollWidth: 400, scrollHeight: 20});
        Object.assign(document.querySelector(title(2)), {clientWidth: 120, clientHeight: 20, scrollWidth: 120, scrollHeight: 20});
        await __tick(600);
        const tip = () => ({shown: $('titleTooltip').classList.contains('show'), text: $('titleTooltip').textContent});
        const out = {native: [1, 2].map(id => document.querySelector(title(id)).getAttribute('title'))};
        await __fire(title(1), 'mouseover', {}, 'MouseEvent'); out.long = tip();
        await __fire(title(1), 'mouseout', {}, 'MouseEvent'); out.left = tip();
        await __fire(title(2), 'mouseover', {}, 'MouseEvent'); out.short = tip();
        return out;
        """,
        state={"planned": planned},
    )
    out = result.value
    assert out["native"] == [None, None]
    assert out["long"] == {"shown": True, "text": "A title far too long for its card"}
    assert out["left"]["shown"] is False
    assert out["short"]["shown"] is False
    rules = css_rules("index.html")
    assert (rules.get(".title-tooltip", "opacity"), rules.get(".title-tooltip.show", "opacity")) == ("0", "1")


SCORE_WHEEL = r"""
const queue = [];                                   // Math.random answers, in order
Math.random = () => queue.length ? queue.shift() : 0.5;
const look = () => ({value: __text('#modalBody .random-score-value'), use: $('modalBody').querySelector('[data-action="score-anime"]').disabled,
                     useLabel: __text('#modalBody [data-action="score-anime"]'), reroll: $('modalBody').querySelector('[data-action="reroll-score"]').disabled});
queue.push(0.001, 0.5, 0.5);                        // score 1
showScoreModal({media_id: 9, title: 'Show'}, false, null);
const out = {title: __text('#modalTitle')};
let shownAt = null;
for (let ms = 10; ms <= 1500 && shownAt == null; ms += 10) { await __tick(10); if (ms === 500) out.spinning = look(); if (look().value) shownAt = ms; }
out.first = {...look(), shownAt};
queue.push(0.001, 0.999, 0.5, 0.5);                 // score 1 again (not allowed), then 10
await __click('#modalBody [data-action="reroll-score"]');
out.rerolling = look();
await __tick(1000);
out.second = look();
await __click('#modalBody [data-action="score-anime"]'); await __tick(20);
out.open = __visible('#modalBackdrop');
out.texts = {title: t('modal.setScore'), use1: t('action.useGeneratedScore', {score: 1}), use10: t('action.useGeneratedScore', {score: 10}),
             saved: t('toast.scoreSaved')};
out.toasts = __toasts();
// How likely each score is: the pick sweeps the whole range in 480 even steps.
const counts = Array(10).fill(0);
for (let i = 0; i < 480; i++) { queue.push((i + 0.5) / 480, 0.5); counts[chooseWheelResult(null).score - 1]++; }
out.counts = counts;
return out;
"""


def test_score_wheel_spins_one_second_weighted_toward_the_middle(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            SCORE_WHEEL,
            lang=lang,
            api={"set_anilist_score": {"ok": True}, "final_sequel_option": {"ok": True, "sequel": None}},
        )
        out, texts = result.value, result.value["texts"]
        assert out["title"] == texts["title"]
        # While the wheel turns there is nothing to save and no second spin.
        assert (
            out["spinning"]["use"] is True
            and out["spinning"]["reroll"] is True
            and out["spinning"]["value"] == ""
        )
        # The result shows after one second (the spin starts on the next frame).
        assert 1000 < out["first"]["shownAt"] <= 1050
        assert out["first"] == {
            "value": "1",
            "use": False,
            "useLabel": texts["use1"],
            "reroll": False,
            "shownAt": out["first"]["shownAt"],
        }
        # Spinning again works at once (no cooldown after a result) and never repeats the last score.
        assert out["rerolling"]["use"] is True and out["rerolling"]["value"] == ""
        assert out["second"] == {"value": "10", "use": False, "useLabel": texts["use10"], "reroll": False}
        assert result.calls_of("set_anilist_score") == [[9, 10, None]]
        assert out["open"] is False and texts["saved"] in out["toasts"]
        assert out["counts"] == [10, 20, 40, 70, 100, 100, 70, 40, 20, 10]


def test_external_subtitle_reveals_file_in_finder(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    subtitle = tmp_path / "prepared subtitles.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    calls: list[list[str]] = []

    patch_webapp_popen(monkeypatch, lambda command, *args, **kwargs: calls.append(command))

    result = api.reveal_subtitle_file(str(subtitle))

    assert result["ok"] is True
    assert calls == [["open", "-R", str(subtitle.resolve())]]


def test_library_row_names_the_external_subtitle_file_and_reveals_it(page) -> None:
    # Which rows reveal what (and the reveal_subtitle_file calls): test_v0616_graph_subtitle_settings.py.
    rows = [
        {
            "episode": 3,
            "state": "ready",
            "subtitle_source": "external",
            "subtitle_path": "/s/Show - 03.ja.ass",
            "subtitle_filename": "Show - 03.ja.ass",
            "video_path": "/v/03.mkv",
            "filename": "03.mkv",
        },
        {
            "episode": 4,
            "state": "ready",
            "subtitle_source": "external",
            "subtitle_path": "/s/04.srt",
            "video_path": "/v/04.mkv",
            "filename": "04.mkv",
        },
    ]
    result = page.run(
        """
        const box = document.createElement('div');
        box.innerHTML = ROWS.map(libraryEpisodeRow).join('');
        document.body.appendChild(box);
        const links = [...box.querySelectorAll('.library-subtitle')];
        const out = {tooltips: links.map(link => link.getAttribute('title')), labels: links.map(link => link.innerText), expected: t('subtitle.external')};
        links[0].click(); await __flush();
        return out;
        """.replace("ROWS", json.dumps(rows)),
    )
    out = result.value
    assert out["tooltips"] == ["Show - 03.ja.ass", "/s/04.srt"]  # the file name, else the path
    assert out["labels"] == [out["expected"], out["expected"]]
    assert result.calls_of("reveal_subtitle_file") == [["/s/Show - 03.ja.ass"]]
    # Shown as a link.
    assert css_rules("index.html").get(".library-subtitle.external-link", "cursor") == "pointer"


def test_save_settings_persists_notifications_toggle(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    result = api.save_settings({"notifications_enabled": False})

    assert result["settings"]["notifications_enabled"] is False
    assert api.config.ui.notifications_enabled is False


def test_home_moves_resumable_ready_anime_to_continue_watching(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

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


def test_poll_planned_release_discovery_exposes_backend_offers(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=321,
            title="Upcoming",
            status="PLANNING",
            next_airing_episode=1,
            next_airing_at=2_100_000_000,
        )
    )
    discovery = api.manager.planned_release_discovery()
    discovery._save(321, {"phase": "found", "found_at": 2_100_000_001})

    result = api.poll_planned_release_discovery()

    assert [row["media_id"] for row in result["offers"]] == [321]
    assert result["offers"][0]["title"] == "Upcoming"
