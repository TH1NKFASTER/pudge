"""A release named by the prequel's title with this entry's absolute episode number is this entry.

"[SubsPlease] Boku no Hero Academia - 139" is My Hero Academia season 7 episode 1
(absolute 139). With the franchise graph's titles as negatives, "Boku no Hero
Academia" (season 1's title) made it a "related-media-title" and SubsPlease, the
weekly source, was lost for continuing shows.
"""

from __future__ import annotations

from test_fix_number_dash_episode_not_pack import FeedClient, rss

from pudge.manager_models import LibraryAnime
from pudge.providers import nyaa

MHA7 = LibraryAnime(
    media_id=163139,
    title="Boku no Hero Academia 7",
    titles=["My Hero Academia Season 7"],
    episodes=21,
    format="TV",
    media_status="FINISHED",
    relations=[{"relation_type": "PREQUEL", "title": "Boku no Hero Academia 6"}],
)
NEGATIVES = ("Boku no Hero Academia", "Boku no Hero Academia 6")
ABSOLUTE = "[SubsPlease] Boku no Hero Academia - 139 (1080p) [ABCD1234].mkv"


def _ranked(titles, *, alternative_episodes=(139,)):
    client = FeedClient(rss([(title, "1.4 GiB", 300, True) for title in titles]))
    nyaa._MANUAL_QUERY_CACHE.clear()
    return nyaa.search_ranked(
        client,
        MHA7,
        episode=1,
        batch=False,
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        alternative_episodes=alternative_episodes,
        alternative_titles=("Boku no Hero Academia",),
        negative_titles=NEGATIVES,
        query_budget_seconds=18.0,
    )


def test_prequel_title_with_absolute_alias_is_plausible():
    assert nyaa.release_title_is_plausible(
        MHA7,
        ABSOLUTE,
        alternative_titles=("Boku no Hero Academia",),
        negative_titles=NEGATIVES,
        alternative_episodes=(139,),
    )
    found = _ranked([ABSOLUTE])
    assert [item.title for item in found] == [ABSOLUTE]
    assert "absolute-ep=139" in found[0].reasons


def test_prequel_title_without_the_alias_is_still_another_entry():
    # Season 1 episode 1, or no absolute numbering known: still rejected.
    assert not nyaa.release_title_is_plausible(
        MHA7,
        "[SubsPlease] Boku no Hero Academia - 01 (1080p) [ABCD1234].mkv",
        alternative_titles=("Boku no Hero Academia",),
        negative_titles=NEGATIVES,
        alternative_episodes=(139,),
    )
    assert _ranked([ABSOLUTE], alternative_episodes=()) == []
    # A negative that is not one of the alias titles still conflicts.
    assert (
        nyaa.release_related_title_conflict_reason(
            MHA7,
            "[SubsPlease] Boku no Hero Academia: Vigilantes - 139 (1080p)",
            ("Boku no Hero Academia: Vigilantes",),
            alternative_titles=("Boku no Hero Academia",),
            alternative_episodes=(139,),
        )
        == "related-media-title"
    )
