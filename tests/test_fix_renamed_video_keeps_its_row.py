"""A renamed or moved video keeps its library row (position, watched mark, subtitle).

Tidying "[SubsPlease] Dorohedoro - 03 (1080p) [ABCD1234].mkv" into "Dorohedoro 03.mkv"
started a new "local" row with a new subtitle job, while the old row with its watched
mark and subtitle stayed until the missing-file forget delay passed.
"""

from __future__ import annotations

from pathlib import Path

from test_web_app import make_api

from pudge.manager_models import LibraryAnime, LibraryEpisode


def _setup(tmp_path: Path, monkeypatch):
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=1,
            title="Dorohedoro",
            status="CURRENT",
            episodes=12,
            media_status="FINISHED",
            format="TV",
        )
    )
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    folder = manager.config.library.root_dir.expanduser() / "Dorohedoro"
    folder.mkdir(parents=True)
    return manager, db, folder


def _prepared(db, folder: Path, video: Path) -> Path:
    sub = folder / "ep3.ja.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nドロヘドロ\n", encoding="utf-8")
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Dorohedoro",
            episode=3,
            media_episode=3,
            video_path=video.resolve(),
            subtitle_path=sub.resolve(),
            subtitle_origin="jimaku",
            state="watched",
        )
    )
    db.record_subtitle_history(
        video_path=video.resolve(), media_id=1, episode=3, source="jimaku", status="selected"
    )
    db.record_playback(video.resolve(), 600.0, 1400.0)
    return sub


def test_renamed_prepared_video_keeps_state_and_subtitle(tmp_path: Path, monkeypatch) -> None:
    manager, db, folder = _setup(tmp_path, monkeypatch)
    old = folder / "[SubsPlease] Dorohedoro - 03 (1080p) [ABCD1234].mkv"
    old.write_bytes(b"video" * 1000)
    manager.scan_library()
    sub = _prepared(db, folder, old)

    new = folder / "Dorohedoro 03.mkv"
    old.rename(new)
    manager.scan_library()

    rows = {row.video_path.name: row for row in db.episodes(1)}
    assert list(rows) == ["Dorohedoro 03.mkv"]
    row = rows["Dorohedoro 03.mkv"]
    assert (row.episode, row.state, row.subtitle_path) == (3, "watched", sub.resolve())
    assert row.playback_position == 600.0
    assert [Path(job["video_path"]).name for job in db.subtitle_jobs()] in ([], ["Dorohedoro 03.mkv"])
    assert db.latest_selected_subtitle(new.resolve()) is not None


def test_moved_into_another_folder_keeps_the_row(tmp_path: Path, monkeypatch) -> None:
    manager, db, folder = _setup(tmp_path, monkeypatch)
    old = folder / "[SubsPlease] Dorohedoro - 03 (1080p) [ABCD1234].mkv"
    old.write_bytes(b"video" * 1000)
    manager.scan_library()
    _prepared(db, folder, old)
    target = folder / "Season 1"
    target.mkdir()
    old.rename(target / old.name)
    manager.scan_library()
    assert [(row.video_path.parent.name, row.state) for row in db.episodes(1)] == [("Season 1", "watched")]


def test_a_different_file_of_the_same_episode_does_not_take_the_subtitle(tmp_path: Path, monkeypatch) -> None:
    manager, db, folder = _setup(tmp_path, monkeypatch)
    old = folder / "[SubsPlease] Dorohedoro - 03 (1080p) [ABCD1234].mkv"
    old.write_bytes(b"video" * 1000)
    manager.scan_library()
    _prepared(db, folder, old)

    old.unlink()
    other = folder / "[Erai-raws] Dorohedoro - 03 [1080p].mkv"
    other.write_bytes(b"other release" * 900)
    manager.scan_library()

    rows = {row.video_path.name: row for row in db.episodes(1)}
    assert rows[other.name].state == "local" and rows[other.name].subtitle_path is None
    # The old row is still there until the missing-file delay forgets it.
    assert rows[old.name].state == "watched"


def test_unprepared_row_moves_but_two_missing_copies_are_ambiguous(tmp_path: Path, monkeypatch) -> None:
    manager, db, folder = _setup(tmp_path, monkeypatch)
    first = folder / "[SubsPlease] Dorohedoro - 05 (1080p) [ABCD1234].mkv"
    first.write_bytes(b"video" * 1000)
    manager.scan_library()
    first.rename(folder / "Dorohedoro 05.mkv")
    manager.scan_library()
    assert [row.video_path.name for row in db.episodes(1)] == ["Dorohedoro 05.mkv"]

    a = folder / "[A] Dorohedoro - 07.mkv"
    b = folder / "[B] Dorohedoro - 07.mkv"
    a.write_bytes(b"a" * 100)
    b.write_bytes(b"b" * 100)
    manager.scan_library()
    a.unlink()
    b.unlink()
    (folder / "Dorohedoro 07.mkv").write_bytes(b"c" * 100)
    manager.scan_library()
    names = sorted(row.video_path.name for row in db.episodes(1) if row.episode == 7)
    assert names == ["Dorohedoro 07.mkv", a.name, b.name]
