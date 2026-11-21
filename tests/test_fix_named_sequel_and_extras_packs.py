"""Season packs the release search used to throw away.

User: "why can't it find the second season of Rakugo" — "[SCY-kBaraka] Shouwa Genroku Rakugo
Shinjuu S2 [BD 1080p FLAC]" and "[sam] Shouwa Genroku Rakugo Shinjuu Season 2: Sukeroku
Futatabi-hen (BD 1080p ...)" exist. AniList calls the entry "Shouwa Genroku Rakugo Shinjuu:
Sukeroku Futatabi-hen" with no number, so its season was 1: every "S2" pack was "another
season", the first season's title in it was "another entry", SeaDex's own pick (sam, files
"... S2 - 01") was dropped too, and no query asked for "<first season title> S2". The search
found one 2.6 GiB release and went on to 12 episode searches. Now the entry's place in its
relation chain (season 2) is accepted next to the titles' own season.

User: "Baccano decided to download 480p although" 1080p packs exist. "Baccano! + Specials
[BD 1080p ...]" (171 seeders) was taken for the Specials entry, "1920x1080" was no known
resolution, "x.264" read as season X and "10-bit" as episode 10; only DVD packs and one
slow 1080p pack were left, and the race ended on the DVD.
"""

from __future__ import annotations

from types import SimpleNamespace

from pudge import manager as manager_module
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa as N
from pudge.providers.seadex import SeaDexRecommendation, SeaDexTorrent

GIB = 1024**3
ROOT = "Shouwa Genroku Rakugo Shinjuu"
S2_TITLE = "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen"


def _rakugo(chain: bool = True) -> LibraryAnime:
    return LibraryAnime(
        21733,
        S2_TITLE,
        titles=[S2_TITLE, "Descending Stories: Showa Genroku Rakugo Shinju"],
        episodes=12,
        format="TV",
        season_year=2017,
        media_status="FINISHED",
        duration=24,
        relations=[{"relation_type": "PREQUEL", "media_id": 20972, "title": ROOT, "format": "TV"}],
        chain_season=2 if chain else 0,
        chain_titles=[ROOT] if chain else [],
    )


def _baccano() -> LibraryAnime:
    return LibraryAnime(
        2251,
        "Baccano!",
        titles=["Baccano!"],
        episodes=13,
        format="TV",
        season_year=2007,
        media_status="FINISHED",
        duration=24,
        relations=[
            {
                "relation_type": "SIDE_STORY",
                "media_id": 3290,
                "title": "Baccano! Specials",
                "format": "SPECIAL",
            }
        ],
    )


def _kwargs() -> dict:
    nyaa = AppConfig().nyaa
    return dict(
        trusted_groups=nyaa.trusted_groups,
        preferred_groups=nyaa.preferred_groups,
        blocked_groups=nyaa.blocked_groups,
        preferred_resolution="highest",
        preferred_video_codecs=nyaa.preferred_video_codecs,
        preferred_sources=nyaa.preferred_sources,
        min_seeders=nyaa.min_seeders,
        target_episode_min_bytes=nyaa.episode_min_size_mb * 1024 * 1024,
        target_episode_max_bytes=nyaa.episode_max_size_mb * 1024 * 1024,
    )


def _scored(anime: LibraryAnime, title: str, size_gib: float, seeds: int) -> NyaaRelease:
    release = NyaaRelease(
        title=title,
        link="",
        torrent_url="",
        info_hash="",
        size_text="",
        size_bytes=int(size_gib * GIB),
        seeders=seeds,
        leechers=0,
        downloads=100,
        trusted=False,
        remake=False,
        category_id="1_2",
        is_batch=bool(N.BATCH_RE.search(title)),
        group=N.release_group(title),
    )
    return N.score_release(release, anime, episode=None, batch=True, **_kwargs())


def _takes(anime: LibraryAnime, title: str, size_gib: float, seeds: int, negative: tuple[str, ...]) -> bool:
    if not N.release_title_is_plausible(anime, title, (), negative):
        return False
    item = _scored(anime, title, size_gib, seeds)
    return item.score >= 72 and N.release_is_safe_batch_candidate(anime, item, negative_titles=negative)


