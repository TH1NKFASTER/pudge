"""A folder named after season 1 does not make a file that says "S2" a season 1 episode.

In the managed library, "Jujutsu Kaisen/[SubsPlease] Jujutsu Kaisen S2 - 01.mkv",
"... 2nd Season - 02", "Jujutsu.Kaisen.S02E03" and Plex-style "Jujutsu Kaisen/Season 2/"
files all became Jujutsu Kaisen (season 1) episodes because the parent-folder match
came before any season check: duplicate episodes, wrong subtitles, wrong AniList
progress.
"""

from __future__ import annotations

from pathlib import Path

from test_web_app import make_api

from pudge import library as lib
from pudge.manager_models import LibraryAnime

S1, S2 = 113415, 145064


def _s1(**extra) -> LibraryAnime:
    return LibraryAnime(
        media_id=S1,
        title="Jujutsu Kaisen",
        titles=["JUJUTSU KAISEN"],
        status="COMPLETED",
        episodes=24,
        media_status="FINISHED",
        format="TV",
        **extra,
    )


def _s2() -> LibraryAnime:
    return LibraryAnime(
        media_id=S2,
        title="Jujutsu Kaisen 2nd Season",
        titles=["JUJUTSU KAISEN Season 2"],
        status="CURRENT",
        episodes=23,
        media_status="FINISHED",
        format="TV",
        relations=[{"relation_type": "PREQUEL", "title": "Jujutsu Kaisen", "media_id": S1}],
    )


NAMES = {
    "[SubsPlease] Jujutsu Kaisen - 01 (1080p).mkv": S1,
    "[SubsPlease] Jujutsu Kaisen S2 - 01 (1080p).mkv": S2,
    "[Erai-raws] Jujutsu Kaisen 2nd Season - 02 [1080p].mkv": S2,
    "Jujutsu.Kaisen.S02E03.1080p.WEB.H264-VARYG.mkv": S2,
    "Season 2/Jujutsu Kaisen - 04 (1080p).mkv": S2,
}


def _layout(tmp_path: Path, monkeypatch, entries):
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    for entry in entries:
        db.upsert_anime(entry)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    root = manager.config.library.root_dir.expanduser()
    folder = root / "Jujutsu Kaisen"
    for name in NAMES:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"video" * 100)
    return manager, db, root, folder


def _mapped(rows) -> dict[str, int | None]:
    return {row.video_path.name: row.media_id for row in rows}


def test_s2_files_in_the_s1_folder_go_to_season_2(tmp_path: Path, monkeypatch):
    manager, db, root, _folder = _layout(tmp_path, monkeypatch, [_s1(), _s2()])
    mapped = _mapped(lib.scan_library(root, db, recursive=True))
    assert mapped == {Path(name).name: media for name, media in NAMES.items()}
    mapped = _mapped(manager.scan_library())
    assert mapped == {Path(name).name: media for name, media in NAMES.items()}
    assert [row.episode for row in db.episodes(S2)] == [1, 2, 3, 4]


def test_s2_files_without_the_s2_entry_are_not_season_1(tmp_path: Path, monkeypatch):
    sequel = {"relation_type": "SEQUEL", "title": "Jujutsu Kaisen 2nd Season", "media_id": S2}
    _manager, db, root, _folder = _layout(tmp_path, monkeypatch, [_s1(relations=[sequel])])
    mapped = _mapped(lib.scan_library(root, db, recursive=True))
    assert mapped.pop("[SubsPlease] Jujutsu Kaisen - 01 (1080p).mkv") == S1
    assert set(mapped.values()) == {None}


def test_anilist_id_marker_and_named_sequel_folders_are_unchanged(tmp_path: Path, monkeypatch):
    manager, db, root, folder = _layout(tmp_path, monkeypatch, [_s1(), _s2()])
    (folder / ".anilist.id").write_text(str(S1), encoding="utf-8")
    assert set(_mapped(lib.scan_library(root, db, recursive=True)).values()) == {S1}

    # A named sequel (no number in its titles) keeps its folder match for "S2" files.
    named = LibraryAnime(
        media_id=33255,
        title="Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen",
        episodes=12,
        format="TV",
        media_status="FINISHED",
        relations=[{"relation_type": "PREQUEL", "title": "Shouwa Genroku Rakugo Shinjuu", "media_id": 1}],
    )
    db.upsert_anime(named)
    rakugo = root / "Shouwa Genroku Rakugo Shinjuu Sukeroku Futatabi-hen"
    rakugo.mkdir()
    (rakugo / "[sam] Shouwa Genroku Rakugo Shinjuu S2 - 03 [1080p].mkv").write_bytes(b"video" * 100)
    mapped = _mapped(lib.scan_library(root, db, recursive=True))
    assert mapped["[sam] Shouwa Genroku Rakugo Shinjuu S2 - 03 [1080p].mkv"] == 33255


