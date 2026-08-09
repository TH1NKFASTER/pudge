from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, NyaaRelease
from anime_mpv.providers.nyaa import (
    fresh_trusted_zero_seeders_allowed,
    parse_rss,
    score_release,
)


def _release(*, group: str, published: str, seeders: int = 0) -> NyaaRelease:
    return NyaaRelease(
        title=(
            f"[{group}] From Old Country Bumpkin to Master Swordsman "
            "S02E05 1080p AMZN WEB-DL MULTi DDP2.0 H.264"
        ),
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="a" * 40,
        size_text="1.4 GiB",
        size_bytes=1400 * 1024**2,
        seeders=seeders,
        leechers=0,
        downloads=0,
        trusted=False,
        remake=False,
        category_id="1_2",
        published=published,
        group=group,
        score=200.0,
        reasons=["ep=5"],
    )


def test_parse_rss_keeps_prefix_group_and_extracts_suffix_group() -> None:
    xml = """<?xml version="1.0"?>
<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>
<item><title>[ToonsHub] Example S02E05 1080p</title><link>x</link><guid>x</guid>
<pubDate>Wed, 05 Aug 2026 17:00:00 +0000</pubDate><nyaa:seeders>0</nyaa:seeders>
<nyaa:infoHash>AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA</nyaa:infoHash></item>
<item><title>Example S03E05 1080p CR WEB-DL AAC2.0 H.264-VARYG (Example)</title>
<link>y</link><guid>y</guid><pubDate>Wed, 05 Aug 2026 17:00:00 +0000</pubDate>
<nyaa:seeders>0</nyaa:seeders><nyaa:infoHash>BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB</nyaa:infoHash></item>
</channel></rss>"""

    releases = parse_rss(xml)

    assert [item.group for item in releases] == ["ToonsHub", "VARYG"]


def test_fresh_trusted_zero_seeders_is_allowed_for_24_hours() -> None:
    now = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
    release = _release(
        group="ToonsHub",
        published=format_datetime(now - timedelta(hours=6)),
    )

    assert fresh_trusted_zero_seeders_allowed(
        release,
        ["ToonsHub"],
        now=now,
    )


def test_old_or_untrusted_zero_seeder_release_is_not_allowed() -> None:
    now = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
    old = _release(
        group="ToonsHub",
        published=format_datetime(now - timedelta(hours=25)),
    )
    untrusted = _release(
        group="Unknown",
        published=format_datetime(now - timedelta(hours=1)),
    )

    assert not fresh_trusted_zero_seeders_allowed(old, ["ToonsHub"], now=now)
    assert not fresh_trusted_zero_seeders_allowed(untrusted, ["ToonsHub"], now=now)


def test_scoring_does_not_penalize_fresh_trusted_zero_seeders() -> None:
    release = _release(
        group="ToonsHub",
        published=format_datetime(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    anime = LibraryAnime(
        media_id=194829,
        title="Katainaka no Ossan, Kensei ni Naru II",
        titles=["From Old Country Bumpkin to Master Swordsman Season 2"],
        episodes=12,
        duration=24,
    )

    scored = score_release(
        release,
        anime,
        episode=5,
        batch=False,
        trusted_groups=["ToonsHub"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
    )

    assert "fresh-trusted-zero-seeders" in scored.reasons
    assert "no-seeders" not in scored.reasons


def test_manager_accepts_fresh_trusted_release_with_zero_reported_seeders() -> None:
    release = _release(
        group="ToonsHub",
        published=format_datetime(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.nyaa.auto_require_trusted = True
    manager.config.nyaa.trusted_groups = ["ToonsHub"]
    manager.config.nyaa.min_seeders = 1
    manager.config.nyaa.min_release_score = 50
    manager.logger = logging.getLogger("test-fresh-trusted-zero-seeders")
    manager._storage_can_accept = lambda _size: True

    assert manager._release_is_allowed_for_auto(release)
