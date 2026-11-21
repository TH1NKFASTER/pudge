"""OVA and movie files in the person's own folder of a series, and Delete of a downloaded
extra.

The folder match used to attach "Frieren - OVA" to the series without an episode number
(searched for subtitles forever) and "Frieren - OVA 02" as episode 2 of the series. Now an
OVA or movie belongs to the series' only fitting related AniList entry, otherwise it is a
title of its own. Deleting a downloaded extra also unselects it in the torrent client, so
the client does not fetch it again.
"""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime
from test_fix_batch_extra_files import MEDIA, _series_with_ova
from test_web_app import make_api

SUBTITLE = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは、世界。\n"
SERIES = 154587


def _library(tmp_path: Path, names: list[str], *, graph: bool, ova_episodes: int = 3):
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=SERIES,
            title="Frieren",
            status="COMPLETED",
            media_status="FINISHED",
            progress=2,
            episodes=2,
            format="TV",
        )
    )
    if graph:
        db.store_relation_graph(
            {
                "root_id": SERIES,
                "nodes": [
                    {"media_id": SERIES, "title": "Frieren", "format": "TV", "episodes": 2},
                    {
                        "media_id": 170000,
                        "title": "Frieren: Mini Anime",
                        "format": "OVA",
                        "episodes": ova_episodes,
                        "media_status": "FINISHED",
                    },
                ],
                "edges": [{"source": SERIES, "target": 170000, "relation_type": "SIDE_STORY"}],
            },
            refreshed_at=1,
            next_refresh_at=9999999999,
        )
    folder = api.config.library.root_dir / "Frieren"
    folder.mkdir(parents=True)
    for name in names:
        video = folder / name
        video.write_bytes(b"video")
        if "- 0" in name and "OVA" not in name:
            video.with_suffix(".ja.srt").write_text(SUBTITLE, encoding="utf-8")
    api.manager.scan_library()
    return api, folder


def _identity(api, folder: Path, name: str):
    row = api.manager.db.episode_by_path((folder / name).resolve())
    return (row.media_id, row.episode)


def _home(api) -> dict[str, list[str]]:
    home = api.get_state()["home"]
    return {key: [str(card.get("title")) for card in rows] for key, rows in home.items() if rows}


def test_own_ova_belongs_to_the_related_ova_entry(tmp_path: Path) -> None:
    api, folder = _library(
        tmp_path,
        ["[G] Frieren - 01 (1080p).mkv", "[G] Frieren - 02 (1080p).mkv", "[G] Frieren - OVA 02 (1080p).mkv"],
        graph=True,
    )
    # Episode 2 of the OVA, not a second "episode 2" of the series.
    assert _identity(api, folder, "[G] Frieren - OVA 02 (1080p).mkv") == (170000, 2)
    assert _identity(api, folder, "[G] Frieren - 02 (1080p).mkv") == (SERIES, 2)
    home = _home(api)
    assert home["completed_ready"] == ["Frieren"]
    assert home["waiting"] == ["Frieren: Mini Anime"]
    # AniList untouched: the OVA entry is a local title off the list.
    assert api.manager.db.get_anime(170000).status == ""
    # The next scan keeps it there.
    api.manager.scan_library()
    assert _identity(api, folder, "[G] Frieren - OVA 02 (1080p).mkv") == (170000, 2)


def test_own_ova_without_a_related_entry_is_a_title_of_its_own(tmp_path: Path) -> None:
    api, folder = _library(
        tmp_path, ["[G] Frieren - 01 (1080p).mkv", "[G] Frieren - OVA (1080p).mkv"], graph=False
    )
    assert _identity(api, folder, "[G] Frieren - OVA (1080p).mkv")[0] is None
    home = _home(api)
    assert home["completed_ready"] == ["Frieren"]
    assert home["waiting"] == ["Frieren - OVA"]


def test_deleting_a_downloaded_extra_unselects_it_in_the_torrent_client(tmp_path: Path, monkeypatch) -> None:
    api, folder, ova = _series_with_ova(tmp_path, monkeypatch)
    calls: list[tuple] = []

    class Client:
        def files(self, torrent_hash):
            return [
                {
                    "index": 0,
                    "name": f"{folder.name}/[SubsPlease] Dorohedoro - 01 (1080p).mkv",
                    "priority": 1,
                },
                {"index": 3, "name": f"{folder.name}/{ova.name}", "priority": 1},
            ]

        def set_file_priority(self, torrent_hash, indices, priority):
            calls.append((torrent_hash, list(indices), priority))

        def close(self):
            pass

    monkeypatch.setattr(api.manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(api.manager, "torrent_clients", lambda: [("aria2", Client())])
    assert api.extra_file_delete(str(ova))["ok"] is True
    assert calls == [("f9850c20", [3], 0)]
    assert not ova.exists()
    assert api.manager.db.get_anime(MEDIA) is not None
