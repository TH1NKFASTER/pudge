"""XDEV: watch ~half an episode on the iPad, finish it on the Mac in mpv (or the reverse).

1. The iPad sent its place only when the player closed (nothing while playing, nothing when the
   app went to the background), and the Mac's Home kept a cached snapshot without the episode
   under Continue Watching: the Mac did not know where to continue.
2. Neither device alone saw 65 % of the episode, so finishing it never counted it as watched.
   Now the parts played on each device are joined (overlaps once) and the usual rule is checked
   against the union, once, from whichever device reaches the end.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import cli, watch_coverage
from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.config import AppConfig
from pudge.database import Database
from pudge.playback_save import save_playback

DURATION = 1440.0


# ------------------------------------------------------------------ the union itself


def test_union_counts_overlaps_once_and_retries_add_nothing():
    parts = [(0, 600), (500, 700), (700.2, 800), (1000, 1100)]
    assert watch_coverage.merge(parts) == [(0, 800), (1000, 1100)]
    assert watch_coverage.covered_seconds(parts) == 900
    # The same parts sent again (a retry, the next periodic send) change nothing.
    assert watch_coverage.covered_seconds(parts + parts) == 900
    # Clipped to the episode; nonsense skipped.
    assert watch_coverage.merge([(-5, 10), (1400, 2000)], duration=DURATION) == [(0, 10), (1400, DURATION)]
    assert watch_coverage.parse_ranges([[1, 2], "x", [3], [5, 4], [float("nan"), 9], [7, 9]]) == [
        (1, 2),
        (7, 9),
    ]


def test_stored_parts_are_bounded_to_the_longest_ones():
    parts = [(index * 3.0, index * 3.0 + 1.0) for index in range(500)] + [(5000.0, 5600.0)]
    merged = watch_coverage.merge(parts, cap=10)
    assert len(merged) == 10 and (5000.0, 5600.0) in merged
    assert merged == sorted(merged)


def test_mpv_heartbeat_part_never_reaches_back_past_the_previous_place():
    assert watch_coverage.mpv_interval(600.0, 615.0, 15.0) == (600.0, 615.0)
    # Paused part of the time (5 s played): only those.
    assert watch_coverage.mpv_interval(600.0, 615.0, 5.0) == (610.0, 615.0)
    # A seek back: what was played up to the new place.
    assert watch_coverage.mpv_interval(900.0, 315.0, 15.0) == (300.0, 315.0)
    assert watch_coverage.mpv_interval(None, 10.0, 15.0) == (0.0, 10.0)
    assert watch_coverage.mpv_interval(600.0, 615.0, 0.0) is None


# ------------------------------------------------------------------ the Mac and the iPad


class _AniList:
    calls: list[tuple[int, int]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def update_progress(self, media_id, progress, *_args, **_kwargs):
        _AniList.calls.append((media_id, progress))
        return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

    def close(self) -> None:
        pass


@pytest.fixture
def mac(tmp_path, monkeypatch):
    from pudge.iphone_service import IPhoneService

    _AniList.calls = []
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True
    db = Database(cfg.library.database_path)
    video = (tmp_path / "Show - 02.mkv").resolve()
    video.write_bytes(b"x")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,updated_at) VALUES(5,'Show','CURRENT',1,0)"
        )
        episode = conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) VALUES(5,'Show',2,?,'ready',0)",
            (str(video),),
        ).lastrowid
    service = IPhoneService(db, tmp_path / "mobile")
    service.api = SimpleNamespace(
        manager=SimpleNamespace(db=db),
        config=SimpleNamespace(
            anilist=SimpleNamespace(
                access_token="t",
                endpoint="e",
                add_if_missing=False,
                update_when_rewatching=False,
                completed_to_rewatching_on_episode_one=False,
                complete_current_final=True,
                complete_rewatching_final=True,
            ),
            agent=SimpleNamespace(delete_after_watched_hours=24),
        ),
    )
    monkeypatch.setattr("pudge.providers.anilist.AniListClient", _AniList)
    monkeypatch.setattr(cli, "AniListClient", _AniList)
    return SimpleNamespace(cfg=cfg, db=db, video=video, episode=int(episode), service=service, tmp=tmp_path)


def _mpv_plays(mac, start: float, end: float, *, step: float = 15.0) -> None:
    """mpv opened at ``start`` and played to ``end`` (a heartbeat every ``step`` seconds)."""
    save_playback(mac.db, mac.video, position=start, duration=DURATION, active_seconds=0)
    position = start
    while position < end:
        position = min(end, position + step)
        save_playback(mac.db, mac.video, position=position, duration=DURATION, active_seconds=step)


def _mpv_reaches_watched_point(mac) -> int:
    tracking = create_tracking_file(
        mac.tmp / "cache",
        TrackingPayload(
            video=str(mac.video),
            title="Show",
            media_id=5,
            episode=2,
            total_episodes=12,
            threshold=5 / 6,
            mapping_key="show",
        ),
    )
    args = SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False)
    return cli._run_anilist_action(args, mac.cfg)


def _ipad_place(mac, seconds: float, ranges, *, observed: float | None = None):
    value = {"seconds": seconds, "duration": DURATION, "ranges": ranges}
    if observed is not None:
        value.update(observedAt=observed, sentAt=time.time())
    return mac.service.media.episode_position(mac.episode, value)


def _state(mac) -> str:
    with mac.db.connect() as conn:
        return conn.execute("SELECT state FROM episodes WHERE id=?", (mac.episode,)).fetchone()[0]


def test_ipad_first_half_then_mac_finishes_counts_once(mac):
    # 10 min on the iPad, sent while playing.
    assert _ipad_place(mac, 600, [[0, 600]]) == {"ok": True}
    # mpv opens there and plays the rest (8 min of its own: not enough alone).
    _mpv_plays(mac, 600, 1100)
    assert _mpv_reaches_watched_point(mac) == 0
    assert _AniList.calls == [(5, 2)]
    assert _state(mac) == "watched"
    # The parts are gone with the count: a rewatch starts from nothing.
    assert watch_coverage.coverage(mac.db, str(mac.video)) == 0


def test_mac_alone_with_the_same_seconds_is_still_not_enough(mac):
    _mpv_plays(mac, 600, 1100)
    assert _mpv_reaches_watched_point(mac) == 3
    assert _AniList.calls == [] and _state(mac) == "ready"


def test_overlap_on_both_devices_counts_once(mac):
    # The iPad saw 0–600, the Mac rewatched 300–900: 900 s, not 1200 (needs 936).
    _ipad_place(mac, 600, [[0, 600]])
    _mpv_plays(mac, 300, 900)
    assert watch_coverage.coverage(mac.db, str(mac.video)) == pytest.approx(900)
    assert _mpv_reaches_watched_point(mac) == 3
    assert _AniList.calls == []


def test_mac_first_half_then_ipad_finishes_counts_there_once(mac):
    _mpv_plays(mac, 0, 600)
    # The iPad continues at 10:00; before the end nothing counts.
    assert _ipad_place(mac, 900, [[600, 900]]) == {"ok": True}
    assert _AniList.calls == []
    # Past 85 % with the parts of both devices: counted like a watch, once.
    result = _ipad_place(mac, 1300, [[600, 1300]])
    assert result == {"ok": True, "counted": True}
    assert _AniList.calls == [(5, 2)] and _state(mac) == "watched"
    # The next periodic send / a retry does not count it again.
    assert "counted" not in _ipad_place(mac, 1310, [[600, 1310]])
    assert (
        mac.service.media.episode_watched(mac.episode, {"duration": DURATION, "activeSeconds": 710})[
            "anilist"
        ]
        == "already_counted"
    )
    assert _AniList.calls == [(5, 2)]


def test_ipad_parts_sent_late_count_after_the_mac_reached_the_end(mac):
    now = time.time()
    # The iPad watched the first half offline an hour ago; meanwhile the Mac played the rest
    # and reached its watched point: not enough alone (deferred).
    _mpv_plays(mac, 700, 1300)
    assert _mpv_reaches_watched_point(mac) == 3
    # The iPad syncs now: its place is older than the Mac's (kept out), its parts still count.
    result = _ipad_place(mac, 700, [[0, 700]], observed=now - 3600)
    assert result == {"ok": True, "counted": True}
    assert _AniList.calls == [(5, 2)] and _state(mac) == "watched"


def test_iphone_watched_mark_uses_the_union(mac):
    _mpv_plays(mac, 0, 700)
    # The phone played 700–1100 (sent with its place) and reports its 400 s: with the Mac's
    # 700 s, counted.
    assert _ipad_place(mac, 1100, [[700, 1100]]) == {"ok": True}
    result = mac.service.media.episode_watched(mac.episode, {"duration": DURATION, "activeSeconds": 400})
    assert result["ok"] is True and _AniList.calls == [(5, 2)]


def test_a_watched_episode_takes_no_parts(mac):
    with mac.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched',watched_at=? WHERE id=?", (time.time(), mac.episode)
        )
    _mpv_plays(mac, 0, 600)
    _ipad_place(mac, 1300, [[600, 1300]])
    assert watch_coverage.coverage(mac.db, str(mac.video)) == 0
    assert _AniList.calls == []


def test_ipad_place_puts_the_episode_in_continue_watching_on_the_mac_home(mac):
    with mac.db.connect() as conn:
        conn.execute(
            "INSERT INTO state(key,value,updated_at) VALUES('ui_state_version','7',0) "
            "ON CONFLICT(key) DO UPDATE SET value='7'"
        )
    assert _ipad_place(mac, 600, None) == {"ok": True}
    assert mac.db.get_state("ui_state_version") == "8"
    # Further on, still resumable: the Home is not rebuilt on every send.
    _ipad_place(mac, 615, None)
    assert mac.db.get_state("ui_state_version") == "8"
    assert [str(item.video_path) for item in mac.db.resumable_episodes()] == [str(mac.video)]


def test_parts_of_an_abandoned_watch_expire(mac):
    old = time.time() - watch_coverage.EXPIRE_SECONDS - 60
    watch_coverage.record(mac.db, str(mac.video), [(0, 900)], duration=DURATION, now=old)
    assert watch_coverage.coverage(mac.db, str(mac.video)) == 0


def test_cli_playback_save_records_the_part_too(mac):
    base = dict(playback_video=Path(mac.video), playback_duration=DURATION)
    cli._run_playback_save(
        SimpleNamespace(**base, playback_position=100.0, playback_active_seconds=0.0), mac.cfg
    )
    cli._run_playback_save(
        SimpleNamespace(**base, playback_position=115.0, playback_active_seconds=15.0), mac.cfg
    )
    assert watch_coverage.load(mac.db, str(mac.video))["intervals"] == [(100.0, 115.0)]
