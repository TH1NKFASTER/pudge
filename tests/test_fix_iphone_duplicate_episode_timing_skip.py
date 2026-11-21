"""iPad: one item per episode number, subtitle timing shared with mpv, OP/ED segments."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.intro_skipper import ALGORITHM_VERSION, IntroSkipperCache, identity_of
from pudge.playback_save import save_playback
from pudge.subtitle_delay import remembered_subtitle_delay
from pudge.subtitle_formats import clean_srt_for_playback

SRT = "1\n00:00:01,000 --> 00:00:02,000\nうん\n"


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _api(tmp_path, *, skip=True):
    return SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
            playback=SimpleNamespace(skip_segments_enabled=skip),
        )
    )


def _row(service, path: Path, number: int, *, media_id: int = 99426) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    subtitle = path.with_suffix(".ja.srt")
    subtitle.write_text(SRT, encoding="utf-8")
    with service.db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO anime(media_id,title,status,progress,updated_at) "
            "VALUES(?,'Sora yori mo Tooi Basho','CURRENT',0,0)",
            (media_id,),
        )
        return conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) "
            "VALUES(?,'Ep',?,?,?,'ready',0)",
            (media_id, number, str(path), str(subtitle)),
        ).lastrowid


def _library(service, tmp_path):
    """An older single download of episode 2 (inserted first) and a BD batch of 1-13."""
    single = _row(service, tmp_path / "Sora yori mo Tooi Basho" / "[HorribleSubs] Sora - 02 [1080p].mkv", 2)
    batch = {
        number: _row(
            service, tmp_path / "Tooi Basho S1 (BD 1080p)" / f"[MegaMTBB] Sora - {number:02d}.mkv", number
        )
        for number in range(1, 14)
    }
    return single, batch


def test_phone_list_offers_one_item_per_episode_the_batch_copy(service, tmp_path):
    single, batch = _library(service, tmp_path)
    service.api = _api(tmp_path)
    items = service.media.episodes()["items"]
    assert [item["number"] for item in items] == list(range(1, 14))
    assert [item["id"] for item in items if item["number"] == 2] == [batch[2]]
    # The hidden copy still answers for a phone that downloaded it before.
    assert service.media._episode_row(single)["id"] == single


def test_mac_ready_episode_picks_the_same_copy(service, tmp_path):
    _single, batch = _library(service, tmp_path)
    chosen = service.db.ready_episode(99426, 2)
    assert chosen is not None and "MegaMTBB" in chosen.video_path.name
    assert service.db.episode_by_path(chosen.video_path) is not None and batch[2]


def test_a_copy_on_disk_wins_over_a_missing_one(service, tmp_path):
    _single, batch = _library(service, tmp_path)
    (tmp_path / "Tooi Basho S1 (BD 1080p)" / "[MegaMTBB] Sora - 02.mkv").unlink()
    chosen = service.db.ready_episode(99426, 2)
    assert chosen is not None and "HorribleSubs" in chosen.video_path.name


def test_without_duplicates_the_list_is_unchanged(service, tmp_path):
    ids = [_row(service, tmp_path / "s" / f"{n}.mkv", n) for n in (1, 2, 3)]
    service.api = _api(tmp_path)
    assert [item["id"] for item in service.media.episodes()["items"]] == ids


def test_phone_timing_is_stored_and_reaches_mpv_launch_of_the_cleaned_copy(service, tmp_path):
    episode = _row(service, tmp_path / "show" / "01.mkv", 1)
    service.api = _api(tmp_path)
    assert service.media.episodes()["items"][0]["subtitleDelayMs"] == 0
    answer = service.media.handle(
        "POST", f"/api/v2/media/episodes/{episode}/subtitle-delay", {}, lambda: {"ms": 1300}
    )
    assert answer == {"ok": True, "ms": 1300}
    assert service.media.episodes()["items"][0]["subtitleDelayMs"] == 1300
    # mpv plays a cleaned copy of the same subtitle: the launch takes the phone's delay.
    video = (tmp_path / "show" / "01.mkv").resolve()
    subtitle = tmp_path / "show" / "01.ja.srt"
    cleaned, _ = clean_srt_for_playback(subtitle, tmp_path / "cache")
    assert cleaned != subtitle
    row = service.db.episode_by_path(video)
    from pudge.playback_save import _library_subtitle_source

    source = _library_subtitle_source(row, str(cleaned), None, tmp_path / "cache")
    assert source
    assert remembered_subtitle_delay(service.db, video, subtitle=cleaned, source=source) == 1.3
    # Another subtitle mpv may play is not given it.
    other = tmp_path / "other.srt"
    other.write_text(SRT, encoding="utf-8")
    assert _library_subtitle_source(row, str(other), None, tmp_path / "cache") == ""
    # Zero from the phone forgets it.
    service.media.episode_subtitle_delay(episode, {"ms": 0})
    assert service.media.episodes()["items"][0]["subtitleDelayMs"] == 0


def test_mpv_delay_on_the_cleaned_copy_reaches_the_phone(service, tmp_path):
    episode = _row(service, tmp_path / "show" / "01.mkv", 1)
    service.api = _api(tmp_path)
    video = (tmp_path / "show" / "01.mkv").resolve()
    cleaned, _ = clean_srt_for_playback(tmp_path / "show" / "01.ja.srt", tmp_path / "cache")
    save_playback(
        service.db,
        video,
        position=10,
        duration=1400,
        sub_delay=-0.7,
        subtitle=str(cleaned),
        cache_dir=tmp_path / "cache",
    )
    items = service.media.episodes()["items"]
    assert [(item["id"], item["subtitleDelayMs"]) for item in items] == [(episode, -700)]


def test_mpv_delay_without_cache_dir_stays_mpv_only(service, tmp_path):
    _row(service, tmp_path / "show" / "01.mkv", 1)
    service.api = _api(tmp_path)
    video = (tmp_path / "show" / "01.mkv").resolve()
    cleaned, _ = clean_srt_for_playback(tmp_path / "show" / "01.ja.srt", tmp_path / "cache")
    save_playback(service.db, video, position=10, duration=1400, sub_delay=0.5, subtitle=str(cleaned))
    assert service.media.episodes()["items"][0]["subtitleDelayMs"] == 0
    assert remembered_subtitle_delay(service.db, video, subtitle=cleaned) == 0.5


def test_bad_timing_is_refused(service, tmp_path):
    from pudge.iphone_media import MediaError

    episode = _row(service, tmp_path / "show" / "01.mkv", 1)
    service.api = _api(tmp_path)
    with pytest.raises(MediaError):
        service.media.episode_subtitle_delay(episode, {"ms": "soon"})


def _segments(tmp_path, video: Path) -> None:
    identity = identity_of(video)
    IntroSkipperCache(tmp_path / "cache").write_result(
        identity,
        {
            "status": "found",
            "algorithm": ALGORITHM_VERSION,
            "segments": [
                {"kind": "intro", "start_seconds": 60.0, "end_seconds": 149.5, "confidence": 0.9},
                {"kind": "outro", "start_seconds": 1300.0, "end_seconds": 1390.0, "confidence": 0.8},
            ],
        },
    )


def test_episode_items_carry_detected_opening_and_ending(service, tmp_path):
    _row(service, tmp_path / "show" / "01.mkv", 1)
    _row(service, tmp_path / "show" / "02.mkv", 2)
    _segments(tmp_path, tmp_path / "show" / "01.mkv")
    service.api = _api(tmp_path)
    items = service.media.episodes()["items"]
    assert items[0]["skipSegments"] == [
        {"kind": "intro", "start": 60.0, "end": 149.5},
        {"kind": "outro", "start": 1300.0, "end": 1390.0},
    ]
    assert items[1]["skipSegments"] == []
    json.dumps(items)  # plain JSON for the phone


def test_skip_segments_follow_the_mac_setting(service, tmp_path):
    _row(service, tmp_path / "show" / "01.mkv", 1)
    _segments(tmp_path, tmp_path / "show" / "01.mkv")
    service.api = _api(tmp_path, skip=False)
    assert service.media.episodes()["items"][0]["skipSegments"] == []
