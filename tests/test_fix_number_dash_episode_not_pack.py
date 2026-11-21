"""A title that ends in a number, then " - NN", is one episode, not a pack.

"[SubsPlease] Kono Subarashii Sekai ni Shukufuku wo! 3 - 05" matched the old bare
"N - M" pack pattern, so every release flagged is_batch=True (parse_rss, SubsPlease,
Shana and SeaDex all set it from the title) and the episode search gave it
-500 "episode-pack": automatic download found nothing for KonoSuba 3, SubsPlease was
never picked for Kaiju No. 8, and "Mob Psycho 100 - 07" / "86 - 05" were dropped
before ranking. The releases here are built by parse_rss, as in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from pudge.providers import nyaa

GiB_TEXT = "1.4 GiB"


def rss(items: list[tuple[str, str, int, bool]]) -> str:
    published = format_datetime(datetime.now(timezone.utc) - timedelta(hours=30))
    out = ['<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0"><channel>']
    for index, (title, size, seeders, trusted) in enumerate(items):
        info_hash = format(abs(hash(title)), "040x")[-40:]
        out.append(
            f"<item><title>{escape(title)}</title><link>https://nyaa.example/download/{index}.torrent</link>"
            f"<guid>https://nyaa.example/view/{index}</guid><pubDate>{published}</pubDate>"
            f"<nyaa:seeders>{seeders}</nyaa:seeders><nyaa:leechers>3</nyaa:leechers>"
            f"<nyaa:downloads>500</nyaa:downloads><nyaa:infoHash>{info_hash}</nyaa:infoHash>"
            f"<nyaa:categoryId>1_2</nyaa:categoryId><nyaa:size>{size}</nyaa:size>"
            f"<nyaa:trusted>{'Yes' if trusted else 'No'}</nyaa:trusted><nyaa:remake>No</nyaa:remake></item>"
        )
    out.append("</channel></rss>")
    return "".join(out)


class FeedClient(nyaa.NyaaClient):
    def __init__(self, xml: str):
        super().__init__("https://nyaa.example")
        self.xml = xml
        self.queries: list[str] = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        return nyaa.parse_rss(self.xml)


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager._storage_can_accept = lambda _size: True
    manager._release_feeds_configured = lambda: False
    return manager


def search(manager: AnimeManager, anime: LibraryAnime, items, *, episode=None, batch=False):
    manager.db.upsert_anime(anime)
    client = FeedClient(rss(items))
    manager.nyaa_client = lambda timeout=20.0: client
    nyaa._MANUAL_QUERY_CACHE.clear()
    return manager.search_releases(
        anime.media_id, episode=episode, batch=batch, automatic=True, allow_anilist_network=False
    )


KONOSUBA3 = LibraryAnime(
    media_id=136804,
    title="Kono Subarashii Sekai ni Shukufuku wo! 3",
    titles=[
        "Kono Subarashii Sekai ni Shukufuku wo! 3",
        "KONOSUBA -God's blessing on this wonderful world! 3",
    ],
    synonyms=["KonoSuba 3"],
    episodes=11,
    format="TV",
    media_status="FINISHED",
    duration=24,
    relations=[
        {"relation_type": "PREQUEL", "media_id": 21699, "title": "Kono Subarashii Sekai ni Shukufuku wo! 2"}
    ],
)
KAIJU = LibraryAnime(
    media_id=163132,
    title="Kaijuu 8-gou",
    titles=["Kaijuu 8-gou", "Kaiju No. 8"],
    synonyms=["Kaiju No.8"],
    episodes=12,
    format="TV",
    media_status="FINISHED",
    duration=24,
    relations=[],
)
EDGERUNNERS2 = LibraryAnime(
    media_id=195539,
    title="Cyberpunk: Edgerunners 2",
    titles=["Cyberpunk: Edgerunners 2"],
    synonyms=["Cyberpunk: Edgerunners II"],
    episodes=10,
    format="ONA",
    relations=[],
)
MOB = LibraryAnime(
    media_id=21507,
    title="Mob Psycho 100",
    titles=["Mob Psycho 100", "モブサイコ100"],
    episodes=12,
    format="TV",
    media_status="FINISHED",
    duration=24,
    relations=[{"relation_type": "SEQUEL", "media_id": 101338, "title": "Mob Psycho 100 II"}],
)
E86 = LibraryAnime(
    media_id=116589,
    title="86: Eighty Six",
    titles=["86 -Eighty Six-"],
    synonyms=["86"],
    episodes=11,
    relations=[],
)


@pytest.mark.parametrize(
    "anime, episode, title",
    [
        (KONOSUBA3, 5, "[SubsPlease] Kono Subarashii Sekai ni Shukufuku wo! 3 - 05 (1080p) [6A1B2C3D].mkv"),
        (
            KONOSUBA3,
            5,
            "[Erai-raws] Kono Subarashii Sekai ni Shukufuku wo! 3 - 05 [1080p CR WEB-DL AVC AAC][MultiSub]",
        ),
        (KONOSUBA3, 1, "[SubsPlease] Kono Subarashii Sekai ni Shukufuku wo! 3 - 01 (1080p) [6A1B2C3D].mkv"),
        (KAIJU, 5, "[SubsPlease] Kaiju No. 8 - 05 (1080p) [ABCD1234].mkv"),
        (KAIJU, 9, "[SubsPlease] Kaiju No. 8 - 09 (1080p) [ABCD1234].mkv"),
        (
            EDGERUNNERS2,
            1,
            "[Erai-raws] Cyberpunk: Edgerunners 2 - 01 [1080p NF WEB-DL AVC EAC3][MultiSub][7A3F2C1D]",
        ),
        (
            EDGERUNNERS2,
            3,
            "[Erai-raws] Cyberpunk: Edgerunners 2 - 03 [1080p NF WEB-DL AVC EAC3][MultiSub][7A3F2C1D]",
        ),
        (MOB, 7, "[Erai-raws] Mob Psycho 100 - 07 [1080p]"),
        (E86, 5, "[Erai-raws] 86 - 05 [1080p][Multiple Subtitle]"),
    ],
)
def test_title_number_then_episode_is_auto_downloadable(tmp_path: Path, anime, episode, title):
    manager = make_manager(tmp_path)
    found = search(manager, anime, [(title, GiB_TEXT, 300, True)], episode=episode)
    assert [item.title for item in found] == [title]
    (item,) = found
    assert "episode-pack" not in item.reasons, item.reasons
    assert manager._release_is_allowed_for_auto(item), (item.score, item.reasons)


def test_own_release_outranks_the_scene_one(tmp_path: Path):
    erai = "[Erai-raws] Cyberpunk: Edgerunners 2 - 01 [1080p NF WEB-DL AVC EAC3][MultiSub][7A3F2C1D]"
    scene = "[ToonsHub] Cyberpunk Edgerunners S02E01 1080p NF WEB-DL DUAL DDP5.1 H.264 (Multi-Subs)"
    manager = make_manager(tmp_path)
    found = search(
        manager, EDGERUNNERS2, [(erai, GiB_TEXT, 300, True), (scene, "1.5 GiB", 400, False)], episode=1
    )
    assert [item.title for item in found][0] == erai


@pytest.mark.parametrize(
    "title",
    [
        "[Erai-raws] Mob Psycho 100 - 01 ~ 12 [1080p][Batch]",
        "[SubsPlease] Mob Psycho 100 (01-12) (1080p) [Batch]",
        "[Grp] 86 - 01-11 [1080p]",
    ],
)
def test_real_batches_stay_packs_for_an_episode_search(tmp_path: Path, title):
    assert nyaa.release_title_is_pack(title)
    assert nyaa.parse_rss(rss([(title, "14 GiB", 100, True)]))[0].is_batch
    manager = make_manager(tmp_path)
    anime = E86 if "86" in title else MOB
    found = search(manager, anime, [(title, "14 GiB", 100, True)], episode=5)
    assert all(not manager._release_is_allowed_for_auto(item) for item in found)
    for item in found:
        assert "episode-pack" in item.reasons


def test_parsers_flag_only_real_packs():
    assert not nyaa.release_title_is_pack("[HorribleSubs] Mob Psycho 100 - 07 [1080p].mkv")
    assert not nyaa.release_title_is_pack("[SubsPlease] Kaiju No. 8 - 05 (1080p) [ABCD1234].mkv")
    assert nyaa.release_title_is_pack("[Erai-raws] Show - 01 ~ 12 [1080p][Batch]")
    assert nyaa.release_title_is_pack("[Grp] Show Complete [1080p]")
    sub = (
        "<rss><channel><item><title>[SubsPlease] Mob Psycho 100 - 07 (1080p)</title>"
        "<link>magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567</link><guid>g</guid>"
        "<description>1.4 GiB</description></item></channel></rss>"
    )
    assert nyaa.parse_subsplease_rss(sub)[0].is_batch is False


def test_shana_feed_keeps_a_numbered_sequel_episode():
    title = "[SubsPlease] Kono Subarashii Sekai ni Shukufuku wo! 3 - 05 (1080p) [6A1B2C3D].mkv"
    release = nyaa.parse_rss(rss([(title, GiB_TEXT, 300, True)]))[0]
    assert release.is_batch  # "3 - 05" is an ascending pair: only the entry tells it apart

    class _Shana:
        def releases(self):
            return [release]

    found = nyaa.search_shana_ranked(
        _Shana(),
        KONOSUBA3,
        episode=5,
        batch=False,
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
    )
    assert [item.title for item in found] == [title]
    assert "episode-pack" not in found[0].reasons


def test_pair_in_the_name_before_the_episode_is_no_range(tmp_path: Path):
    ranma = LibraryAnime(
        media_id=178533,
        title="Ranma 1/2 (2024)",
        titles=["Ranma 1/2 (2024)", "Ranma1/2"],
        synonyms=["Ranma ½ (2024)"],
        episodes=12,
        format="TV",
        relations=[],
    )
    erai = "[Erai-raws] Ranma 1-2 (2024) - 05 [1080p NF WEB-DL AVC EAC3][MultiSub][ABCDEF12]"
    assert nyaa.release_episode_range(erai) is None
    assert not nyaa.release_title_is_pack(erai)
    assert nyaa.release_episode_range("[Erai-raws] Ranma 1-2 (2024) - 01 ~ 12 [1080p][Batch]") == (1, 12)
    assert nyaa.release_episode_range("[Grp] Show 01-12 - 2019 [1080p]") == (1, 12)
    manager = make_manager(tmp_path)
    found = search(manager, ranma, [(erai, GiB_TEXT, 100, True)], episode=5)
    assert [item.title for item in found] == [erai]
    assert manager._release_is_allowed_for_auto(found[0]), found[0].reasons
