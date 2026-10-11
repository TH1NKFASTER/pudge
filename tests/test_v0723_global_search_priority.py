"""Global search ranks by what the user can open now, then by text match (behaviour; audit
71Б rewrite): ready local media, then watching / local but not ready, then Planning. AniList
suggestions (the fourth tier) are appended by the window after the local results; see
test_v0724_v34_recovery.py::test_global_search_shows_local_results_first_then_up_to_five_new_anilist_titles."""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api


def _episode(tmp_path: Path, media_id: int, title: str, state: str) -> LibraryEpisode:
    video = tmp_path / "library" / f"{title} - 01.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    return LibraryEpisode(media_id=media_id, title=title, episode=1, video_path=video, state=state)


def test_ready_local_media_outranks_better_text_matches_that_are_not_ready(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(media_id=1, title="Frieren Beyond Journey", status="CURRENT"))
    db.upsert_anime(LibraryAnime(media_id=2, title="Frieren", status="CURRENT"))
    db.upsert_anime(LibraryAnime(media_id=3, title="Frieren", status="PLANNING"))
    db.upsert_anime(LibraryAnime(media_id=4, title="Frieren Mini", status="CURRENT"))
    db.upsert_episode(_episode(tmp_path, 1, "Frieren Beyond Journey", "ready"))
    db.upsert_episode(_episode(tmp_path, 4, "Frieren Mini", "waiting_subtitles"))
    novel = tmp_path / "Frieren.txt"
    novel.write_text("第一章\n旅の始まり。", encoding="utf-8")
    book = api.light_novels.import_file(novel)

    rows = api.global_media_search("Frieren", 40)

    order = [(row["kind"], row.get("media_id") or row.get("local_id")) for row in rows]
    assert order == [
        ("light_novel", book["id"]),  # ready: imported light novel (exact title) ...
        ("anime", 1),  # ... and the anime with a ready episode (weaker match)
        ("anime", 2),  # watching, nothing local
        ("anime", 4),  # local episode still waiting for subtitles
        ("anime", 3),  # Planning, even with the exact title
    ]
    scores = {row.get("media_id"): row["score"] for row in rows if row["kind"] == "anime"}
    assert scores[1] < scores[3]  # the text score orders only inside a tier
    assert [row["page"] for row in rows if row["kind"] == "anime"] == [
        "current",
        "current",
        "current",
        "planned",
    ]
