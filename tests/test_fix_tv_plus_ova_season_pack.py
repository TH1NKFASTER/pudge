"""A TV season pack that also carries the OVA ("01 ~ 12 + OVA [Batch]") is offered for the TV entry.

"[Erai-raws] Dorohedoro - 01 ~ 12 + OVA [Batch]" and "[Judas] Dorohedoro + OVA (Batch)"
were rejected as "special-release-ova", although the related-title guard already
treats "+ Specials" as the season with its extras. A single "Dorohedoro - OVA"
release is still no TV episode.
"""

from __future__ import annotations

from pathlib import Path

from test_fix_number_dash_episode_not_pack import make_manager
from test_fix_single_episode_not_season_pack import _candidates

from pudge.manager_models import LibraryAnime
from pudge.providers import nyaa

DOROHEDORO = LibraryAnime(
    media_id=105228,
    title="Dorohedoro",
    titles=["Dorohedoro"],
    episodes=12,
    format="TV",
    media_status="FINISHED",
    duration=24,
    relations=[{"relation_type": "SIDE_STORY", "media_id": 112443, "title": "Dorohedoro: Ma no Omake"}],
)
ERAI = "[Erai-raws] Dorohedoro - 01 ~ 12 + OVA [1080p][Batch]"
JUDAS = "[Judas] Dorohedoro + OVA (Batch) [1080p]"


def test_tv_pack_with_ova_is_a_safe_season_pack(tmp_path: Path):
    for title in (ERAI, JUDAS):
        assert nyaa.release_identity_mismatch_reason(DOROHEDORO, title) is None, title
        assert nyaa.release_title_is_plausible(DOROHEDORO, title), title
    manager = make_manager(tmp_path)
    picked, _ranked = _candidates(
        manager, DOROHEDORO, [(ERAI, "16 GiB", 50, True), (JUDAS, "9 GiB", 30, False)]
    )
    assert {item.title for item in picked} == {ERAI, JUDAS}


def test_single_ova_and_movie_packs_are_still_rejected():
    assert (
        nyaa.release_identity_mismatch_reason(
            DOROHEDORO, "[SubsPlease] Dorohedoro - OVA (1080p) [ABCD1234].mkv"
        )
        == "special-release-ova"
    )
    item = nyaa.NyaaRelease(
        "[Grp] Dorohedoro + OVA + Movie [Batch]",
        "",
        "",
        "a" * 40,
        "",
        0,
        1,
        0,
        0,
        False,
        False,
        is_batch=True,
    )
    assert not nyaa.release_is_safe_batch_candidate(DOROHEDORO, item)
