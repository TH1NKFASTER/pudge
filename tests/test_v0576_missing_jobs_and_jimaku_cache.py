from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.providers.jimaku import JimakuClient


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg, log=lambda _message: None)


def stub_regular_maintenance(monkeypatch, manager: AnimeManager) -> None:
    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_after_resolver_upgrade", lambda: 0)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)


def test_manual_refresh_recreates_missing_job_from_waiting_library_row(
    tmp_path: Path, monkeypatch
) -> None:
    manager = make_manager(tmp_path)
    stub_regular_maintenance(monkeypatch, manager)
    video = manager.config.library.root_dir / "Mushoku Tensei III - 06.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178789,
            title="Mushoku Tensei III: Isekai Ittara Honki Dasu",
            episode=6,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    assert manager.db.subtitle_jobs() == []
    monkeypatch.setattr(manager, "scan_library", lambda: manager.db.episodes())

    observed: dict[str, int] = {}

    def process(*, limit: int = 4) -> int:
        observed["limit"] = limit
        observed["due"] = len(manager.db.due_subtitle_jobs(limit=100))
        return observed["due"]

    monkeypatch.setattr(manager, "process_subtitle_jobs", process)
    stats = manager.run_once(force_subtitle_retry=True)

    assert observed == {"limit": 8, "due": 1}
    assert stats["subs"] == 1
    job = manager.db.subtitle_jobs()[0]
    assert job["media_id"] == 178789
    assert job["episode"] == 6


def test_manual_refresh_recovers_processing_job_before_lease_expires(
    tmp_path: Path, monkeypatch
) -> None:
    manager = make_manager(tmp_path)
    stub_regular_maintenance(monkeypatch, manager)
    video = manager.config.library.root_dir / "Mushoku Tensei III - 06.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178789,
            title="Mushoku Tensei III: Isekai Ittara Honki Dasu",
            episode=6,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 178789, 6)
    claimed = manager.db.claim_due_subtitle_jobs(1, lease_seconds=3600)
    assert len(claimed) == 1
    assert manager.db.due_subtitle_jobs(limit=10) == []
    monkeypatch.setattr(manager, "scan_library", lambda: manager.db.episodes())

    observed: dict[str, int] = {}

    def process(*, limit: int = 4) -> int:
        observed["due"] = len(manager.db.due_subtitle_jobs(limit=100))
        return observed["due"]

    monkeypatch.setattr(manager, "process_subtitle_jobs", process)
    manager.run_once(force_subtitle_retry=True)

    assert observed["due"] == 1
    assert manager.db.subtitle_jobs()[0]["state"] == "pending"


def test_scan_ensures_missing_local_subtitle_job_without_resetting_backoff(
    tmp_path: Path, monkeypatch
) -> None:
    manager = make_manager(tmp_path)
    video = manager.config.library.root_dir / "Mushoku Tensei III - 06.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: ("none", None, None),
    )

    manager.scan_library()
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    manager.db.postpone_subtitle_job(video.resolve(), "not yet", 6 * 3600)
    future = float(manager.db.subtitle_jobs()[0]["next_check"])

    manager.scan_library()
    assert float(manager.db.subtitle_jobs()[0]["next_check"]) >= future - 0.01


def test_empty_jimaku_files_cache_is_revalidated(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    client = JimakuClient(
        "https://jimaku.cc",
        "token",
        cache_dir=cache_dir,
        cache_ttl_seconds=120,
    )
    path = "/api/entries/12216/files"
    params = {"episode": 6}
    raw = json.dumps(
        {"base_url": "https://jimaku.cc", "path": path, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    cache_path = cache_dir / "jimaku-api" / f"{digest}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("[]", encoding="utf-8")
    cache_path.touch()
    calls = 0

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return [
                {
                    "url": "https://example.test/mushoku-06.srt",
                    "name": "[shincaps] Mushoku Tensei III - 06.srt",
                    "size": 123,
                    "last_modified": "",
                }
            ]

    class FakeHttp:
        def get(self, _url, *, params=None):
            nonlocal calls
            calls += 1
            assert params == {"episode": 6}
            return Response()

    client.client = FakeHttp()  # type: ignore[assignment]
    files = client.files(12216, 6)

    assert calls == 1
    assert [item.name for item in files] == ["[shincaps] Mushoku Tensei III - 06.srt"]
