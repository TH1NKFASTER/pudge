from __future__ import annotations

import json
from pathlib import Path

import pudge.cli as cli
from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.models import SubtitleCandidate
from pudge.pipeline_cache import save_final_pipeline_result
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
        manager.db.queue_subtitle_job(video, 151379, episode, priority=120, error="Checking subtitle upgrade")

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
        manager.db.queue_subtitle_job(video, 151379, episode, priority=120, error="Checking subtitle upgrade")

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


def test_ocr_fallback_uses_timing_uncertain_group_and_policy_is_reversible(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video, subtitle = _ready_episode(
        manager, title="Mugenjou-hen Movie 1 - Akaza Sairai", episode=1, origin="ocr"
    )
    assert manager.config.matching.ocr_counts_as_ready is False

    assert manager._reconcile_ocr_readiness_policy() == 1
    item = manager.db.episode_by_path(video)
    assert item is not None
    assert item.state == "couldnt_sync"
    assert item.subtitle_origin == "ocr"
    assert item.subtitle_path == subtitle
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert "timing confidence" in str(jobs[0]["last_error"])
    first_next_check = float(jobs[0]["next_check"])

    assert manager._reconcile_ocr_readiness_policy() == 0
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert float(jobs[0]["next_check"]) == first_next_check

    manager.config.matching.ocr_counts_as_ready = True
    assert manager._reconcile_ocr_readiness_policy() == 1
    item = manager.db.episode_by_path(video)
    assert item is not None
    assert item.state == "ready"
    assert item.subtitle_origin == "ocr"
    assert item.subtitle_path == subtitle
    assert manager.db.subtitle_jobs() == []


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


# The native launcher drains its startup pool before Python runs (no PyObjC callbacks into a
# finalized interpreter on window close): test_install_script.py::test_launcher_runs_pudge_...


STARTUP = r"""
const base = __api.get_state;
const titled = title => { const state = base(); state.home.new_ready = [{media_id: 9, title, local: {video_path: '/v/x.mkv'}, ready_episodes: [1]}]; return state; };
window.__shown = [];
const seen = () => {
  const titles = [...document.querySelectorAll('#currentContent .airing-meta strong')].map(el => el.textContent).join(',');
  if (window.__shown[window.__shown.length - 1] !== titles) window.__shown.push(titles);
};
document.addEventListener('DOMContentLoaded', () => new MutationObserver(seen).observe(document.getElementById('currentContent'), {childList: true, subtree: true, characterData: true, attributes: true}));
__api.get_state = __api.get_state_fast = () => titled('Cached');
__api.startup_sync_anilist = async () => ({skipped: false, state: titled('AniList sync')});
__api.startup_maintenance = async () => ({running: true, state: titled('Maintenance started')});
let polls = 0;
__api.startup_maintenance_status = async () => (++polls < 3 ? {running: true, state: titled('Maintenance step ' + polls)} : {running: false, error: '', state: titled('Finished')});
"""


def test_startup_home_state_is_rendered_atomically_after_background_maintenance(page) -> None:
    result = page.run(
        "await __tick(5000); return {shown: window.__shown, toasts: __toasts(), finished: t('toast.startupFinished')};",
        before=STARTUP,
    )
    # Home shows the cached state, then the finished one: none of the half-done states of the
    # AniList sync or the background maintenance in between (cards used to jump around).
    assert result.value["shown"] == ["Cached", "Finished"]
    assert result.value["finished"] in result.value["toasts"]
    assert len(result.calls_of("startup_maintenance_status")) == 3


def _prepare_with_cached_ocr(tmp_path: Path, monkeypatch, *flags: str):
    """An episode whose final cache holds an OCR result; its text subtitle does not pass."""
    tmp_path.mkdir()
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    srt, sup, ocr_srt = tmp_path / "Show - 01.ja.srt", tmp_path / "Show - 01.sup", tmp_path / "ocr.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは。\n", encoding="utf-8")
    sup.write_bytes(b"PGS")
    ocr_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは。これは日本語です。\n", encoding="utf-8")
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.matching.ocr_image_subtitles = True
    save_final_pipeline_result(video, cfg, subtitle=ocr_srt, subtitle_id=None, source="ocr")
    searched: list[bool] = []
    converted: list[Path] = []
    monkeypatch.setattr(cli, "find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr(
        cli,
        "find_local_subtitles",
        lambda **_k: (
            searched.append(True)
            or [
                SubtitleCandidate(path=sup, source="local", score=95.0, name=sup.name),
                SubtitleCandidate(path=srt, source="local", score=80.0, name=srt.name),
            ]
        ),
    )
    monkeypatch.setattr(
        cli, "_sync_one", lambda _video, subtitle, *a, **k: (subtitle, {"sync_was_successful": True})
    )
    monkeypatch.setattr(cli, "subtitle_quality_accepted", lambda *a, **k: (False, "low_confidence"))
    monkeypatch.setattr(
        cli,
        "image_subtitle_to_srt",
        lambda _video, _cache, **kw: (
            converted.append(kw["subtitle_path"])
            or (ocr_srt, {"reason": "ocr_ready", "cue_count": 1, "quality": {"accepted": True}})
        ),
    )
    args = cli.build_parser().parse_args(
        ["--prepare-only", "--offline", "--no-anilist-progress", *flags, str(video)]
    )
    return cli.process_video(video, args, cfg, None), searched, converted


def test_text_only_retry_bypasses_ocr_final_cache_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    # An ordinary preparation reuses the cached OCR result ...
    status, searched, converted = _prepare_with_cached_ocr(tmp_path / "plain", monkeypatch)
    assert (status, searched, converted) == (0, [], [])
    assert "PREPARE_STATUS=ready" in capsys.readouterr().out
    # ... the background retry for verified text neither takes it from the cache nor makes a
    # new OCR (both made the episode bounce back to Ready on every poll).
    status, searched, converted = _prepare_with_cached_ocr(tmp_path / "retry", monkeypatch, "--text-only")
    out = capsys.readouterr().out
    assert searched == [True] and converted == []
    assert "PREPARE_STATUS=waiting_text_subtitles" in out and "PREPARE_STATUS=ready" not in out
    assert "--text-only" not in cli.build_parser().format_help()  # internal flag
