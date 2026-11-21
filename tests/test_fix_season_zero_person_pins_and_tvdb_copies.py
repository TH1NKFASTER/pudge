"""Reviewer repro (third and fourth review of the season-0 rule), kept as a regression test."""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime
from test_web_app import make_api

HANA, HANA_SP = 20593, 99001


def _hana(tmp_path: Path, folder_name: str, *, history: bool = False):
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            HANA,
            "Hanamonogatari",
            status="CURRENT",
            episodes=5,
            format="TV",
            relations=[
                {
                    "relation_type": "SIDE_STORY",
                    "media_id": HANA_SP,
                    "title": "Hana Recap",
                    "format": "SPECIAL",
                    "episodes": 1,
                    "media_status": "FINISHED",
                }
            ],
        )
    )
    folder = api.config.library.root_dir / folder_name
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text(str(HANA), encoding="utf-8")  # written by the person
    if history:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO release_history(info_hash,media_id,episode,selected_at,title) VALUES('abc',?,1,1,'x')",
                (HANA,),
            )
    names = [f"Monogatari - S00E{n:02d} - Hanamonogatari {n}.mkv" for n in (1, 2)]
    for name in names:
        (folder / name).write_bytes(b"v" + name.encode())
    api.manager.scan_library()
    return {r.video_path.name: (r.media_id, r.episode) for r in db.episodes()}, names


def test_person_pin_in_a_title_named_folder(tmp_path: Path) -> None:
    rows, names = _hana(tmp_path, "Hanamonogatari")
    print(rows)
    assert all(rows[n] == (HANA, i + 1) for i, n in enumerate(names)), rows


def test_person_pin_in_any_top_level_folder_of_a_title_pudge_once_downloaded(tmp_path: Path) -> None:
    rows, names = _hana(tmp_path, "Monogatari (2014) BD", history=True)
    print(rows)
    assert all(rows[n] == (HANA, i + 1) for i, n in enumerate(names)), rows


def test_set_aside_tv_short_episode_stays_trapped_after_the_duplicate_goes(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    mid = 31414
    db.upsert_anime(
        LibraryAnime(mid, "Re:Zero kara Hajimeru Break Time", status="CURRENT", episodes=3, format="TV_SHORT")
    )
    folder = api.config.library.root_dir / "Re Zero kara Hajimeru Break Time"
    folder.mkdir(parents=True)
    tvdb = folder / "Re ZERO Starting Life in Another World - S00E01 - Re Zero Break Time 1.mkv"
    other = folder / "[Grp] Re Zero kara Hajimeru Break Time - 01 [1080p].mkv"
    for f in (tvdb, other):
        f.write_bytes(b"v" + f.name.encode())
    api.manager.scan_library()
    first = {r.video_path.name[:30]: (r.media_id, r.episode) for r in db.episodes()}
    print("both copies:", first)
    other.unlink()  # one copy of episode 1 removed (upgrade kept the other / cleanup)
    api.manager.scan_library()
    row = db.episode_by_path(tvdb.resolve())
    print("after:", row.media_id, row.episode)
    assert (row.media_id, row.episode) == (mid, 1)


def test_keep_on_a_set_aside_tv_special_after_the_regular_episode_is_gone(tmp_path: Path) -> None:
    from test_fix_season_zero_specials import TV, _library, _ova_name, _rows, _tv_name

    api, folder = _library(tmp_path, relation=False)
    api.manager.scan_library()
    assert _rows(api, folder)[_ova_name(1)] == (TV, None)
    (folder / _tv_name(1)).unlink()  # watched and cleaned up
    (folder / _tv_name(1)).with_suffix(".ja.srt").unlink()
    api.manager.scan_library()
    assert _rows(api, folder)[_ova_name(1)] == (TV, None)
    # The person keeps the OVA file in the library.
    api.manager.keep_extra_download_file(folder / _ova_name(1))
    api.manager.scan_library()
    print("after keep:", _rows(api, folder)[_ova_name(1)])
    assert _rows(api, folder)[_ova_name(1)][1] is None, _rows(api, folder)[_ova_name(1)]


def test_a_moved_pudge_download_turns_the_person_pin_into_pudges(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            HANA,
            "Hanamonogatari",
            status="CURRENT",
            episodes=5,
            format="TV",
            relations=[
                {
                    "relation_type": "SIDE_STORY",
                    "media_id": HANA_SP,
                    "title": "Hana Recap",
                    "format": "SPECIAL",
                    "episodes": 1,
                    "media_status": "FINISHED",
                }
            ],
        )
    )
    folder = api.config.library.root_dir / "Monogatari" / "Hanamonogatari"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text(str(HANA), encoding="utf-8")  # the person's pin
    s00 = folder / "Monogatari - S00E01 - Hanamonogatari 1.mkv"
    s00.write_bytes(b"a")
    moved = folder / "[Grp] Hanamonogatari - 05 [1080p].mkv"  # a Pudge download the person moved here
    moved.write_bytes(b"b")
    db.upsert_episode(
        LibraryEpisode(
            media_id=HANA,
            title="Hanamonogatari",
            episode=5,
            media_episode=5,
            release_episode=5,
            video_path=moved.resolve(),
            torrent_hash="abc123",
        )
    )
    api.manager.scan_library()
    row = db.episode_by_path(s00.resolve())
    print("pinned S00E01:", row.media_id, row.episode)
    assert (row.media_id, row.episode) == (HANA, 1)