SECOND_SEASON_PACKS = [
    ("[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 [BD 1080p FLAC]", 28.2, 7),
    (
        "[sam] Shouwa Genroku Rakugo Shinjuu Season 2: Sukeroku Futatabi-hen (BD 1080p HEVC x265 10-bit FLAC)",
        29.8,
        29,
    ),
    ("[Ñ] Shouwa Genroku Rakugo Shinjuu 2 [BD 1080p]", 13.2, 4),
    (
        "[VCB-Studio] Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi Hen / 昭和元禄落語心中 -助六再び篇- "
        "10-bit 1080p HEVC BDRip [Fin]",
        25.4,
        14,
    ),
]


def test_second_season_packs_named_after_the_first_season_are_taken() -> None:
    anime, negative = _rakugo(), (ROOT,)
    for title, size, seeds in SECOND_SEASON_PACKS:
        assert _takes(anime, title, size, seeds, negative), title
    # The first season and a third one stay out.
    assert not _takes(
        anime, "[HorribleSubs] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", 7.6, 6, negative
    )
    assert not _takes(
        anime, "[sam] Shouwa Genroku Rakugo Shinjuu (BD 1080p HEVC x265 10-bit FLAC)", 43.7, 33, negative
    )
    assert not _takes(anime, "[Grp] Shouwa Genroku Rakugo Shinjuu S3 [BD 1080p]", 20.0, 9, negative)


def test_without_a_known_chain_nothing_changes() -> None:
    anime, negative = _rakugo(chain=False), (ROOT,)
    assert not _takes(anime, SECOND_SEASON_PACKS[0][0], 28.2, 7, negative)
    assert N._accepted_seasons(anime) == {1}


def test_queries_ask_for_the_first_season_title_with_the_season() -> None:
    queries = N.release_search_queries(_rakugo(), episode=None, batch=True)
    assert f"{ROOT} S2" in queries and f"{ROOT} 2" in queries
    assert queries[0] == S2_TITLE and len(queries) <= N.BATCH_QUERY_LIMIT
    assert f"{ROOT} S2" not in N.release_search_queries(_rakugo(chain=False), episode=None, batch=True)


def test_seadex_pick_with_s2_file_names_is_offered() -> None:
    files = tuple(
        (f"[sam] Shouwa Genroku Rakugo Shinjuu S2 - {n:02d} [BD 1080p FLAC] [ABCD{n:04d}].mkv", 2 * GIB)
        for n in range(1, 13)
    )
    seadex = SeaDexRecommendation(
        21733,
        "found",
        preferred_hashes=frozenset({"af" * 20}),
        torrents=(SeaDexTorrent(info_hash="af" * 20, url="", release_group="sam", best=True, files=files),),
    )
    offered = N.seadex_batch_candidates(seadex, _rakugo(), negative_titles=(ROOT,), **_kwargs())
    assert [item.info_hash for item in offered] == ["af" * 20]
    assert N.release_is_safe_batch_candidate(_rakugo(), offered[0], negative_titles=(ROOT,))
    assert N.seadex_batch_candidates(seadex, _rakugo(chain=False), negative_titles=(ROOT,), **_kwargs()) == []


def test_manager_finds_the_place_in_the_chain(tmp_path, monkeypatch) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    manager = AnimeManager(config, log=lambda _message: None)
    anime = _rakugo(chain=False)
    seen = {}

    def numbering(item, episode, *_args, **kwargs):
        seen["call"] = (item.media_id, episode, kwargs.get("allow_network"))
        return SimpleNamespace(
            chain=(20972, 21733), prequel_titles=(ROOT,), status=seen.get("status", "resolved")
        )

    monkeypatch.setattr(manager_module, "resolve_episode_numbering", numbering)
    found = manager._with_chain_season(anime)
    assert (found.chain_season, found.chain_titles) == (2, [ROOT])
    assert seen["call"] == (21733, 1, False)  # cached graph only, no AniList call
    seen["status"] = "conflict"
    assert manager._with_chain_season(anime).chain_season == 2  # remembered for a while
    manager._chain_season_memo.clear()
    assert manager._with_chain_season(anime).chain_season == 0
    # Planning rows often have no relations: the cached graph still decides.
    bare = LibraryAnime(21733, S2_TITLE, episodes=12, format="TV")
    seen["status"] = "resolved"
    manager._chain_season_memo.clear()
    assert manager._with_chain_season(bare).chain_titles == [ROOT]
    lone = LibraryAnime(5, "Lone", episodes=12, format="TV")
    assert manager._with_chain_season(lone) is lone


