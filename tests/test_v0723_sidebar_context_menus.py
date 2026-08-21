from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_refresh_and_torrent_right_click_contract() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert 'data-sidebar-context-action="subtitles-new"' in html
    assert 'data-sidebar-context-action="anilist"' in html
    assert 'data-sidebar-context-action="scan-local"' in html
    assert 'data-sidebar-context-action="releases"' in html
    assert 'data-sidebar-context-action="torrent-toggle"' in html
    assert 'data-sidebar-context-action="downloads"' in html
    assert "e.target.closest?.('#refreshAll,#torrentToggleButton')" in html
    assert "await openDownloadCenter(null)" in html
    assert "await syncAniList(false)" in html
    assert "pywebview.api.scan_library()" in html
    assert "pywebview.api.search_new_subtitles()" in html
    assert "pywebview.api.search_new_releases()" in html


def test_new_subtitle_action_never_requeues_old_attempts(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    old = tmp_path / "old.mkv"
    new = tmp_path / "new.mkv"
    old.write_bytes(b"old")
    new.write_bytes(b"new")

    api.manager.db.queue_subtitle_job(old.resolve(), 1, 1)
    api.manager.db.queue_subtitle_job(new.resolve(), 2, 2)
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=2,next_check=0 WHERE video_path=?",
            (str(old.resolve()),),
        )
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=0,next_check=0 WHERE video_path=?",
            (str(new.resolve()),),
        )

    @contextmanager
    def acquired(*_args, **_kwargs):
        yield True

    observed: list[Path] = []

    def process(*, limit: int = 4, preferred_paths=()):
        del limit
        observed.extend(Path(item).resolve() for item in preferred_paths)
        return len(preferred_paths)

    monkeypatch.setattr("pudge.web_app.maintenance_lock", acquired)
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", process)
    monkeypatch.setattr(api, "get_state", lambda: {"ok": True})

    result = api.search_new_subtitles()

    assert result["eligible"] == 1
    assert result["processed"] == 1
    assert observed == [new.resolve()]
    assert old.resolve() not in observed


def test_new_subtitle_action_has_no_force_retry_path() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    method = source[
        source.index("    def search_new_subtitles("):
        source.index("    def search_new_releases(")
    ]
    assert 'attempts != 0' in method
    assert 'str(row["state"] or "") != "pending"' in method
    assert "force_subtitle_retry" not in method
    assert "postpone_subtitle_job" not in method
    assert "queue_subtitle_job" not in method


def test_release_search_reuses_existing_auto_discovery() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    method = source[
        source.index("    def search_new_releases("):
        source.index("    def _run_startup_maintenance_background(")
    ]
    assert "self.manager.auto_search_current()" in method
