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
    db.upsert_episode(LibraryEpisode(media_id=51, title="Ready", episode=1, video_path=video, state="waiting_subtitles"))

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


def test_ui_watches_ready_marker_and_refreshes_immediately_on_focus_and_home() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")

    assert "readyStateWatchTimer:null" in html
    assert "await pywebview.api.ui_state_versions()" in html
    assert "const next=await pywebview.api.get_state_fast()" in html
    assert "scheduleReadyStateWatch(document.hidden||!ui.windowActive?15000:1000)" in html
    assert "void syncReadyStateVersion(true);scheduleReadyStateWatch(1000)" in html
    assert "if(b.dataset.page==='current')void syncReadyStateVersion(true)" in html