def test_tv_pack_with_specials_is_the_tv_entry() -> None:
    anime, negative = _baccano(), ("Baccano! Specials",)
    for title, size, seeds in [
        ("Baccano! + Specials [BD 1080p x265 10Bit AAC] (Dual Audio)", 4.7, 171),
        ("[AkihitoSubs] Baccano! + Specials [BD 1920x1080 x265 10Bit AAC]", 4.3, 51),
        ("[Moozzi2] Baccano! (BD 1920x1080 x.264 FLACx2) - TV + SP", 11.6, 5),
    ]:
        assert _takes(anime, title, size, seeds, negative), title
    # The Specials entry's own release is still not the TV season.
    assert not N.release_title_is_plausible(anime, "[Grp] Baccano! Specials [BD 1080p]", (), negative)
    best = _scored(anime, "Baccano! + Specials [BD 1080p x265 10Bit AAC] (Dual Audio)", 4.7, 171)
    dvd = _scored(anime, "[MTBB] Baccano! (DVD 480p)", 14.2, 40)
    assert best.score > dvd.score + 60  # more than SeaDex's bonus for the DVD


def test_codec_bit_depth_and_frame_size_are_not_season_episode_or_unknown() -> None:
    assert N._season_number("[Moozzi2] Baccano! (BD 1920x1080 x.264 FLACx2) - TV + SP") is None
    assert N._season_number("Show X - 05 [1080p]") == 10  # a real roman X still is
    assert N._release_episode_match("[sam] Show (BD 1080p HEVC x265 10-bit FLAC)") == (None, False)
    assert N._release_episode_match("[Grp] Show - 10 (BD 1080p 10-bit)")[0] == 10
    assert N._quality_score("[Grp] Show [BD 1920x1080 x265]", "highest")[1][0] == "1080p"
    assert N._quality_score("[Grp] Show (1280×720)", "1080p")[1][0] == "720p"


def test_a_second_season_synonym_and_the_chain_together() -> None:
    """The user's row: a "2nd Season" synonym made the season 2 already, so the chain
    was not consulted and "Shouwa Genroku Rakugo Shinjuu S2" was still "another entry"."""
    from dataclasses import replace

    anime = replace(_rakugo(), synonyms=["Descending Stories: Showa Genroku Rakugo Shinju 2nd Season"])
    assert N._expected_season(anime) == 2
    for title, size, seeds in SECOND_SEASON_PACKS:
        assert _takes(anime, title, size, seeds, (ROOT,)), title
    queries = N.release_search_queries(anime, episode=None, batch=True)
    assert f"{ROOT} S2" in queries and f"{ROOT} 2" in queries


def test_seadex_dvd_pick_gets_no_bonus_over_an_hd_preference() -> None:
    seadex = SeaDexRecommendation(2251, "found", preferred_hashes=frozenset({"ab" * 20}))
    dvd = NyaaRelease(
        title="[MTBB] Baccano! (DVD 480p)",
        link="",
        torrent_url="",
        info_hash="ab" * 20,
        size_text="",
        size_bytes=int(14.2 * GIB),
        seeders=40,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        category_id="1_2",
        is_batch=False,
        group="MTBB",
    )
    kwargs = _kwargs()
    highest = N.score_release(dvd, _baccano(), episode=None, batch=True, seadex=seadex, **kwargs)
    assert "seadex-sd-pick-no-bonus" in highest.reasons and "seadex-preferred" not in highest.reasons
    kwargs["preferred_resolution"] = "720p"
    sd_ok = N.score_release(dvd, _baccano(), episode=None, batch=True, seadex=seadex, **kwargs)
    assert "seadex-preferred" in sd_ok.reasons


def test_aria2_race_of_a_pack_also_tries_well_seeded_packs_after_the_best_three() -> None:
    def item(name: str, seeds: int) -> NyaaRelease:
        return NyaaRelease(
            title=name,
            link="",
            torrent_url="",
            info_hash=name,
            size_text="",
            size_bytes=GIB,
            seeders=seeds,
            leechers=0,
            downloads=0,
            trusted=False,
            remake=False,
            category_id="1_2",
            is_batch=True,
            group="",
        )

    ranked = [
        item("a", 20),
        item("b", 46),
        item("c", 40),
        item("d", 6),
        item("e", 171),
        item("f", 120),
        item("g", 95),
    ]
    assert [x.title for x in AnimeManager._race_pool(ranked, batch=True)] == ["a", "b", "c", "e", "f"]
    assert [x.title for x in AnimeManager._race_pool(ranked, batch=False)] == ["a", "b", "c"]
    assert [x.title for x in AnimeManager._race_pool(ranked[:2], batch=True)] == ["a", "b"]
