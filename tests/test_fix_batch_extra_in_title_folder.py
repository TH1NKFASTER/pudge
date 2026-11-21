"""A batch's extra file in the title's folder is left alone by every library scan.

The scan looked the managed download up by the file's path, but a batch is keyed by its
folder. For the real layout (root/Dorohedoro/<batch>/) every full scan treated the
downloaded "Dorohedoro - OVA" as one of the person's own extras: set_episode_identity
moved it to the related OVA entry (or to no title), deleted its subtitle job, upserted
a phantom anime row, and only the later reconcile put it back.
"""

from __future__ import annotations

from pathlib import Path

from test_web_app import make_api

import pudge.database as dbm
from pudge.manager_models import DownloadItem, LibraryAnime

MEDIA = 105228
OVA_ENTRY = 110000
OVA_NAME = "[SubsPlease] Dorohedoro - OVA (1080p) [63C766E6].mkv"


def _job(db, name):
    return [dict(row) for row in db.subtitle_jobs() if Path(row["video_path"]).name == name]


def test_scan_keeps_batch_extra_untouched(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=MEDIA,
            title="Dorohedoro",
            status="CURRENT",
            episodes=12,
            media_status="FINISHED",
            format="TV",
        )
    )
    db.store_relation_graph(
        {
            "root_id": MEDIA,
            "nodes": [
                {"media_id": MEDIA, "title": "Dorohedoro", "format": "TV", "episodes": 12},
                {"media_id": OVA_ENTRY, "title": "Dorohedoro: Ma no Omake", "format": "OVA", "episodes": 6},
            ],
            "edges": [{"source": MEDIA, "target": OVA_ENTRY, "relation_type": "SIDE_STORY"}],
        },
        refreshed_at=1,
        next_refresh_at=9999999999,
    )
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *a, **k: ("none", None))
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    root = manager.config.library.root_dir.expanduser()
    folder = root / "Dorohedoro" / "[SubsPlease] Dorohedoro (01-12) (1080p) [Batch]"
    folder.mkdir(parents=True)
    for name in ("[SubsPlease] Dorohedoro - 01 (1080p) [AAAAAAAA].mkv", OVA_NAME):
        (folder / name).write_bytes(b"video" * 100)
    item = DownloadItem(
        torrent_hash="f9850c20",
        name=folder.name,
        state="uploading",
        progress=1.0,
        save_path=str(folder.parent),
        content_path=str(folder),
        media_id=MEDIA,
        is_batch=True,
        completed_on=1,
    )
    db.upsert_download(item)
    manager._register_completed_download(item)
    ova = (folder / OVA_NAME).resolve()
    before_row = db.episode_by_path(ova)
    before_job = [
        (job["state"], job["attempts"], job["media_id"], job["action_code"]) for job in _job(db, OVA_NAME)
    ]
    assert before_row is not None and before_row.media_id == MEDIA

    calls = []
    original = dbm.Database.set_episode_identity
    monkeypatch.setattr(
        dbm.Database, "set_episode_identity", lambda self, *a: calls.append(a) or original(self, *a)
    )
    for _scan in range(2):
        manager._scan_library_uncached()

    assert calls == []
    after_row = db.episode_by_path(ova)
    assert (after_row.media_id, after_row.episode, after_row.torrent_hash) == (
        before_row.media_id,
        before_row.episode,
        before_row.torrent_hash,
    )
    assert [
        (job["state"], job["attempts"], job["media_id"], job["action_code"]) for job in _job(db, OVA_NAME)
    ] == before_job
    assert db.get_anime(OVA_ENTRY) is None
    # The TV episode is still an ordinary episode of the series.
    episode = db.episode_by_path((folder / "[SubsPlease] Dorohedoro - 01 (1080p) [AAAAAAAA].mkv").resolve())
    assert (episode.media_id, episode.episode) == (MEDIA, 1)
