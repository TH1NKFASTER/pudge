"""A title whose download is under way waits for preparation like the others.

User: "why are they in different groups? I think they should be in the preparation group."
Two season packs were downloading: one title, with a finished file of its pack already on
disk, was under "Waiting for preparation"; the other, with no file finished yet, stayed under
"Available to download" with its percentage. A download under way is now always
"Waiting for preparation"; "Available to download" keeps only titles with a Download button.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_web_app import make_api

from pudge.manager_models import DownloadItem, LibraryAnime


def _home(api) -> dict:
    return api.get_state()["home"]


def _pack(tmp_path: Path, media_id: int) -> DownloadItem:
    # A season pack with no episode file finished yet (Sora yori mo Tooi Basho at 82 %).
    folder = tmp_path / "library" / "Show" / "[Group] Show (BD 1080p)"
    return DownloadItem(
        "c" * 40,
        "[Group] Show (BD 1080p)",
        "downloading",
        0.82,
        str(folder.parent),
        str(folder),
        media_id=media_id,
        episode=None,
        is_batch=True,
        raw={"backend": "aria2"},
    )


@pytest.mark.parametrize("status", ["PLANNING", "CURRENT"])
def test_a_title_downloading_its_first_file_waits_for_preparation(tmp_path: Path, status: str) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=42, title="Show", status=status, episodes=12, media_status="FINISHED")
    )
    api.manager.db.upsert_download(_pack(tmp_path, 42))
    home = _home(api)
    assert [card["media_id"] for card in home["waiting"]] == [42]
    assert home["waiting"][0]["download"]["progress"] == pytest.approx(0.82)
    assert home["download_available"] == []


def test_a_title_without_a_download_is_still_available_to_download(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=43, title="Other", status="CURRENT", episodes=12, media_status="FINISHED")
    )
    home = _home(api)
    assert [card["media_id"] for card in home["download_available"]] == [43]
    assert "download" not in home["download_available"][0]
