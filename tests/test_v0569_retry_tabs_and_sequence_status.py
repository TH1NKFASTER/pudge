from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager


ROOT = Path(__file__).parents[1]
HTML = ROOT / "anime_mpv" / "web" / "index.html"
INSTALLER = ROOT / "install.sh"


def test_active_navigation_tab_is_a_noop() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert "function setPage(page,force=false){if(!force&&ui.page===page)return false;" in html
    assert "const changed=setPage(b.dataset.page);" in html
    assert "if(changed&&b.dataset.page==='diagnostics')" in html
    assert "setPage(ui.page,true)" in html


def test_completed_watch_order_card_has_ready_status() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html[html.index("function readySequenceCard"):html.index("function readyHomeRenderer")]

    assert '<span class="airing-state">${escapeHtml(downloadedEpisodeText(lead))}</span>' in source
    assert "row.dataset.stateText||t('label.readyMovie')" in source


def test_installer_force_reinstalls_bundled_wheel() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert '[[ -d "$PROJECT_DIR/.git" ]]' in installer
    assert 'pip wheel "$PROJECT_DIR"' in installer
    assert 'WHEEL_CANDIDATES=("$PROJECT_DIR"/anime_mpv-*.whl(N))' in installer
    assert 'pip install --force-reinstall --no-deps "$WHEEL_PATH"' in installer


def test_resolver_upgrade_retries_delayed_jobs_immediately_with_agent_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.agent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = tmp_path / "Boku no Hero Academia - I am a Hero too.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 211711, None, delay_seconds=21600)
    manager.db.set_state("subtitle_resolver_generation", "8")

    monkeypatch.setattr(manager, "sync_downloads", lambda: [])
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)

    calls: list[int] = []
    monkeypatch.setattr(
        manager,
        "process_subtitle_jobs",
        lambda limit=4: calls.append(limit) or 1,
    )

    stats = manager.run_startup_once()

    assert calls == []
    assert stats["subs"] == 0
    job = next(row for row in manager.db.subtitle_jobs() if str(row["video_path"]) == str(video))
    assert int(job["priority"]) >= 200
    assert manager.db.get_state("subtitle_resolver_generation", "") == "9"
    row = manager.db.subtitle_jobs()[0]
    assert float(row["next_check"]) <= __import__("time").time() + 2