def _scan_one(tmp_path: Path, monkeypatch, entries, name: str, graph=None):
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    for entry in entries:
        db.upsert_anime(entry)
    if graph is not None:
        db.store_relation_graph(graph, refreshed_at=1, next_refresh_at=9999999999)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *a, **k: ("none", None, None))
    video = manager.config.library.root_dir.expanduser() / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video" * 100)
    rows = {row.video_path.name: row for row in manager.scan_library()}
    row = rows[video.name]
    return row.media_id, row.episode


ONE_PIECE = LibraryAnime(
    media_id=21,
    title="ONE PIECE",
    titles=["ONE PIECE", "One Piece"],
    status="CURRENT",
    episodes=None,
    media_status="RELEASING",
    format="TV",
    relations=[
        {
            "relation_type": "SIDE_STORY",
            "media_id": 459,
            "title": "ONE PIECE FILM: STRONG WORLD",
            "format": "MOVIE",
        }
    ],
)
SONARR_ONE_PIECE = "One Piece/Season 21/One Piece - S21E1050 - Title.mkv"


def test_sonarr_layout_of_a_single_entry_long_show_keeps_its_title(tmp_path: Path, monkeypatch):
    assert _scan_one(tmp_path, monkeypatch, [ONE_PIECE], SONARR_ONE_PIECE) == (21, 1050)


def test_sonarr_layout_with_a_cached_graph_of_movies_only(tmp_path: Path, monkeypatch):
    graph = {
        "root_id": 21,
        "nodes": [
            {"media_id": 21, "title": "ONE PIECE", "format": "TV"},
            {"media_id": 459, "title": "ONE PIECE FILM: STRONG WORLD", "format": "MOVIE"},
        ],
        "edges": [{"source": 21, "target": 459, "relation_type": "SIDE_STORY"}],
    }
    assert _scan_one(tmp_path, monkeypatch, [ONE_PIECE], SONARR_ONE_PIECE, graph) == (21, 1050)


BLEACH = 269
TYBW = 116674


def _bleach(**extra) -> LibraryAnime:
    return LibraryAnime(
        media_id=BLEACH,
        title="BLEACH",
        titles=["BLEACH", "Bleach"],
        status="COMPLETED",
        episodes=366,
        media_status="FINISHED",
        format="TV",
        **extra,
    )


SONARR_BLEACH = "Bleach/Season 17/Bleach - S17E01 - The Blood Warfare.mkv"


def test_bleach_season_17_is_not_bleach_episode_1_when_tybw_is_known(tmp_path: Path, monkeypatch):
    sequel = {
        "relation_type": "SEQUEL",
        "media_id": TYBW,
        "title": "BLEACH: Sennen Kessen-hen",
        "format": "TV",
    }
    media, _episode = _scan_one(tmp_path, monkeypatch, [_bleach(relations=[sequel])], SONARR_BLEACH)
    assert media is None


def test_bleach_season_17_with_tybw_only_in_the_cached_graph(tmp_path: Path, monkeypatch):
    graph = {
        "root_id": BLEACH,
        "nodes": [
            {"media_id": BLEACH, "title": "BLEACH", "format": "TV"},
            {"media_id": TYBW, "title": "BLEACH: Sennen Kessen-hen", "format": "TV"},
            {"media_id": 159322, "title": "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan", "format": "TV"},
        ],
        "edges": [
            {"source": BLEACH, "target": TYBW, "relation_type": "SEQUEL"},
            {"source": TYBW, "target": BLEACH, "relation_type": "PREQUEL"},
            {"source": TYBW, "target": 159322, "relation_type": "SEQUEL"},
        ],
    }
    media, _episode = _scan_one(tmp_path, monkeypatch, [_bleach()], SONARR_BLEACH, graph)
    assert media is None


def test_bleach_season_17_with_nothing_known_stays_as_before(tmp_path: Path, monkeypatch):
    assert _scan_one(tmp_path, monkeypatch, [_bleach()], SONARR_BLEACH) == (BLEACH, 1)


def test_a_named_sequels_own_sonarr_folder_keeps_it(tmp_path: Path, monkeypatch):
    tybw = LibraryAnime(
        media_id=TYBW,
        title="BLEACH: Sennen Kessen-hen",
        titles=["BLEACH: Sennen Kessen-hen", "Bleach: Thousand-Year Blood War"],
        status="CURRENT",
        episodes=13,
        media_status="FINISHED",
        format="TV",
        relations=[{"relation_type": "PREQUEL", "media_id": BLEACH, "title": "BLEACH", "format": "TV"}],
    )
    name = "BLEACH Sennen Kessen-hen/Season 17/Bleach - S17E05 - Title.mkv"
    assert _scan_one(tmp_path, monkeypatch, [tybw], name) == (TYBW, 5)
