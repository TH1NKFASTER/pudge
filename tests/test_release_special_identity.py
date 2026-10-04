from pudge.manager_models import LibraryAnime
from pudge.providers.nyaa import (
    _season_number,
    release_identity_mismatch_reason,
    release_title_is_plausible,
)


def _konosuba_s1() -> LibraryAnime:
    return LibraryAnime(
        media_id=21202,
        title="Kono Subarashii Sekai ni Shukufuku wo!",
        titles=["KONOSUBA -God's blessing on this wonderful world!"],
        synonyms=["KonoSuba"],
        episodes=10,
        format="TV",
    )


def test_number_before_ova_is_recognized_as_season_marker() -> None:
    title = "[Erai-raws] Kono Subarashii Sekai ni Shukufuku wo! 3 - OVA 02 [1080p]"
    assert _season_number(title) == 3


def test_tv_episode_rejects_other_season_ova_release() -> None:
    anime = _konosuba_s1()
    title = "[Erai-raws] Kono Subarashii Sekai ni Shukufuku wo! 3 - OVA 02 [1080p CR WEB-DL]"
    assert release_identity_mismatch_reason(anime, title) in {
        "special-release-ova",
        "cross-season-release",
    }
    assert not release_title_is_plausible(anime, title)


def test_tv_episode_rejects_unnumbered_ova_release() -> None:
    anime = _konosuba_s1()
    title = "[Group] Kono Subarashii Sekai ni Shukufuku wo! - OVA 02 [1080p]"
    assert release_identity_mismatch_reason(anime, title) == "special-release-ova"
    assert not release_title_is_plausible(anime, title)


def test_tv_episode_keeps_normal_episode_release() -> None:
    anime = _konosuba_s1()
    title = "[HorribleSubs] Kono Subarashii Sekai ni Shukufuku wo! - 02 [1080p].mkv"
    assert release_identity_mismatch_reason(anime, title) is None
    assert release_title_is_plausible(anime, title)


def test_ova_target_may_use_ova_release() -> None:
    anime = LibraryAnime(
        media_id=999999,
        title="Example Anime OVA",
        titles=["Example Anime OVA"],
        episodes=2,
        format="OVA",
    )
    title = "[Group] Example Anime OVA - 02 [1080p]"
    assert release_identity_mismatch_reason(anime, title) is None
    assert release_title_is_plausible(anime, title)
