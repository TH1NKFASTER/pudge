"""A season 0 file (S00E01, a "Season 0"/"Specials" folder) is a special, never an episode of
the TV entry.

User (Baccano!): "[DeeVeeDee] Baccano - S00E01 - Episode 14 - OVA 1" and S00E02/E03 sat in
the TV series' folder and became episodes 1-3 of the TV entry next to S01E01-E03: Jimaku's
TV subtitles were "upgraded" onto them, and the OP/ED detection for the title (and every
title after it) was stuck on the two files per episode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import extra_files
from pudge.episode_numbering import match_release_episode
from pudge.filename import parse_anime_filename
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.release_parser import double_episode_span, parse_release_name
from test_web_app import make_api

TV, SPECIALS = 2251, 3901
SUBTITLE = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは、世界。\n"
FOLDER = "[DeeVeeDee] Baccano! [DVD 480p HEVC Opus Dual-Audio]"


def _tv_name(n: int) -> str:
    return f"[DeeVeeDee] Baccano - S01E{n:02d} [DVD 480p HEVC Opus Dual-Audio] [A4C0AB{n:02d}].mkv"


def _ova_name(n: int) -> str:
    return (
        f"[DeeVeeDee] Baccano - S00E{n:02d} - Episode {13 + n} - OVA {n} "
        f"[DVD 480p HEVC Opus Dual-Audio] [E1FD25{n:02d}].mkv"
    )


@pytest.mark.parametrize(
    "name,episode",
    [
        (_ova_name(1), 1),
        ("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run S00E03 [1080p]", 3),
        ("Show.S0E02.1080p.WEB.mkv", 2),
    ],
)
def test_release_parser_reads_season_zero(name, episode) -> None:
    parsed = parse_release_name(name)
    assert (parsed.season, parsed.episode, parsed.explicit_episode) == (0, episode, True)
    assert double_episode_span(name) is None


def test_season_zero_release_is_rejected_for_a_regular_episode_search() -> None:
    anime = LibraryAnime(TV, "Baccano!", episodes=13, format="TV")
    match = match_release_episode(anime, parse_release_name("[Grp] Baccano! S00E02 [1080p]"), 2, None)
    assert match.status == "rejected" and match.mapped_media_episode is None


def test_extra_kind_of_a_season_zero_file() -> None:
    tv = LibraryAnime(TV, "Baccano!", episodes=13, format="TV")
    special = LibraryAnime(SPECIALS, "Baccano! Bangai-hen", episodes=3, format="SPECIAL")
    assert extra_files.extra_file_kind(tv, _ova_name(1)) == "Special"
    assert extra_files.extra_file_kind(tv, "Baccano - 01.mkv", season=0) == "Special"
    assert extra_files.extra_file_kind(tv, _tv_name(1)) == ""
    # The specials entry's own files are its episodes.
    assert extra_files.extra_file_kind(special, _ova_name(1)) == ""
    # Only a TV series has specials beside its episodes (TVDB numbers TV_SHORT/ONA in season 0).
    for fmt in ("TV_SHORT", "ONA", "OVA"):
        assert extra_files.extra_file_kind(LibraryAnime(1, "X", format=fmt), _ova_name(1)) == ""
    assert extra_files.specials_folder(Path("/m/Baccano!/Specials/Baccano - 01.mkv"))
    assert not extra_files.specials_folder(Path("/m/Baccano!/Baccano - 01.mkv"))
    assert parse_anime_filename("/m/Baccano!/Season 00/Baccano - 02.mkv").season == 0


def _library(tmp_path: Path, *, relation: bool = True):
    api = make_api(tmp_path)
    db = api.manager.db
    relations = (
        [
            {
                "relation_type": "SEQUEL",
                "media_id": SPECIALS,
                "title": "Baccano! Bangai-hen",
                "format": "SPECIAL",
                "episodes": 3,
                "media_status": "FINISHED",
            }
        ]
        if relation
        else []
    )
    db.upsert_anime(
        LibraryAnime(
            media_id=TV,
            title="Baccano!",
            status="PLANNING",
            media_status="FINISHED",
            episodes=13,
            format="TV",
            relations=relations,
        )
    )
    folder = api.config.library.root_dir / "Baccano!" / FOLDER
    folder.mkdir(parents=True)
    for n in (1, 2, 3):
        for name in (_tv_name(n), _ova_name(n)):
            video = folder / name
            video.write_bytes(b"video " + name.encode())
            video.with_suffix(".ja.srt").write_text(SUBTITLE, encoding="utf-8")
    return api, folder


def _rows(api, folder: Path) -> dict[str, tuple]:
    rows = {}
    for row in api.manager.db.episodes():
        if row.video_path.parent == folder.resolve():
            rows[row.video_path.name] = (row.media_id, row.episode)
    return rows


def test_scan_files_season_zero_as_the_related_specials_entry(tmp_path: Path) -> None:
    api, folder = _library(tmp_path)
    api.manager.scan_library()
    rows = _rows(api, folder)
    for n in (1, 2, 3):
        assert rows[_tv_name(n)] == (TV, n)
        assert rows[_ova_name(n)] == (SPECIALS, n)  # Baccano! Bangai-hen episode n
    # One file per TV episode.
    tv_rows = [row for row in api.manager.db.episodes(TV)]
    assert sorted(row.episode for row in tv_rows) == [1, 2, 3]
    api.manager.scan_library()
    assert _rows(api, folder) == rows


def test_without_a_related_entry_a_colliding_special_is_an_extra_of_the_series(tmp_path: Path) -> None:
    api, folder = _library(tmp_path, relation=False)
    for n in (1, 2, 3):
        (folder / _ova_name(n)).with_suffix(".ja.srt").unlink()  # no subtitles of its own
    api.manager.scan_library()
    rows = _rows(api, folder)
    assert [rows[_tv_name(n)] for n in (1, 2, 3)] == [(TV, 1), (TV, 2), (TV, 3)]
    # Still Baccano!'s file, but no second "episode 1": the person decides (Action required).
    assert [rows[_ova_name(n)] for n in (1, 2, 3)] == [(TV, None)] * 3
    jobs = {
        Path(row["video_path"]).name: (row["state"], row["action_code"])
        for row in api.manager.db.subtitle_jobs()
    }
    assert jobs[_ova_name(1)] == ("needs_action", "review_extra_file")
    video = (folder / _ova_name(1)).resolve()
    assert api.manager._downloaded_extra_file_kind(video, TV, None) == "Special"  # Delete / Keep work
    api.manager.scan_library()
    assert _rows(api, folder) == rows


@pytest.mark.parametrize(
    "fmt,names,folder",
    [
        # TVDB numbers short series in season 0: "Re:Zero kara Hajimeru Break Time".
        (
            "TV_SHORT",
            ["Re ZERO - S00E01 - Break Time 1.mkv", "Re ZERO - S00E02 - Break Time 2.mkv"],
            "Break Time",
        ),
        # A TV entry that TVDB keeps in season 0 and no regular file with that number.
        (
            "TV",
            ["Monogatari - S00E01 - Hanamonogatari 1.mkv", "Monogatari - S00E02 - Hanamonogatari 2.mkv"],
            "Hanamonogatari",
        ),
    ],
)
def test_season_zero_files_of_their_own_entry_keep_it(tmp_path: Path, fmt, names, folder) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(31414, folder, status="CURRENT", episodes=2, format=fmt))
    directory = api.config.library.root_dir / folder
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_bytes(name.encode())
    api.manager.scan_library()
    assert {row.video_path.name: (row.media_id, row.episode) for row in db.episodes()} == {
        names[0]: (31414, 1),
        names[1]: (31414, 2),
    }


def test_specials_folder_counts_only_inside_the_series_folder(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(20593, "Hanamonogatari", status="CURRENT", episodes=2, format="TV"))
    top = api.config.library.root_dir / "Specials"  # a top-level folder that holds a series
    top.mkdir(parents=True)
    names = ["[Grp] Hanamonogatari - 01 [1080p].mkv", "[Grp] Hanamonogatari - 02 [1080p].mkv"]
    for name in names:
        (top / name).write_bytes(name.encode())
    api.manager.scan_library()
    assert {row.video_path.name: (row.media_id, row.episode) for row in db.episodes()} == {
        names[0]: (20593, 1),
        names[1]: (20593, 2),
    }


def test_anilist_pin_of_the_person_keeps_the_title_but_never_a_duplicate_episode(tmp_path: Path) -> None:
    api, folder = _library(tmp_path, relation=True)
    (folder / ".anilist.id").write_text(str(TV))  # the person pinned their DVD folder
    api.manager.scan_library()
    rows = _rows(api, folder)
    # Not moved to the related entry (the pin decides the title), yet no second "episode 1".
    assert [rows[_ova_name(n)] for n in (1, 2, 3)] == [(TV, None)] * 3
    assert [rows[_tv_name(n)] for n in (1, 2, 3)] == [(TV, 1), (TV, 2), (TV, 3)]


def test_files_of_a_pudge_batch_follow_the_rule_under_any_sidecar(tmp_path: Path) -> None:
    api, folder = _library(tmp_path, relation=True)
    (folder / ".anilist.id").write_text(str(TV))
    db = api.manager.db
    db.record_release("24b3ff70", TV, None, "[DeeVeeDee] Baccano! [DVD 480p]", 321.0)  # a Pudge batch
    for n in (1, 2, 3):
        for name in (_tv_name(n), _ova_name(n)):
            db.upsert_episode(
                LibraryEpisode(
                    media_id=TV,
                    title="Baccano!",
                    episode=n,
                    media_episode=n,
                    release_episode=n,
                    video_path=(folder / name).resolve(),
                    state="local",
                    torrent_hash="24b3ff70",
                )
            )
    api.manager.scan_library()
    rows = _rows(api, folder)
    assert [rows[_ova_name(n)] for n in (1, 2, 3)] == [(SPECIALS, 1), (SPECIALS, 2), (SPECIALS, 3)]
    assert [rows[_tv_name(n)] for n in (1, 2, 3)] == [(TV, 1), (TV, 2), (TV, 3)]


def test_a_sidecar_pudge_recorded_is_its_own_also_in_a_renamed_folder(tmp_path: Path) -> None:
    from pudge import extra_files

    api, folder = _library(tmp_path, relation=True)
    renamed = folder.parent.with_name("Baccano (old title)")
    folder.parent.rename(renamed)
    folder = renamed / FOLDER
    (renamed / ".anilist.id").write_text(str(TV))
    api.manager.db.set_state(extra_files.pudge_sidecar_state_key(renamed), str(TV))  # Pudge wrote it
    api.manager.scan_library()
    rows = _rows(api, folder)
    assert [rows[_ova_name(n)] for n in (1, 2, 3)] == [(SPECIALS, 1), (SPECIALS, 2), (SPECIALS, 3)]


def test_series_of_other_episodic_formats_get_no_duplicate_episodes(tmp_path: Path) -> None:
    """Cyberpunk: Edgerunners is an ONA: its S00E01 next to S01E01 is an extra, no second ep. 1."""
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            120377,
            "Cyberpunk: Edgerunners",
            status="CURRENT",
            episodes=10,
            format="ONA",
            relations=[{"relation_type": "SIDE_STORY", "media_id": 5, "title": "X", "format": "SPECIAL"}],
        )
    )
    directory = api.config.library.root_dir / "Cyberpunk Edgerunners"
    directory.mkdir(parents=True)
    names = ["Cyberpunk Edgerunners - S01E01.mkv", "Cyberpunk Edgerunners - S00E01 - Prologue.mkv"]
    for name in names:
        (directory / name).write_bytes(name.encode())
    api.manager.scan_library()
    rows = {row.video_path.name: (row.media_id, row.episode) for row in db.episodes()}
    # Not moved to the related entry (TV only), set aside instead of a duplicate episode 1.
    assert rows == {names[0]: (120377, 1), names[1]: (120377, None)}
    video = (directory / names[1]).resolve()
    assert api.manager._downloaded_extra_file_kind(video, 120377, None) == "Special"
    # Keep: the person decided; the scan leaves it alone.
    assert api.manager.keep_extra_download_file(video)
    api.manager.scan_library()
    assert not db.get_state(extra_files.set_aside_state_key(video), "")
    row = db.episode_by_path(video)
    assert (row.media_id, row.episode) == (120377, None)  # kept: still no second episode 1


def test_season_zero_releases_of_a_specials_entry_match_by_title_as_before(tmp_path: Path) -> None:
    from pudge.episode_numbering import media_episode_from_release

    special = LibraryAnime(SPECIALS, "Baccano! Bangai-hen", episodes=3, format="SPECIAL")
    parsed = parse_release_name("[Grp] Baccano! Bangai-hen S00E02 [1080p]")
    assert match_release_episode(special, parsed, 2, None).status == "ambiguous"
    api = make_api(tmp_path)
    assert (
        media_episode_from_release(
            special, 2, api.config, api.manager.logger, release_name="[Grp] Baccano! Bangai-hen S00E02.mkv"
        )
        == 2
    )
    tv = LibraryAnime(TV, "Baccano!", episodes=13, format="TV")
    assert (
        media_episode_from_release(
            tv, 2, api.config, api.manager.logger, release_name="[Grp] Baccano! S00E02.mkv"
        )
        is None
    )


def test_wrong_rows_of_an_older_scan_are_corrected_without_leaking_onto_tv_episodes(tmp_path: Path) -> None:
    api, folder = _library(tmp_path)
    db = api.manager.db
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    for n in (1, 2, 3):
        # What the old scan stored: the OVA as TV episode n, Ready with Jimaku's TV subtitles.
        for name in (_tv_name(n), _ova_name(n)):
            (folder / name).with_suffix(".ja.srt").unlink()
        tv_sub = prepared / f"tv{n}.srt"
        tv_sub.write_text(SUBTITLE, encoding="utf-8")
        ova_sub = prepared / f"ova{n}.srt"
        ova_sub.write_text(SUBTITLE, encoding="utf-8")
        for name, subtitle, watched in ((_tv_name(n), tv_sub, n == 1), (_ova_name(n), ova_sub, False)):
            db.upsert_episode(
                LibraryEpisode(
                    media_id=TV,
                    title="Baccano!",
                    episode=n,
                    media_episode=n,
                    release_episode=n,
                    video_path=(folder / name).resolve(),
                    subtitle_path=subtitle,
                    state="watched" if watched else "ready",
                    subtitle_origin="jimaku",
                    watched_at=1.0 if watched else None,
                )
            )
    api.manager.scan_library()
    for n in (1, 2, 3):
        ova = db.episode_by_path((folder / _ova_name(n)).resolve())
        assert (ova.media_id, ova.episode) == (SPECIALS, n)
        # Jimaku's subtitles of TV episode n are not the special's: prepared again.
        assert ova.subtitle_path is None and ova.state in {"local", "waiting_subtitles"}
        tv = db.episode_by_path((folder / _tv_name(n)).resolve())
        assert (tv.media_id, tv.episode, tv.subtitle_path) == (TV, n, prepared / f"tv{n}.srt")
        assert tv.state == ("watched" if n == 1 else "ready")
    jobs = {Path(row["video_path"]).name: (row["media_id"], row["episode"]) for row in db.subtitle_jobs()}
    assert jobs[_ova_name(2)] == (SPECIALS, 2)
    assert _tv_name(2) not in jobs
    assert sorted(row.episode for row in db.episodes(TV)) == [1, 2, 3]
    assert [row.state for row in db.episodes(TV) if row.episode == 1] == ["watched"]


def test_specials_folder_files_are_specials(tmp_path: Path) -> None:
    api, folder = _library(tmp_path)
    specials = folder.parent / "Specials"
    specials.mkdir()
    video = specials / "Baccano! - 02.mkv"
    video.write_bytes(b"special 2")
    api.manager.scan_library()
    row = api.manager.db.episode_by_path(video.resolve())
    assert (row.media_id, row.episode) == (SPECIALS, 2)


def test_season_zero_file_of_a_downloaded_batch_is_an_extra_file(tmp_path: Path, monkeypatch) -> None:
    from test_fix_batch_extra_files import MEDIA, _batch, _no_subtitles, _series

    api = make_api(tmp_path)
    _series(api.manager.db)
    _no_subtitles(monkeypatch)
    _folder, item = _batch(
        tmp_path,
        ["[Grp] Dorohedoro - S01E01 (1080p).mkv", "[Grp] Dorohedoro - S00E01 (1080p).mkv"],
    )
    api.manager._register_completed_download(item)
    episodes = {row.video_path.name: row.episode for row in api.manager.db.episodes(MEDIA)}
    # Not a second "episode 1": an extra file the person decides about (Action required).
    assert episodes == {
        "[Grp] Dorohedoro - S01E01 (1080p).mkv": 1,
        "[Grp] Dorohedoro - S00E01 (1080p).mkv": None,
    }


def test_registration_of_an_ona_batch_leaves_a_set_aside_special_alone(tmp_path: Path, monkeypatch) -> None:
    from test_fix_batch_extra_files import MEDIA, _batch, _no_subtitles

    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(MEDIA, "Dorohedoro", status="CURRENT", episodes=3, format="ONA"))
    _no_subtitles(monkeypatch)
    root = api.config.library.root_dir
    root.mkdir(parents=True, exist_ok=True)
    folder, item = _batch(
        root, ["[Grp] Dorohedoro - S01E01 (1080p).mkv", "[Grp] Dorohedoro - S00E01 (1080p).mkv"]
    )
    db.upsert_download(item)
    api.manager._register_completed_download(item)
    special = (folder / "[Grp] Dorohedoro - S00E01 (1080p).mkv").resolve()
    api.manager.scan_library()
    assert db.episode_by_path(special).episode is None  # no second episode 1 of the ONA
    # The download is polled again while it seeds: the decision stays.
    api.manager._register_completed_download(item)
    api.manager.scan_library()
    assert db.episode_by_path(special).episode is None


def test_sidecars_of_folders_pudge_downloaded_into_are_recorded_once(tmp_path: Path) -> None:
    from pudge import extra_files
    from pudge.library import _record_pudge_sidecars_once
    from pudge.manager_models import DownloadItem

    api, folder = _library(tmp_path)
    db = api.manager.db
    root = api.config.library.root_dir
    (folder.parent / ".anilist.id").write_text(str(TV))  # written by an older Pudge
    db.upsert_download(
        DownloadItem(
            torrent_hash="24b3ff70",
            name=FOLDER,
            state="uploading",
            progress=1.0,
            save_path=str(folder.parent),
            content_path=str(folder),
            media_id=TV,
            is_batch=True,
            completed_on=1,
        )
    )
    # A person's pinned folder holding a file with a torrent hash they moved there: not Pudge's.
    other = root / "My Baccano"
    other.mkdir()
    (other / ".anilist.id").write_text(str(TV))
    moved = other / "[Grp] Baccano - 05.mkv"
    moved.write_bytes(b"moved")
    db.upsert_episode(
        LibraryEpisode(
            media_id=TV,
            title="Baccano!",
            episode=5,
            media_episode=5,
            release_episode=5,
            video_path=moved.resolve(),
            state="local",
            torrent_hash="99aa",
        )
    )
    _record_pudge_sidecars_once(db, root)
    assert db.get_state(extra_files.pudge_sidecar_state_key(folder.parent), "") == str(TV)
    assert not db.get_state(extra_files.pudge_sidecar_state_key(other), "")


def test_two_specials_never_share_one_episode_of_the_related_entry(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    relation = {
        "relation_type": "SIDE_STORY",
        "media_id": SPECIALS,
        "title": "Recap",
        "format": "SPECIAL",
        "episodes": 1,
    }
    db.upsert_anime(
        LibraryAnime(TV, "Baccano!", status="CURRENT", episodes=13, format="TV", relations=[relation])
    )
    folder = api.config.library.root_dir / "Baccano!"
    folder.mkdir(parents=True)
    names = [f"Baccano - S00E0{n} - Special {n}.mkv" for n in (1, 2)]
    for name in names:
        (folder / name).write_bytes(name.encode())
    api.manager.scan_library()
    api.manager.scan_library()
    rows = sorted((row.media_id or 0, row.episode or 0) for row in db.episodes())
    # One takes the one-episode entry; the other waits for the person instead of a second "episode 1".
    assert rows == sorted([(SPECIALS, 1), (TV, 0)])


def test_keep_gives_a_set_aside_tvdb_episode_its_number_and_clears_the_record_at_once(tmp_path: Path) -> None:
    from pudge import extra_files

    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            31414, "Re:Zero kara Hajimeru Break Time", status="CURRENT", episodes=3, format="TV_SHORT"
        )
    )
    folder = api.config.library.root_dir / "Re Zero kara Hajimeru Break Time"
    folder.mkdir(parents=True)
    tvdb = folder / "Re ZERO - S00E01 - Re Zero Break Time 1.mkv"
    other = folder / "[Grp] Re Zero kara Hajimeru Break Time - 01 [1080p].mkv"
    for path in (tvdb, other):
        path.write_bytes(path.name.encode())
    api.manager.scan_library()
    video = tvdb.resolve()
    assert db.episode_by_path(video).episode is None and db.get_state(
        extra_files.set_aside_state_key(video), ""
    )
    # Episode 1 is taken by the other copy: Keep with the number the person chooses.
    assert api.extra_file_keep(str(video), 2)["ok"] is True
    row = db.episode_by_path(video)
    assert (row.media_id, row.episode) == (31414, 2)
    assert not db.get_state(extra_files.set_aside_state_key(video), "")  # cleared at once
    api.manager.scan_library()
    row = db.episode_by_path(video)
    assert (row.media_id, row.episode) == (31414, 2)


def test_a_present_download_proves_the_sidecar_only_while_it_is_there(tmp_path: Path) -> None:
    from pudge import extra_files
    from pudge.library import _pudge_sidecar, _PudgeDownloads
    from pudge.manager_models import DownloadItem

    api, folder = _library(tmp_path)
    db = api.manager.db
    item = DownloadItem(
        torrent_hash="24b3ff70",
        name=FOLDER,
        state="uploading",
        progress=1.0,
        save_path=str(folder.parent),
        content_path=str(folder),
        media_id=TV,
        is_batch=True,
        completed_on=1,
    )
    db.upsert_download(item)
    assert _pudge_sidecar(db, folder.parent, TV, _PudgeDownloads(db))
    assert not db.get_state(extra_files.pudge_sidecar_state_key(folder.parent), "")  # not stored
    with db.connect() as conn:
        conn.execute("DELETE FROM downloads")
    assert not _pudge_sidecar(db, folder.parent, TV, _PudgeDownloads(db))
