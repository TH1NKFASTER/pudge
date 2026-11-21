"""Personal-schedule unlock notifications for a fully downloaded batch.

Every file of the batch is on disk from the start; episodes unlock one a week. The
unlock notification must come for every episode (not only the first), from the
Python side too (closed window, throttled page, sleeping Mac), and only once.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.personal_schedule import READY_BEFORE_PLAYBACK
from pudge.web_app import WebAppApi

MEDIA_ID = 105228


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.ui.notifications_enabled = True
    cfg.ui.language = "en"
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(
        media_id=MEDIA_ID,
        title="Synthetic Batch",
        status="CURRENT",
        progress=0,
        episodes=3,
        format="TV",
        media_status="FINISHED",
    )
    manager.db.upsert_anime(anime)
    videos: list[Path] = []
    for number in (1, 2, 3):
        video = tmp_path / f"Synthetic Batch - {number:02d}.mkv"
        video.write_bytes(b"video")
        subtitle = tmp_path / f"Synthetic Batch - {number:02d}.ja.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=MEDIA_ID,
                title=anime.title,
                episode=number,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )
        videos.append(video)
    schedule = manager.db.save_personal_release_schedule(
        MEDIA_ID,
        episodes=[1, 2, 3],
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="UTC",
        rewatch=False,
        now=0,
    )
    clock = _Clock(schedule.items[0].unlock_at_utc + 60)
    monkeypatch.setattr(time, "time", clock)
    api = object.__new__(WebAppApi)
    api.manager = manager
    api.config = cfg
    api.logger = logging.getLogger("test.schedule_unlock")
    api._episode_offset_cache = {}
    api._get_state = lambda **_kwargs: {}
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.web_app.send_native_notification",
        lambda subtitle, message: sent.append((subtitle, message)) or True,
    )
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda subtitle, message: sent.append((subtitle, message)) or True,
    )
    return manager, api, schedule, videos, clock, sent


def _key(schedule, episode: int) -> str:
    return f"personal_schedule_notification:{schedule.schedule_id}:{schedule.cycle_id}:{episode}:ready"


def _watch(manager: AnimeManager, episode: int, clock: _Clock) -> None:
    assert manager.db.mark_personal_release_watched(MEDIA_ID, episode, watched_at=clock())


def test_playing_e1_while_e2_locked_does_not_suppress_e2_unlock(batch) -> None:
    manager, api, schedule, videos, clock, sent = batch
    assert api._personal_schedule_deliver_notifications(source="background") == [
        {"media_id": MEDIA_ID, "episode": 1}
    ]
    manager.remember_ready_before_playback(videos[0])
    assert manager.db.get_state(_key(schedule, 2), "") == ""
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 30
    assert api._personal_schedule_deliver_notifications(source="background") == [
        {"media_id": MEDIA_ID, "episode": 2}
    ]
    assert len(sent) == 2 and "episode 2" in sent[1][1]
    # E3 too: the bug was "only the first episode notifies".
    manager.remember_ready_before_playback(videos[1])
    _watch(manager, 2, clock)
    clock.value = schedule.items[2].unlock_at_utc + 30
    api._personal_schedule_deliver_notifications(source="background")
    assert len(sent) == 3 and "episode 3" in sent[2][1]


def test_already_unlocked_next_episode_is_still_suppressed(batch) -> None:
    manager, api, schedule, videos, clock, sent = batch
    clock.value = schedule.items[1].unlock_at_utc + 30  # E1 and E2 both unlocked
    manager.db.personal_release_schedule(MEDIA_ID)
    manager.remember_ready_before_playback(videos[0])
    assert manager.db.get_state(_key(schedule, 2), "") == READY_BEFORE_PLAYBACK
    _watch(manager, 1, clock)
    clock.value += 600
    api._personal_schedule_deliver_notifications(source="background")
    assert not any("episode 2" in message for _subtitle, message in sent)


def test_marker_left_by_older_build_while_locked_does_not_block_unlock(batch) -> None:
    manager, api, schedule, _videos, clock, sent = batch
    manager.db.set_state(_key(schedule, 1), "delivered")
    manager.db.set_state(_key(schedule, 2), READY_BEFORE_PLAYBACK)  # set while E2 was locked
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 30
    assert api._personal_schedule_deliver_notifications(source="background") == [
        {"media_id": MEDIA_ID, "episode": 2}
    ]
    assert manager.db.get_state(_key(schedule, 2)) == "delivered"


def test_background_loop_notifies_without_the_page(batch) -> None:
    manager, api, schedule, _videos, clock, sent = batch
    manager.db.set_state(_key(schedule, 1), "delivered")
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 5

    class _Cancel:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return False

        def wait(self, seconds: float) -> bool:
            self.waits.append(float(seconds))
            return len(self.waits) >= 2  # start delay, one tick, then stop

    cancel = _Cancel()
    api._personal_schedule_notify_loop(cancel)
    assert len(sent) == 1 and "episode 2" in sent[0][1]
    # The next wait aims at E3's unlock but never sleeps longer than the cap.
    assert 1.0 <= cancel.waits[1] <= api.PERSONAL_SCHEDULE_TICK_MAX_WAIT


def test_next_wait_targets_unlock_and_caps_for_sleep(batch) -> None:
    _manager, api, schedule, _videos, clock, _sent = batch
    clock.value = schedule.items[1].unlock_at_utc - 42
    assert api._personal_schedule_next_wait(now=clock()) == pytest.approx(43.0)
    clock.value = schedule.items[1].unlock_at_utc - 86400
    assert api._personal_schedule_next_wait(now=clock()) == api.PERSONAL_SCHEDULE_TICK_MAX_WAIT


def test_page_and_background_ticks_deliver_once(batch, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, api, schedule, _videos, clock, sent = batch
    manager.db.set_state(_key(schedule, 1), "delivered")
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 5
    started = threading.Event()

    def slow_send(subtitle: str, message: str) -> bool:
        started.set()
        threading.Event().wait(0.2)
        sent.append((subtitle, message))
        return True

    monkeypatch.setattr("pudge.web_app.send_native_notification", slow_send)
    worker = threading.Thread(
        target=api._personal_schedule_deliver_notifications, kwargs={"source": "background"}
    )
    worker.start()
    assert started.wait(5)
    page = api.personal_schedule_tick()
    worker.join(5)
    assert page["ok"] is True and page["delivered"] == []
    assert len(sent) == 1
    api.personal_schedule_tick()
    api._personal_schedule_deliver_notifications(source="background")
    assert len(sent) == 1


def test_late_delivery_after_sleep_says_when_it_unlocked(batch) -> None:
    manager, api, schedule, _videos, clock, sent = batch
    manager.db.set_state(_key(schedule, 1), "delivered")
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 3 * 3600  # the Mac slept through it
    api._personal_schedule_deliver_notifications(source="background")
    expected = api._personal_schedule_unlock_time_text(schedule.items[1].unlock_at_utc, clock())
    assert len(sent) == 1 and f"unlocked at {expected}" in sent[0][1]


def test_disabled_notifications_are_respected(batch) -> None:
    manager, api, schedule, _videos, _clock, sent = batch
    api.config.ui.notifications_enabled = False
    api._personal_schedule_deliver_notifications(source="background")
    assert sent == []
    assert manager.db.get_state(_key(schedule, 1)) == "disabled"


def test_file_ready_path_ignores_stale_locked_marker(batch) -> None:
    manager, _api, schedule, videos, clock, sent = batch
    manager.db.set_state(_key(schedule, 1), "delivered")
    manager.db.set_state(_key(schedule, 2), READY_BEFORE_PLAYBACK)
    _watch(manager, 1, clock)
    clock.value = schedule.items[1].unlock_at_utc + 30
    manager._notify_ready_episode(video=videos[1], media_id=MEDIA_ID, episode=2)
    assert len(sent) == 1 and "episode 2" in sent[0][1].lower()
