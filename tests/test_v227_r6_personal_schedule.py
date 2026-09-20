from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.personal_schedule import materialize_weekly_items
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from pudge.web_app import WebAppApi


def _anime(media_id: int = 42, *, progress: int = 12) -> LibraryAnime:
    return LibraryAnime(
        media_id=media_id,
        title="Finished TV",
        status="COMPLETED" if progress >= 12 else "CURRENT",
        progress=progress,
        episodes=12,
        format="TV",
        media_status="FINISHED",
    )


def _api(db: Database) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(
        db=db,
        japanese_subtitles_required=lambda _media_id: True,
    )
    api.config = SimpleNamespace(
        matching=SimpleNamespace(ocr_counts_as_ready=True),
    )
    api._episode_offset_cache = {}
    return api


def test_weekly_materialization_preserves_wall_clock_across_dst_and_resolves_gap() -> None:
    rows = materialize_weekly_items(
        [1, 2, 3],
        start_local_datetime="2026-03-01T02:30",
        timezone_iana="America/New_York",
    )
    assert rows[0].nominal_local_datetime == "2026-03-01T02:30"
    assert rows[1].nominal_local_datetime == "2026-03-08T02:30"
    # 02:30 does not exist on the US spring-forward day. The authoritative UTC
    # boundary is shifted to the first valid local time (03:00), not +604800s.
    resolved = datetime.fromtimestamp(rows[1].unlock_at_utc, UTC).astimezone()
    assert rows[1].unlock_at_utc - rows[0].unlock_at_utc == pytest.approx(167.5 * 3600)
    assert rows[2].unlock_at_utc > rows[1].unlock_at_utc


