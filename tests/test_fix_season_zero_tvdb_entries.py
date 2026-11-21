"""Reviewer repro (review of d45e772), kept as a regression test."""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime
from test_web_app import make_api


def _scan(tmp_path: Path, fmt: str, names: list[str], folder_name: str, title: str, mid: int = 31414):
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(mid, title, status="CURRENT", episodes=len(names), format=fmt))
    folder = api.config.library.root_dir / folder_name
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"v" + name.encode())
    api.manager.scan_library()
    return {row.video_path.name: (row.media_id, row.episode) for row in db.episodes()}


def test_tv_short_whose_files_are_named_s00(tmp_path: Path) -> None:
    names = [
        f"Re ZERO Starting Life in Another World - S00E{n:02d} - Re Zero Break Time {n}.mkv"
        for n in (1, 2, 3)
    ]
    rows = _scan(
        tmp_path, "TV_SHORT", names, "Re Zero kara Hajimeru Break Time", "Re:Zero kara Hajimeru Break Time"
    )
    print(rows)
    assert all(rows[name][0] == 31414 for name in names), rows


def test_specials_folder_holding_a_tv_series(tmp_path: Path) -> None:
    names = [f"[Grp] Hanamonogatari - {n:02d} [1080p].mkv" for n in (1, 2)]
    rows = _scan(tmp_path, "TV", names, "Specials", "Hanamonogatari", mid=20593)
    print(rows)
    assert all(rows[name] == (20593, i + 1) for i, name in enumerate(names)), rows


def test_existing_ready_tv_short_rows_lose_their_jimaku_subtitles(tmp_path: Path) -> None:
    from pudge.manager_models import LibraryEpisode

    api = make_api(tmp_path)
    db = api.manager.db
    mid = 31414
    db.upsert_anime(
        LibraryAnime(mid, "Re:Zero kara Hajimeru Break Time", status="CURRENT", episodes=3, format="TV_SHORT")
    )
    folder = api.config.library.root_dir / "Re Zero kara Hajimeru Break Time"
    folder.mkdir(parents=True)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    name = "Re ZERO Starting Life in Another World - S00E01 - Re Zero Break Time 1.mkv"
    video = folder / name
    video.write_bytes(b"v")
    sub = prepared / "ep1.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:03,000\nこんにちは。\n", encoding="utf-8")
    db.upsert_episode(
        LibraryEpisode(
            media_id=mid,
            title="Re:Zero kara Hajimeru Break Time",
            episode=1,
            media_episode=1,
            release_episode=1,
            video_path=video.resolve(),
            subtitle_path=sub,
            state="ready",
            subtitle_origin="jimaku",
        )
    )
    api.manager.scan_library()
    row = db.episode_by_path(video.resolve())
    print("row:", row.media_id, row.episode, row.state, row.subtitle_path)
    assert (row.media_id, row.subtitle_path) == (mid, sub)
