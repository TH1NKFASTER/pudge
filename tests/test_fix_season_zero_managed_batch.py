"""Reviewer repro (review of d45e772), kept as a regression test."""

from __future__ import annotations

from pathlib import Path

from test_fix_batch_extra_files import MEDIA, _batch, _no_subtitles, _series
from test_web_app import make_api


def test_wrong_row_of_a_managed_batch_is_corrected(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    _series(db)
    _no_subtitles(monkeypatch)
    root = api.config.library.root_dir
    root.mkdir(parents=True, exist_ok=True)
    folder, item = _batch(
        root,
        ["[Grp] Dorohedoro - S01E01 (1080p).mkv", "[Grp] Dorohedoro - S00E01 (1080p).mkv"],
    )
    db.upsert_download(item)
    api.manager._register_completed_download(item)
    print("downloads:", [(d.name, d.is_batch, d.content_path) for d in db.downloads()])
    ova = (folder / "[Grp] Dorohedoro - S00E01 (1080p).mkv").resolve()
    # What the pre-d45e772 scan stored: the S00E01 file as TV episode 1.
    db.set_episode_identity(ova, MEDIA, 1, "Dorohedoro")
    assert db.episode_by_path(ova).episode == 1
    print("before:", {r.video_path.name: r.episode for r in db.episodes(MEDIA)})
    api.manager.scan_library()
    row = db.episode_by_path(ova)
    print("after rescan:", row.media_id, row.episode)
    assert row.episode is None, (row.media_id, row.episode)
