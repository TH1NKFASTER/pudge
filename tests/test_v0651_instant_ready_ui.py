from pathlib import Path

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
from pudge.config import AppConfig, write_config


def _make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_ready_transition_bumps_cross_process_ui_marker(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=51, title="Ready", status="CURRENT", progress=0, episodes=12))
    video = tmp_path / "Ready - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(media_id=51, title="Ready", episode=1, video_path=video, state="waiting_subtitles")
    )

    assert db.get_state("ready_state_version", "") == ""
    db.set_subtitle_ready(video, None, origin="embedded")
    first = db.get_state("ready_state_version", "")
    assert first

    # Re-saving an already-ready row is not a new UI event.
    db.set_subtitle_ready(video, None, origin="embedded")
    assert db.get_state("ready_state_version", "") == first


def test_web_api_exposes_cheap_ready_marker_and_state_payload(tmp_path: Path) -> None:
    api = _make_api(tmp_path)
    api.manager.db.set_state("ready_state_version", "123456")

    assert api.ready_state_version() == "123456"
    assert api.ui_state_versions()["ready"] == "123456"
    assert "ui" in api.ui_state_versions()
    assert api.get_state_fast()["ready_state_version"] == "123456"
    assert "ui_state_version" in api.get_state_fast()


# The one-second version check, the state fetch only on a change, the immediate resync on
# focus: test_ready_notification_ui_refresh_v0603.py.
def test_returning_to_anime_tab_fetches_the_state_at_once(page) -> None:
    # Versions unchanged: opening the Anime tab still fetches the state immediately (it may
    # have been missed), so a newly ready episode is there without waiting for a check.
    result = page.run(
        r"""
        await __tick(2500);
        await __click('.nav button[data-page="settings"]'); await __tick(300);
        window.__ready = true;                                  // the backend now has a ready episode
        const fetches = () => __apiCalls.filter(c => c.name === 'get_state_fast').length;
        const before = fetches();
        await __click('.nav button[data-page="current"]'); await __flush();     // no clock time passes
        return {fetched: fetches() - before, shown: __text('#currentContent').includes('New show')};
        """,
        before="""
        const base = __api.get_state_fast;
        __api.get_state_fast = async () => { const state = await base();
          if (window.__ready) state.home = {...state.home, new_ready: [{media_id: 9, title: 'New show', ready_episodes: [1], site_url: 'https://anilist.co/anime/9',
                                                                         local: {episode: 1, state: 'ready', video_path: '/v/n1.mkv'}}]};
          return state; };
        """,
        api={"ui_state_versions": {"ready": "1", "ui": "1"}},
    )
    out = result.value
    assert out == {"fetched": 1, "shown": True}