def test_exact_boundary_unlocks_and_clock_rewind_does_not_relock(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(_anime())
    schedule = db.save_personal_release_schedule(
        42,
        episodes=[1, 2, 3],
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    boundary = schedule.items[0].unlock_at_utc
    before = db.personal_release_schedule(42, now=boundary - 0.001)
    assert before is not None and before.items[0].unlocked is False
    at = db.personal_release_schedule(42, now=boundary)
    assert at is not None and at.items[0].unlocked is True
    rewound = db.personal_release_schedule(42, now=boundary - 3600)
    assert rewound is not None and rewound.items[0].unlocked is True


def test_skipped_weeks_accumulate_but_nearest_unwatched_stays_authoritative(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    schedule = db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2, 3, 4],
        start_local_datetime="2026-09-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    late = schedule.items[2].unlock_at_utc + 1
    refreshed = db.personal_release_schedule(anime.media_id, now=late)
    assert refreshed is not None
    assert [item.unlocked for item in refreshed.items] == [True, True, True, False]
    api = _api(db)
    snapshot = api._personal_schedule_snapshot(anime, now=late)
    assert snapshot["next_episode"] == 1


def test_edit_changes_only_future_locked_items(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(_anime())
    first = db.save_personal_release_schedule(
        42,
        episodes=[1, 2, 3],
        start_local_datetime="2026-09-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    opened_at = first.items[0].unlock_at_utc
    db.personal_release_schedule(42, now=opened_at)
    edited = db.save_personal_release_schedule(
        42,
        episodes=[1, 2, 3],
        start_local_datetime="2026-10-01T21:30",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=opened_at,
    )
    assert edited.items[0].unlock_at_utc == first.items[0].unlock_at_utc
    assert edited.items[0].unlocked is True
    assert edited.items[1].unlock_at_utc != first.items[1].unlock_at_utc
    assert edited.revision == first.revision + 1


def test_completed_title_rewatch_does_not_mutate_anilist_and_review_grant_survives(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    gate = ReviewGateStore(db)
    identity = EpisodeReviewIdentity(media_id=anime.media_id, episode=1)
    gate.mark_confirmed("jiten:test", identity, required=1, card_key="100:0")
    db.save_personal_release_schedule(
        anime.media_id,
        episodes=list(range(1, 13)),
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    unchanged = db.get_anime(anime.media_id)
    assert unchanged is not None
    assert unchanged.status == "COMPLETED"
    assert unchanged.progress == 12
    assert unchanged.media_status == "FINISHED"
    assert gate.status("jiten:test", identity, required=1).granted is True


def test_schedule_snapshot_moves_caught_up_to_ready_and_handles_missing_subs(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime(progress=12)
    db.upsert_anime(anime)
    video = tmp_path / "ep1.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(media_id=anime.media_id, title=anime.title, episode=1, video_path=video, state="local"))
    schedule = db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    api = _api(db)
    before = api._personal_schedule_snapshot(anime, now=schedule.items[0].unlock_at_utc - 1)
    assert before["status"] == "caught_up"
    after = api._personal_schedule_snapshot(anime, now=schedule.items[0].unlock_at_utc)
    assert after["status"] == "waiting"
    subtitle = tmp_path / "ep1.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    db.upsert_episode(LibraryEpisode(media_id=anime.media_id, title=anime.title, episode=1, video_path=video, subtitle_path=subtitle, state="ready"))
    ready = api._personal_schedule_snapshot(anime, now=schedule.items[0].unlock_at_utc)
    assert ready["status"] == "new_ready"
    assert ready["next_episode"] == 1


def test_future_schedule_item_is_retained_from_cleanup(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    video = tmp_path / "ep2.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=2,
            video_path=video,
            state="watched",
            watched_at=10,
            delete_after=20,
        )
    )
    db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2099-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=30,
    )
    assert db.personal_release_item_retained(anime.media_id, 2) is True
    # due_cleanup uses real wall time, but the schedule is far in the future and
    # must pin the old watched file despite its old delete_after timestamp.
    assert all(Path(row["video_path"]) != video for row in db.due_cleanup())


def test_schema_version_contains_personal_schedule_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "personal_release_schedules" in tables
    assert "personal_release_items" in tables

def test_play_admission_blocks_future_and_out_of_order_items(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    paths = []
    for number in (1, 2):
        video = tmp_path / f"ep{number}.mkv"
        video.write_bytes(b"video")
        subtitle = tmp_path / f"ep{number}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        db.upsert_episode(
            LibraryEpisode(
                media_id=anime.media_id,
                title=anime.title,
                episode=number,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )
        paths.append(video)
    schedule = db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    api = _api(db)
    db.personal_release_schedule(anime.media_id, now=schedule.items[0].unlock_at_utc)
    assert api._personal_schedule_play_admission(paths[0])["allowed"] is True
    second_early = api._personal_schedule_play_admission(paths[1])
    assert second_early["allowed"] is False
    assert second_early["reason"] == "previous_episode_pending"
    db.mark_personal_release_watched(anime.media_id, 1, watched_at=schedule.items[0].unlock_at_utc + 1)
    second_locked = api._personal_schedule_play_admission(paths[1])
    assert second_locked["allowed"] is False
    assert second_locked["reason"] == "not_unlocked"
    db.personal_release_schedule(anime.media_id, now=schedule.items[1].unlock_at_utc)
    assert api._personal_schedule_play_admission(paths[1])["allowed"] is True


def test_disabling_schedule_keeps_schedule_history_but_removes_retention_pin(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2099-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    assert db.personal_release_item_retained(anime.media_id, 2) is True
    assert db.disable_personal_release_schedule(anime.media_id) is True
    history = db.personal_release_schedule(anime.media_id)
    assert history is not None and history.enabled is False and len(history.items) == 2
    assert db.personal_release_item_retained(anime.media_id, 2) is False


def test_schedule_rewatch_counts_as_ready_for_review_prefetch_target(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    video = tmp_path / "ep1.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "ep1.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=1,
            video_path=video,
            subtitle_path=subtitle,
            state="watched",
        )
    )
    db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2000-01-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    api = _api(db)
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    assert api._review_gate_count_ready_anime() == 1


def test_schedule_notification_uses_schedule_dedupe_even_if_original_notification_was_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = _anime()
    manager.db.upsert_anime(anime)
    for number in (1, 2):
        video = tmp_path / f"ep{number}.mkv"
        video.write_bytes(b"video")
        subtitle = tmp_path / f"ep{number}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=anime.media_id,
                title=anime.title,
                episode=number,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )
    schedule = manager.db.save_personal_release_schedule(
        anime.media_id,
        episodes=[1, 2],
        start_local_datetime="2000-01-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    manager.db.set_state(f"ready_notification:episode:{anime.media_id}:1", "delivered")
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda subtitle, message: delivered.append((subtitle, message)) or True,
    )

    manager._notify_ready_episode(
        video=(tmp_path / "ep1.mkv"), media_id=anime.media_id, episode=1
    )

    assert len(delivered) == 1
    assert "episode 1" in delivered[0][1].lower()
    assert "all episodes" not in delivered[0][1].lower()
    assert manager.db.get_state(
        f"personal_schedule_notification:{schedule.schedule_id}:1:ready", ""
    ) == "delivered"


def test_schedule_dialog_preview_lists_every_scheduled_episode(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime()
    db.upsert_anime(anime)
    db.save_personal_release_schedule(
        anime.media_id,
        episodes=list(range(1, 13)),
        start_local_datetime="2099-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    api = _api(db)
    payload = api.personal_schedule_get(anime.media_id)
    preview = payload["schedule"]["preview"]
    assert len(preview) == 12
    assert [row["ordinal"] for row in preview] == list(range(12))
