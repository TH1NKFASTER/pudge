from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.presentation_state import derive_episode_presentation


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir()
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return AnimeManager(cfg)


def _ready_episode(
    manager: AnimeManager,
    *,
    title: str,
    episode: int,
    origin: str = "external",
) -> tuple[Path, Path]:
    video = manager.config.library.root_dir / f"{title} - {episode:02d}.mkv"
    subtitle = manager.config.library.root_dir / f"{title} - {episode:02d}.ja.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            151379,
            title,
            episode,
            video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
            subtitle_origin=origin,
        )
    )
    return video.resolve(), subtitle.resolve()


def test_akiba_upgrade_jobs_never_demote_ready_episodes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    rows = [_ready_episode(manager, title="Akiba Maid Sensou", episode=ep) for ep in (7, 8)]

    for episode, (video, subtitle) in zip((7, 8), rows, strict=True):
        manager.db.set_state(
            manager._subtitle_upgrade_state_key(video),
            json.dumps(
                {
                    "previous_subtitle_path": str(subtitle),
                    "previous_embedded_sid": None,
                    "previous_state": "ready",
                    "previous_origin": "external",
                }
            ),
        )
        manager.db.queue_subtitle_job(
            video, 151379, episode, priority=120, error="Checking subtitle upgrade"
        )

    preserve = manager._active_subtitle_upgrade_paths()
    assert preserve == {str(video) for video, _subtitle in rows}
    assert manager.db.repair_spurious_ready_subtitle_jobs(preserve_paths=preserve) == 0
    assert manager.db.repair_stale_subtitle_selections(preserve_paths=preserve) == 0

    for video, subtitle in rows:
        item = manager.db.episode_by_path(video)
        assert item is not None
        assert item.state == "ready"
        assert item.subtitle_path == subtitle
    assert len(manager.db.subtitle_jobs()) == 2


def test_akiba_v39_demotions_are_restored_from_upgrade_markers(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    rows = [_ready_episode(manager, title="Akiba Maid Sensou", episode=ep) for ep in (7, 8)]

    for episode, (video, subtitle) in zip((7, 8), rows, strict=True):
        manager.db.set_state(
            manager._subtitle_upgrade_state_key(video),
            json.dumps(
                {
                    "previous_subtitle_path": str(subtitle),
                    "previous_embedded_sid": None,
                    "previous_state": "ready",
                    "previous_origin": "external",
                }
            ),
        )
        manager.db.queue_subtitle_job(
            video, 151379, episode, priority=120, error="Checking subtitle upgrade"
        )

    # Reproduce the v39 generic repair race from the production log.
    assert manager.db.repair_stale_subtitle_selections() == 2
    assert all(manager.db.episode_by_path(video).state == "waiting_subtitles" for video, _ in rows)

    assert manager._restore_interrupted_subtitle_upgrades() == 2
    for video, subtitle in rows:
        item = manager.db.episode_by_path(video)
        assert item is not None
        assert item.state == "ready"
        assert item.subtitle_path == subtitle
    assert len(manager.db.subtitle_jobs()) == 2


def test_ocr_fallback_is_not_ready_by_default_and_is_reconciled(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video, subtitle = _ready_episode(
        manager, title="Mugenjou-hen Movie 1 - Akaza Sairai", episode=1, origin="ocr"
    )
    assert manager.config.matching.ocr_counts_as_ready is False

    assert manager._reconcile_ocr_readiness_policy() == 1
    item = manager.db.episode_by_path(video)
    assert item is not None
    assert item.state == "waiting_text_subtitles"
    assert item.subtitle_origin == "ocr"
    assert item.subtitle_path == subtitle
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert "verified Japanese text" in str(jobs[0]["last_error"])


def test_ocr_ready_policy_is_separate_from_ocr_generation_setting(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.matching.ocr_image_subtitles is True
    assert cfg.matching.ocr_counts_as_ready is False
    cfg.matching.ocr_counts_as_ready = True
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.matching.ocr_image_subtitles is True
    assert loaded.matching.ocr_counts_as_ready is True


def test_presentation_hides_ocr_ready_when_policy_disallows_it(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    local = {"video_path": str(video), "state": "ready", "subtitle_origin": "ocr"}

    blocked = derive_episode_presentation(local=local, allow_ocr_ready=False)
    allowed = derive_episode_presentation(local=local, allow_ocr_ready=True)

    assert blocked["status"] == "waiting_text_subtitles"
    assert blocked["ready"] is False
    assert allowed["status"] == "ready"
    assert allowed["ready"] is True


def test_native_launcher_does_not_drain_outer_objc_pool_after_python_finalize() -> None:
    source = Path("install.sh").read_text(encoding="utf-8")
    assert "NATIVE_SHELL_REV=2" in source
    main = source[source.index("int main(int argc, char *argv[]) {") : source.index('"""', source.index("int main(int argc, char *argv[]) {"))]

    # Py_RunMain finalizes Python. There must be no outer Objective-C pool whose
    # later drain can invoke PyObjC dealloc callbacks after interpreter teardown.
    assert "int main(int argc, char *argv[]) {\n    @autoreleasepool" not in main
    assert "@autoreleasepool {\n        NSString *icon_path" in main
    assert main.index("@autoreleasepool {\n        NSString *icon_path") < main.index("PyConfig config;")
    assert main.rstrip().endswith("return Py_RunMain();\n}")


def test_startup_home_state_is_rendered_atomically_after_background_maintenance() -> None:
    source = Path("pudge/web/index.html").read_text(encoding="utf-8")
    poll = source[source.index("async function pollStartupMaintenance()") : source.index("async function startupSequence()")]
    startup = source[source.index("async function startupSequence()") : source.index("async function loadState()")]

    assert "if(r.running){ui.startupDeferredState=r.state||ui.startupDeferredState" in poll
    running_branch = poll.split("if(r.running){", 1)[1].split("return;}", 1)[0]
    assert "renderDataPages" not in running_branch
    assert "ui.state=r.state||ui.startupDeferredState||ui.state" in poll
    assert "renderDataPages(true)" in poll

    assert "if(ui.startupMaintenanceRunning){ui.startupDeferredState=r.state||ui.state;}" in startup
    assert "else{ui.state=r.state||ui.state;ui.startupDeferredState=null;renderDataPages();}" in startup

    sync = source[source.index("async function syncAniList(startup=false)") : source.index("async function pollStartupMaintenance()")]
    assert "if(startup)ui.startupDeferredState=r.state||ui.startupDeferredState;else ui.state=r.state" in sync
    startup_sync_branch = sync.split("if(startup)", 1)[1].split("else ui.state", 1)[0]
    assert "renderDataPages" not in startup_sync_branch


def test_text_only_retry_bypasses_ocr_final_cache_paths() -> None:
    source = Path("pudge/cli.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--text-only", action="store_true", help=argparse.SUPPRESS)' in source
    assert "not args.text_only" in source
