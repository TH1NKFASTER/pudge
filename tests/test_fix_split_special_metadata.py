"""A season-0 special / OVA split from its series into its own AniList entry has its own
metadata: cover, AniList page, episodes.

Maintainer (Baccano!): "[DeeVeeDee] Baccano - S00E01 - Episode 14 - OVA 1" .. S00E03 went to
"Baccano! Bangai-hen" (AniList 3901, off the list). Its row was made from the relation node
without the cover the node carries, and the list sync never refreshes off-list rows: Home
showed the "Cover" placeholder under "Waiting for subs" for days. Jimaku's "Baccano!
Specials" entry had 12 files, none of them numbered 1-3 (the DVDs number them 14-16).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pudge import extra_files
from pudge.cli import _season_zero_series_numbers
from pudge.manager_models import LibraryAnime
from pudge.providers.jimaku import JimakuClient, JimakuFile
from pudge.filename import parse_anime_filename
from test_web_app import make_api

TV, SPECIALS = 2251, 3901
TV_COVER = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx2251-tv.jpg"
OVA_COVER = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx3901-ova.jpg"
FOLDER = "[DeeVeeDee] Baccano! [DVD 480p HEVC Opus Dual-Audio]"


def _ova_name(n: int) -> str:
    return (
        f"[DeeVeeDee] Baccano - S00E{n:02d} - Episode {13 + n} - OVA {n} "
        f"[DVD 480p HEVC Opus Dual-Audio] [E1FD25{n:02d}].mkv"
    )


def _node(cover: str = OVA_COVER) -> dict:
    return {
        "relation_type": "SIDE_STORY",
        "media_id": SPECIALS,
        "title": "Baccano! Bangai-hen",
        "site_url": f"https://anilist.co/anime/{SPECIALS}",
        "format": "SPECIAL",
        "episodes": 3,
        "cover_url": cover,
        "media_status": "FINISHED",
        "start_date": "2008-04-01",
    }


def _series(relations: list[dict]) -> LibraryAnime:
    return LibraryAnime(
        media_id=TV,
        title="Baccano!",
        cover_url=TV_COVER,
        status="CURRENT",
        progress=13,
        media_status="FINISHED",
        episodes=13,
        format="TV",
        relations=relations,
    )


def _library(tmp_path: Path, node: dict):
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(_series([node]))
    folder = api.config.library.root_dir / "Baccano!" / FOLDER
    folder.mkdir(parents=True)
    for n in (1, 2, 3):
        (folder / _ova_name(n)).write_bytes(b"video " + bytes([n]))
    return api, folder


def test_scan_gives_the_split_specials_entry_its_own_cover(tmp_path: Path) -> None:
    api, folder = _library(tmp_path, _node())
    api.manager.scan_library()
    assert {
        row.media_id for row in api.manager.db.episodes() if row.video_path.parent == folder.resolve()
    } == {SPECIALS}
    special = api.manager.db.get_anime(SPECIALS)
    # Before: cover_url "" and site_url "" (the node's cover was dropped).
    assert special.cover_url == OVA_COVER
    assert special.site_url == f"https://anilist.co/anime/{SPECIALS}"
    assert special.title == "Baccano! Bangai-hen" and special.episodes == 3
    payload = api._anime_payload(special, relations=False)
    assert payload["cover_remote"] == OVA_COVER


def test_an_entry_without_an_anilist_cover_shows_the_series_cover(tmp_path: Path) -> None:
    api, _folder = _library(tmp_path, _node(cover=""))
    api.manager.scan_library()
    assert api.manager.db.get_anime(SPECIALS).cover_url == TV_COVER


def test_rows_made_before_get_their_cover_from_the_cached_relation_graph(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(_series([_node()]))
    # The row an older Pudge made: no cover, no AniList page.
    db.upsert_anime(LibraryAnime(SPECIALS, "Baccano! Bangai-hen", status="", episodes=3, format="SPECIAL"))
    graph = {
        "nodes": [
            {
                **_node(),
                "relation_type": "SELF",
                "media_id": TV,
                "title": "Baccano!",
                "format": "TV",
                "cover_url": TV_COVER,
                "episodes": 13,
            },
            _node(),
        ],
        "edges": [{"source": TV, "target": SPECIALS, "relation_type": "SIDE_STORY"}],
    }
    db.store_relation_graph(graph, refreshed_at=1.0, next_refresh_at=2.0)
    api.manager.cover_path = lambda anime, **_k: None  # no network
    assert api.manager.refresh_off_list_entry_metadata() == 1
    special = db.get_anime(SPECIALS)
    assert special.cover_url == OVA_COVER and special.site_url.endswith(f"/{SPECIALS}")
    assert api.manager.refresh_off_list_entry_metadata() == 0  # nothing left to change

    # AniList had no image for the entry: the series' cover, replaced once AniList has one.
    db.upsert_anime(LibraryAnime(SPECIALS, "Baccano! Bangai-hen", status="", episodes=3, format="SPECIAL"))
    graph["nodes"][1] = _node(cover="")
    db.store_relation_graph(graph, refreshed_at=3.0, next_refresh_at=4.0)
    api.manager.refresh_off_list_entry_metadata()
    assert db.get_anime(SPECIALS).cover_url == TV_COVER
    graph["nodes"][1] = _node()
    db.store_relation_graph(graph, refreshed_at=5.0, next_refresh_at=6.0)
    api.manager.refresh_off_list_entry_metadata()
    assert db.get_anime(SPECIALS).cover_url == OVA_COVER


def test_rows_on_the_list_keep_what_the_list_sync_wrote() -> None:
    listed = LibraryAnime(
        SPECIALS, "Baccano! Specials", status="PLANNING", cover_url="https://x/listed.jpg", episodes=3
    )
    extra_files.refresh_own_title_metadata(listed, _node(), None)
    assert listed.cover_url == "https://x/listed.jpg" and listed.title == "Baccano! Specials"


def test_season_zero_file_tries_the_series_number_on_jimaku() -> None:
    assert _season_zero_series_numbers(Path(_ova_name(1)), 1) == (14,)
    assert _season_zero_series_numbers(Path("[Grp] Show - S00E02 [1080p].mkv"), 2) == ()
    assert _season_zero_series_numbers(Path("[Grp] Show - 05 [1080p].mkv"), 5) == ()

    names = [f"バッカーノ！ 第{n}話.srt" for n in (14, 15, 16)] + [
        f"Baccano! - {n} [DVD].ass" for n in (14, 15, 16)
    ]

    class Client:
        logger = logging.getLogger("test")

        def files(self, entry_id, episode):
            if episode is None:
                return [JimakuFile(url=f"u/{name}", name=name, size=1, last_modified="") for name in names]
            return [
                JimakuFile(url=f"u/{name}", name=name, size=1, last_modified="")
                for name in names
                if parse_anime_filename(Path(name)).episode == episode
            ]

    client = Client()
    # Before: episode 1 alone found none of the entry's 12 files (filtered=0 merged=0).
    assert JimakuClient.files_for_episode(client, 5789, 1) == []
    found = JimakuClient.files_for_episode(client, 5789, 1, alternative_episodes=(14,))
    assert sorted(item.name for item in found) == sorted(name for name in names if "14" in name)


def test_a_fresh_start_repairs_an_empty_entry_and_caches_its_cover(tmp_path: Path, monkeypatch) -> None:
    """fx132 on the maintainer's Mac: the repair waited for the next AniList refresh."""
    import threading

    import httpx

    fetched: list[str] = []

    def fake_get(url, **_kwargs):
        fetched.append(url)
        return httpx.Response(200, content=b"\xff\xd8jpeg", request=httpx.Request("GET", url))

    monkeypatch.setattr("pudge.manager.httpx.get", fake_get)
    api, folder = _library(tmp_path, _node())
    db = api.manager.db
    api.manager.scan_library()
    assert fetched == [OVA_COVER]  # a split entry made by the scan gets its cover at once
    fetched.clear()
    for path in api.config.library.cover_cache_dir.glob(f"{SPECIALS}*"):
        path.unlink()
    # The row an older Pudge made: no cover, no AniList page.
    db.upsert_anime(LibraryAnime(SPECIALS, "Baccano! Bangai-hen", status="", episodes=3, format="SPECIAL"))
    graph = {
        "nodes": [
            {
                **_node(),
                "relation_type": "SELF",
                "media_id": TV,
                "title": "Baccano!",
                "format": "TV",
                "cover_url": TV_COVER,
                "episodes": 13,
            },
            _node(),
        ],
        "edges": [{"source": TV, "target": SPECIALS, "relation_type": "SIDE_STORY"}],
    }
    db.store_relation_graph(graph, refreshed_at=1.0, next_refresh_at=2.0)
    monkeypatch.setattr(api.manager, "run_startup_once", lambda: {})
    monkeypatch.setattr(api, "_startup_planned_release_discovery", lambda stats: None)
    monkeypatch.setattr(api, "_relink_manga_import_copies", lambda: None)
    api.asset_base = "http://127.0.0.1:1234"

    api._run_startup_maintenance_background()
    for thread in threading.enumerate():
        if thread.name.endswith("off-list-metadata"):
            thread.join(10)

    special = db.get_anime(SPECIALS)
    assert special.cover_url == OVA_COVER
    assert fetched == [OVA_COVER]
    card = api._anime_payload(special, relations=False)
    assert card["cover_remote"] == OVA_COVER
    assert card["cover"].startswith("http://127.0.0.1:1234/covers/")  # the cached file
