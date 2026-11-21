"""IOS-13: an episode place from the phone was stored with the time it arrived on the Mac. A place
sent late (watched offline, synced later) then counted as newer than a later place of another
device, which was dropped as "older" — the bug already fixed for light novels and manga. Now an
episode place keeps the time it was watched (never earlier than the place it replaces)."""

from __future__ import annotations

import time

from pudge.database import Database
from pudge.iphone_service import IPhoneService


def _episode(tmp_path):
    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    with service.db.connect() as conn:
        episode = conn.execute(
            "INSERT INTO episodes(video_path,title,episode,state,updated_at) VALUES(?,'Show',1,'ready',0)",
            (str(tmp_path / "Show - 01.mkv"),),
        ).lastrowid
    return service, int(episode)


def _place(service, episode):
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT playback_position,playback_updated_at FROM episodes WHERE id=?", (episode,)
        ).fetchone()
    return float(row[0]), float(row[1])


def test_an_episode_place_keeps_the_time_it_was_watched(tmp_path):
    service, episode = _episode(tmp_path)
    now = time.time()
    # The iPad watched to 10:00 five minutes ago, offline; it syncs only now.
    assert service.media.episode_position(
        episode, {"seconds": 600, "duration": 1440, "observedAt": now - 300}
    ) == {"ok": True}
    position, stamp = _place(service, episode)
    assert position == 600 and abs(stamp - (now - 300)) < 1
    # The iPhone rewound to 1:40 a minute after that: the later place wins.
    assert service.media.episode_position(
        episode, {"seconds": 100, "duration": 1440, "observedAt": now - 240}
    ) == {"ok": True}
    assert _place(service, episode)[0] == 100
    # A place really watched earlier than the stored one is still kept out.
    late = {"seconds": 50, "duration": 1440, "observedAt": now - 280}
    assert service.media.episode_position(episode, late) == {"ok": True, "kept": "newer"}


def test_an_older_app_without_a_time_keeps_the_macs_save_time(tmp_path):
    service, episode = _episode(tmp_path)
    before = time.time()
    service.media.episode_position(episode, {"seconds": 30, "duration": 1440})
    assert _place(service, episode)[1] >= before
