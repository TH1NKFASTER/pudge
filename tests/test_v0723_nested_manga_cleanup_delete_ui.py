"""Nested manga packs, cleanup, destructive buttons, series cards, AniList scores and the
manga reader's page progress of v0.7.23 (behaviour; audit 71Б rewrite).

Window checks run index.html in node (tests/page_harness.py, fixture `page`); stylesheet
checks read parsed rules (tests/css_support.py)."""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from css_support import css_rules
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.manga import MangaService, _natural_key
from test_web_app import make_api
from tests.i18n_support import en, ru


def _write_image(path: Path, shade: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (3, 3), (shade, shade, shade))
    data = io.BytesIO()
    image.save(data, format="JPEG")
    path.write_bytes(data.getvalue())


def test_nested_mang_zip_pack_imports_one_book_per_volume(tmp_path: Path) -> None:
    pack = tmp_path / "Mang-Zip.info_Akagi Genius Darkness vol 01-36"
    bundle = pack / "Mang-Zip.info_Akagi Genius Darkness v01-05"
    vol1 = bundle / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第01巻"
    vol2 = bundle / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第02巻"

    for index, name in enumerate(
        ["Mang-Zip.info_AKAGI_017.jpg", "Mang-Zip.info_AKAGI_018-019.jpg", "Mang-Zip.info_AKAGI_020.jpg"]
    ):
        _write_image(vol1 / name, 250 - index)
    for index, name in enumerate(["Mang-Zip.info_AKAGI_001.jpg", "Mang-Zip.info_AKAGI_002.jpg"]):
        _write_image(vol2 / name, 240 - index)

    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")

    found = service.discover_image_files(pack)
    books = service.import_image_groups(found)

    assert len(found) == 5
    assert len(books) == 2
    assert [book["volume"] for book in books] == [1, 2]
    assert [book["page_count"] for book in books] == [3, 2]
    assert books[0]["series_key"] == books[1]["series_key"]
    assert "Mang-Zip.info" not in books[0]["title"]
    assert "福本伸行" not in books[0]["title"]
    assert books[0]["series_title"] == "アカギ ~闇に降り立った天才~"

    names = [
        "Mang-Zip.info_AKAGI_020.jpg",
        "Mang-Zip.info_AKAGI_018-019.jpg",
        "Mang-Zip.info_AKAGI_017.jpg",
    ]
    assert sorted(names, key=_natural_key) == [
        "Mang-Zip.info_AKAGI_017.jpg",
        "Mang-Zip.info_AKAGI_018-019.jpg",
        "Mang-Zip.info_AKAGI_020.jpg",
    ]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    cfg.agent.delete_after_watched_hours = 24
    return AnimeManager(cfg, log=lambda _message: None)


def test_cleanup_removes_managed_episodes_covered_by_anilist_progress(tmp_path: Path) -> None:
    # Progress 7 counts episodes 6 and 7 (also the one at the progress): both
    # become watched with the usual delete delay; a file Pudge does not own stays.
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=200637,
            title="100 Girlfriends S3",
            status="CURRENT",
            progress=7,
            episodes=12,
            format="TV",
        )
    )

    managed6 = tmp_path / "library" / "S03E06.mkv"
    managed7 = tmp_path / "library" / "S03E07.mkv"
    unmanaged5 = tmp_path / "external-S03E05.mkv"
    for path in (managed6, managed7, unmanaged5):
        path.write_bytes(b"video")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=6,
            media_episode=6,
            release_episode=6,
            video_path=managed6,
            state="ready",
        ),
        downloaded_at=1,
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=7,
            media_episode=7,
            release_episode=7,
            video_path=managed7,
            state="ready",
        ),
        downloaded_at=1,
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=5,
            media_episode=5,
            release_episode=5,
            video_path=unmanaged5,
            state="ready",
        )
    )

    assert manager.cleanup() == 0
    delay = manager.config.agent.delete_after_watched_hours * 3600.0
    for path in (managed6, managed7):
        row = manager.db.episode_by_path(path)
        assert row.state == "watched"
        assert abs(row.delete_after - (time.time() + delay)) < 60
    assert manager.db.episode_by_path(unmanaged5).state == "ready"

    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE state='watched'", (time.time() - 1,))
    deleted = manager.cleanup()

    assert deleted == 2
    assert not managed6.exists()
    assert not managed7.exists()
    assert unmanaged5.exists()


def test_nested_mangazip_aliases_become_one_series_and_reimport_migrates_legacy(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    root = tmp_path / "Mang-Zip.info_Akagi Genius Darkness vol 01-36"
    folders = [
        root
        / "Mang-Zip.info_Akagi Genius Darkness v01-05"
        / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第01巻",
        root
        / "Mang-Zip.info_Akagi Genius Darkness v01-05"
        / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第02巻",
        root / "Mang-Zip.info_Akagi Genius Darkness v06-10" / "Mang-Zip.info_ƒAƒJƒM ‘æ06Ša",
        root
        / "Mang-Zip.info_Akagi Genius Darkness v06-10"
        / "Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第09巻",
        root / "Mang-Zip.info_Akagi Genius Darkness v28",
        root / "Mang-Zip.info_Akagi Genius Darkness v30-31" / "Mang-Zip.info_30",
        root / "Mang-Zip.info_Akagi Genius Darkness v32",
    ]
    pages: list[Path] = []
    for index, folder in enumerate(folders, 1):
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / f"page-{index:02d}.jpg"
        page.write_bytes(b"not-a-real-jpeg")
        pages.append(page)
    legacy = service.import_images([pages[2]], title=folders[2].name)
    legacy_id = int(legacy["id"])
    books = service.import_image_groups(pages, source_root=root)
    assert len(books) == 7
    assert sorted(int(book["volume"]) for book in books) == [1, 2, 6, 9, 28, 30, 32]
    assert len({book["series_key"] for book in books}) == 1
    assert {book["series_title"] for book in books} == {"アカギ ~闇に降り立った天才~"}
    assert any(int(book["id"]) == legacy_id and int(book["volume"]) == 6 for book in books)
    assert not any("ƒAƒJƒM" in str(book["title"]) for book in books)
    assert not any(str(book["title"]).strip() in {"30", "31"} for book in books)


def test_manga_mean_score_is_persisted_and_personal_score_is_series_scoped(
    tmp_path: Path,
) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(manga_books)").fetchall()}
    assert "mean_score" in columns

    service = MangaService(db, cache_dir=tmp_path / "cache")
    paths = [tmp_path / "Score Series Vol. 1.cbz", tmp_path / "Score Series Vol. 2.cbz"]
    for path in paths:
        with zipfile.ZipFile(path, "w") as archive:
            buffer = io.BytesIO()
            Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
            archive.writestr("001.jpg", buffer.getvalue())

    one = service.import_file(paths[0])
    two = service.import_file(paths[1])
    service.bind_anilist(
        int(one["id"]),
        12345,
        site_url="https://anilist.co/manga/12345",
        user_score=7.0,
        mean_score=84.0,
    )

    linked_ids = {int(one["id"]), int(two["id"])}
    linked = [book for book in service.state()["books"] if int(book["id"]) in linked_ids]
    assert {book["mean_score"] for book in linked} == {84.0}
    assert {book["user_score"] for book in linked} == {7.0}

    service.set_score(int(two["id"]), 9.0)
    linked = [book for book in service.state()["books"] if int(book["id"]) in linked_ids]
    assert {book["user_score"] for book in linked} == {9.0}


def test_existing_linked_manga_can_backfill_public_anilist_mean_score(tmp_path: Path) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    path = tmp_path / "Backfill Series Vol. 1.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
        archive.writestr("001.jpg", buffer.getvalue())

    book = service.import_file(path)
    service.bind_anilist(int(book["id"]), 99999, site_url="https://anilist.co/manga/99999")
    assert service._payload(service._book(int(book["id"])))["mean_score"] is None

    assert service.set_mean_scores({99999: 84.0}) == 1
    updated = service._payload(service._book(int(book["id"])))
    assert updated["mean_score"] == 84.0


def test_manga_page_fetch_is_read_only_and_read_pages_are_monotonic(tmp_path: Path) -> None:
    import zipfile

    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(manga_books)").fetchall()}
    assert "read_pages" in columns

    service = MangaService(db, cache_dir=tmp_path / "cache")
    path = tmp_path / "Progress Series Vol. 1.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(3):
            buffer = io.BytesIO()
            Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
            archive.writestr(f"{index + 1:03d}.jpg", buffer.getvalue())

    book = service.import_file(path)
    book_id = int(book["id"])
    assert service._payload(service._book(book_id))["read_pages"] == 0

    service.page(book_id, 1)
    fetched = service._payload(service._book(book_id))
    assert fetched["read_pages"] == 0
    assert fetched["position"] == 0

    service.set_position(book_id, 1)
    positioned = service._payload(service._book(book_id))
    assert positioned["position"] == 1
    assert positioned["read_pages"] == 0

    service.mark_read(book_id, 1)
    assert service._payload(service._book(book_id))["read_pages"] == 2
    service.mark_read(book_id, 0)
    assert service._payload(service._book(book_id))["read_pages"] == 2
    service.mark_read(book_id, 2)
    assert service._payload(service._book(book_id))["read_pages"] == 3


def test_physical_spread_pages_are_reported_as_spreads(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    source = tmp_path / "spread-test"
    source.mkdir()

    portrait = source / "017.jpg"
    spread = source / "018-019.jpg"
    after = source / "020.jpg"
    Image.new("RGB", (600, 900), "white").save(portrait)
    Image.new("RGB", (1200, 900), "white").save(spread)
    Image.new("RGB", (600, 900), "white").save(after)

    book = service.import_images([portrait, spread, after], title="Spread Test Vol. 1")
    first = service.page(int(book["id"]), 0)
    middle = service.page(int(book["id"]), 1)
    last = service.page(int(book["id"]), 2)

    assert first["spread"] is False
    assert middle["spread"] is True
    assert "018-019" in middle["name"]
    assert last["spread"] is False


# --- Import ---------------------------------------------------------------------------------


def _pack(root: Path, volumes: tuple[int, ...] = (1, 2)) -> Path:
    """A Mang-Zip style pack: pack / bundle / one folder of pages per volume."""
    for volume in volumes:
        folder = (
            root
            / "Mang-Zip.info_Akagi v01-05"
            / f"Mang-Zip.info_[福本伸行] アカギ ～闇に降り立った天才～ 第{volume:02d}巻"
        )
        for page in range(2):
            _write_image(folder / f"Mang-Zip.info_AKAGI_{page + 1:03d}.jpg", 250 - page)
    return root


def _cbz(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30), "white").save(buffer, format="JPEG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", buffer.getvalue())
    return path


def test_dropping_a_nested_manga_pack_imports_one_book_per_volume(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    pack = _pack(tmp_path / "Mang-Zip.info_Akagi vol 01-36")

    result = api.import_dropped_paths([str(pack)])

    books = [row["book"] for row in result["imports"] if row["kind"] == "manga"]
    assert result["errors"] == []
    assert sorted(int(book["volume"]) for book in books) == [1, 2]
    assert [int(book["page_count"]) for book in books] == [2, 2]
    assert len({book["series_key"] for book in books}) == 1


def test_manga_import_button_imports_every_volume_under_the_chosen_folder(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    pack = _pack(tmp_path / "Downloads" / "Akagi")
    _cbz(pack / "extras" / "アカギ 第03巻.cbz")
    asked: list[tuple] = []

    class Window:
        def create_file_dialog(self, kind, **options):
            asked.append((kind, options))
            return [str(pack)]

    monkeypatch.setitem(__import__("sys").modules, "webview", SimpleNamespace(FOLDER_DIALOG=20))
    api.window = Window()
    result = api.choose_manga_folder()

    # A folder picker that starts in Downloads; everything below the folder is imported.
    assert asked == [(20, {"directory": str(Path.home() / "Downloads"), "allow_multiple": False})]
    assert result["cancelled"] is False and result["errors"] == []
    assert sorted(int(book["volume"]) for book in result["books"]) == [1, 2, 3]
    assert len({book["series_key"] for book in result["books"] if int(book["page_count"]) == 2}) == 1


# --- AniList scores (backend) ---------------------------------------------------------------


def _anilist_api(tmp_path: Path, monkeypatch, answer) -> tuple:
    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "token"
    asked: list[dict] = []

    def post(query: str, variables: dict) -> dict:
        asked.append(dict(variables))
        return answer(query, variables)

    monkeypatch.setattr(api.light_novels, "_anilist_post", post)
    return api, asked


def test_manga_anilist_search_shows_and_binds_the_public_mean_score(tmp_path: Path, monkeypatch) -> None:
    def answer(query: str, _variables: dict) -> dict:
        media = {
            "id": 86082,
            "format": "MANGA",
            "chapters": 97,
            "volumes": 14,
            "status": "FINISHED",
            "title": {"userPreferred": "Dungeon Meshi"},
            "coverImage": {"large": ""},
            "siteUrl": "https://anilist.co/manga/86082",
            "mediaListEntry": None,
        }
        if "meanScore" in query:  # AniList answers only the fields asked for
            media["meanScore"] = 84
        return {"Page": {"media": [media]}}

    api, _asked = _anilist_api(tmp_path, monkeypatch, answer)
    first = api.manga.import_file(_cbz(tmp_path / "Dungeon Meshi Vol. 1.cbz"))
    second = api.manga.import_file(_cbz(tmp_path / "Other Vol. 1.cbz"))

    [row] = api.manga_search_anilist("Dungeon Meshi")
    assert row["mean_score"] == 84.0
    assert api.manga_bind_anilist(int(first["id"]), 86082, row)["book"]["mean_score"] == 84.0
    # Binding without the chosen row looks the title up again and keeps the score too.
    assert api.manga_bind_anilist(int(second["id"]), 86082)["book"]["mean_score"] == 84.0


def test_manga_tab_fills_missing_public_scores_in_the_background_once(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()

    def answer(query: str, variables: dict) -> dict:
        release.wait(5)  # a slow AniList
        scores = {86082: 84, 999: None} if "meanScore" in query else {}  # 999: AniList has no score yet
        return {
            "Page": {
                "media": [
                    {"id": media_id, "meanScore": scores.get(media_id)} for media_id in variables["ids"]
                ]
            }
        }

    api, asked = _anilist_api(tmp_path, monkeypatch, answer)
    linked = api.manga.import_file(_cbz(tmp_path / "Dungeon Meshi Vol. 1.cbz"))
    scored = api.manga.import_file(_cbz(tmp_path / "Frieren Vol. 1.cbz"))
    api.manga.bind_anilist(int(linked["id"]), 86082, site_url="https://anilist.co/manga/86082")
    api.manga.bind_anilist(
        int(scored["id"]), 118586, site_url="https://anilist.co/manga/118586", mean_score=90.0
    )
    unscored = api.manga.import_file(_cbz(tmp_path / "New Series Vol. 1.cbz"))
    api.manga.bind_anilist(int(unscored["id"]), 999, site_url="https://anilist.co/manga/999")
    stored = threading.Event()
    original = api.manga.set_mean_scores
    monkeypatch.setattr(api.manga, "set_mean_scores", lambda scores: (original(scores), stored.set())[0])

    scores = lambda state: {int(book["id"]): book["mean_score"] for book in state["books"]}  # noqa: E731
    # Opening the tab answers at once from local data while AniList is still busy.
    expected = {int(linked["id"]): None, int(scored["id"]): 90.0, int(unscored["id"]): None}
    assert scores(api.manga_state()) == expected
    release.set()
    assert stored.wait(5)
    assert scores(api.manga_state()) == {**expected, int(linked["id"]): 84.0}
    api.manga_state()
    # Only volumes without a score were asked about, and only once per run of Pudge
    # (a title AniList has not scored yet is not asked again on every visit).
    assert asked == [{"ids": [999, 86082]}]


# --- Library window -------------------------------------------------------------------------

LN_BOOKS = [
    {
        "id": 1,
        "title": "Spice 1",
        "series_title": "Spice",
        "series_key": "spice",
        "volume": 1,
        "anilist_id": 31,
        "anilist_mean_score": 84,
        "character_count": 1000,
    },
    {
        "id": 2,
        "title": "Spice 2",
        "series_title": "Spice",
        "series_key": "spice",
        "volume": 2,
        "anilist_id": 31,
        "anilist_mean_score": 84,
        "character_count": 1000,
    },
    {
        "id": 9,
        "title": "Solo",
        "volume": 1,
        "anilist_id": 5,
        "anilist_mean_score": 70,
        "character_count": 5000,
        "chapter_count": 12,
    },
]
MANGA_BOOKS = [
    {
        "id": 11,
        "title": "Food v1",
        "series_title": "Food",
        "series_key": "food",
        "volume": 1,
        "page_count": 10,
        "read_pages": 3,
        "anilist_id": 86082,
        "mean_score": 84,
    },
    {
        "id": 12,
        "title": "Food v2",
        "series_title": "Food",
        "series_key": "food",
        "volume": 2,
        "page_count": 10,
        "anilist_id": 86082,
        "mean_score": 84,
    },
    {
        "id": 20,
        "title": "Solo v1",
        "series_title": "Solo",
        "series_key": "solo",
        "volume": 1,
        "page_count": 10,
        "read_pages": 3,
        "anilist_id": 7,
        "mean_score": 61,
    },
]
LIBRARY = {
    "light_novel_state": {"version": 1, "settings": {}, "books": LN_BOOKS},
    "manga_state": {"books": MANGA_BOOKS, "ocr_available": True},
}
MENU = r"""
const menu = async selector => {
  await __fire(selector, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
  const items = [...document.querySelectorAll('#contextMenu button')].filter(b => b.checkVisibility());
  return items;
};
const actions = items => items.map(b => b.dataset.lnContextAction || b.dataset.mangaContextAction);
"""


def test_destructive_actions_are_red_everywhere(page) -> None:
    downloads = [
        {
            "hash": "aaa",
            "name": "[Sub] Show - 03.mkv",
            "anime_title": "Show",
            "episode": 3,
            "media_id": 7,
            "state": "downloading",
            "progress": 0.4,
            "backend": "qBittorrent",
            "backend_id": "qbittorrent",
            "total_bytes": 1000,
            "downloaded_bytes": 400,
        }
    ]
    result = page.run(
        MENU
        + r"""
        const red = button => button.matches('button.danger-action,button.selection-delete,button.uninstall-pudge');
        const destructive = items => items.filter(b => /delete|remove/.test(b.dataset.lnContextAction || b.dataset.mangaContextAction || ''))
          .map(b => [b.dataset.lnContextAction || b.dataset.mangaContextAction, red(b)]);
        const out = {};
        await openDownloadCenter(); await __tick(20);
        out.torrent = [...document.querySelectorAll('#modalBody [data-action^="torrent-"]')].map(b => [b.dataset.action, red(b)]);
        await __click('#modalClose'); await __tick(10);
        setPage('lightnovels'); await __tick(100);
        out.ln = [...destructive(await menu('#lightNovelsContent [data-ln-book="9"]')),
                  ...destructive(await menu('#lightNovelsContent [data-ln-series-key="spice"] .ln-series-head'))];
        await __key('Escape');
        await __click('#lightNovelsContent [data-ln-book="9"]', {metaKey: true}); await __tick(10);
        out.selection = [...document.querySelectorAll('#pageSelectionActions button')].map(b => [b.dataset.selectionAction, red(b)]);
        clearLnSelection();
        setPage('manga'); await __tick(100);
        out.manga = [...destructive(await menu('#mangaContent [data-manga-book="20"]')),
                     ...destructive(await menu('#mangaContent [data-manga-series-key="food"] .ln-series-head'))];
        await __key('Escape');
        setPage('settings'); await __tick(50);
        out.uninstall = red($('uninstallPudge'));
        return out;
        """,
        api={
            **LIBRARY,
            "torrent_downloads": {"downloads": downloads, "backend": "qBittorrent", "observed_at": 1},
        },
    )
    out = result.value
    torrent = dict(out["torrent"])
    assert torrent["torrent-remove"] is True and torrent["torrent-remove-files"] is True
    assert not [
        action for action, is_red in out["torrent"] if is_red and "remove" not in action
    ]  # pause etc. stay normal
    assert out["ln"] == [["delete", True], ["delete-series", True]]
    assert out["manga"] == [["remove-series", True], ["remove-series", True]]
    assert dict(out["selection"]) == {"clear": False, "delete": True}
    assert out["uninstall"] is True
    rules = css_rules("index.html")
    for selector in ("button.danger-action", "button.selection-delete"):
        assert rules.get(selector, "background") == "rgba(255,107,125,.16)"
        assert rules.get(selector, "border-color") == "var(--danger)"
        assert rules.get(selector + ":hover", "background") == "rgba(255,107,125,.28)"
    assert rules.get(".context-menu button.danger-action", "color") == "#ff9aac"
    assert rules.get("button.uninstall-pudge", "background") == "#a51f33"


# Jiten answers per AniList id: the series' difficulty, with per-volume decks for volume 1.
JITEN = r"""
window.DIFFICULTY = {5: 0, 31: 2, 86082: 3, 77: 4, 7: 5};
window.JITEN_DELAY = 0;
__api.planning_jiten_stats = async mediaId => {
  if (JITEN_DELAY) await new Promise(resolve => setTimeout(resolve, JITEN_DELAY));
  return {available: true, difficulty: DIFFICULTY[mediaId] ?? 1, word_count: 90000, unique_word_count: 7000,
          url: `https://jiten.moe/decks/media/${mediaId}/detail`,
          volumes: mediaId === 86082 ? {'1': {difficulty: 1, word_count: 3000, unique_word_count: 900}} : {}};
};
"""
JITEN_VIEW = r"""
const chips = region => region ? [...region.querySelectorAll('.planned-jiten-chip')].map(n => [n.innerText, [...n.classList].find(c => c.startsWith('difficulty-')) || '']) : null;
const level = n => t('uiA.jiten.difficulty', {level: t(`uiA.jiten.level${n}`)});
"""


def test_jiten_difficulty_colours_are_shared_by_planning_light_novels_and_manga(page) -> None:
    planned = [
        {
            "media_id": 77,
            "title": "Plan",
            "status": "PLANNING",
            "episodes": 12,
            "progress": 0,
            "media_status": "FINISHED",
        }
    ]
    result = page.run(
        JITEN_VIEW
        + r"""
        setPage('planned'); await __tick(100);
        // The planning card scrolls into view.
        for (const o of __observers.intersection.filter(o => !o.disconnected && o.targets.some(n => n.matches('[data-planned-jiten]'))))
          o.__fire(o.targets.map(target => ({target})));
        await __tick(300);
        const out = {planned: chips(document.querySelector('#plannedContent [data-media-id="77"] [data-planned-jiten]'))};
        setPage('lightnovels'); await __tick(300);
        out.ln = chips(document.querySelector('#lightNovelsContent [data-ln-book="9"] [data-ln-card-jiten]'));
        out.lnSeries = chips(document.querySelector('#lightNovelsContent [data-ln-series-jiten]'));
        setPage('manga'); await __tick(300);
        out.manga = chips(document.querySelector('#mangaContent [data-manga-book="20"] [data-library-card-jiten]'));
        out.mangaSeries = chips(document.querySelector('#mangaContent [data-manga-series-jiten]'));
        out.levels = [0, 1, 2, 3, 4, 5].map(level);
        return out;
        """,
        before=JITEN,
        api=LIBRARY,
        state={"planned": planned},
    )
    out = result.value
    lv = out["levels"]
    assert out["planned"][0] == [lv[4], "difficulty-4"]
    assert out["ln"][0] == [lv[0], "difficulty-0"]
    assert out["lnSeries"][0] == [lv[2], "difficulty-2"]
    assert out["manga"][0] == [lv[5], "difficulty-5"]
    assert out["mangaSeries"][0] == [lv[3], "difficulty-3"]
    rules = css_rules("index.html")
    palette = {
        0: ("rgba(21,128,61,.8)", "#86efac"),
        1: ("rgba(34,197,94,.8)", "#bbf7d0"),
        2: ("rgba(6,182,212,.8)", "#67e8f9"),
        3: ("rgba(217,119,6,.8)", "#fcd34d"),
        4: ("rgba(220,38,38,.8)", "#fca5a5"),
        5: ("rgba(220,38,38,.8)", "#fca5a5"),
    }
    for level, (border, color) in palette.items():
        selector = f".planned-jiten-chip.difficulty-{level}"
        assert (rules.get(selector, "border-color"), rules.get(selector, "color")) == (border, color), (
            selector
        )


def test_series_jiten_header_hides_length_and_tooltip_but_volumes_keep_them(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            JITEN_VIEW
            + r"""
            setPage('manga'); await __tick(300);
            const series = document.querySelector('#mangaContent [data-manga-series-jiten]');
            const volume = document.querySelector('#mangaContent [data-manga-book="11"] [data-library-card-jiten]');
            const locale = PudgeI18n.t('ui.en');
            return {series: chips(series), seriesTips: series.querySelectorAll('[data-tooltip],[title]').length,
                    seriesLink: series.querySelector('button')?.dataset.url,
                    volume: chips(volume), volumeTip: volume.querySelector('[data-tooltip]')?.dataset.tooltip,
                    levels: [level(1), level(3)], length: tn('uiA.jiten.length', 1000, {value: compactJitenNumber(3000)}),
                    detail: t('uiA.jiten.detail', {words: (3000).toLocaleString(locale), unique: (900).toLocaleString(locale)})};
            """,
            lang=lang,
            before=JITEN,
            api=LIBRARY,
        )
        out = result.value
        easy, hard = out["levels"]
        # The series header: difficulty of the whole series, no length, no hover numbers.
        assert out["series"] == [[hard, "difficulty-3"]] and out["seriesTips"] == 0
        assert out["seriesLink"] == "https://jiten.moe/decks/media/86082/detail"
        # A volume card keeps its length and the word counts on hover.
        assert out["volume"] == [[easy, "difficulty-1"], [out["length"], ""]]
        assert out["volumeTip"] == out["detail"]


def test_series_cards_select_all_volumes_and_keep_scores_and_ocr_per_series(page) -> None:
    result = page.run(
        MENU
        + r"""
        const out = {};
        setPage('lightnovels'); await __tick(100);
        const lnSeries = '#lightNovelsContent [data-ln-series-key="spice"]';
        await __click(`${lnSeries} .ln-series-head strong`); await __tick(50);
        out.lnClick = {panel: __visible('#lnVolumePanel'), volumes: [...document.querySelectorAll('#lnVolumePanel [data-ln-book]')].map(n => Number(n.dataset.lnBook)),
                       selected: [...ui.lnSelection]};
        await __key('Escape'); await __tick(50);
        await __click(`${lnSeries} .ln-series-head strong`, {metaKey: true}); await __tick(10);
        out.lnCmd = {selected: [...ui.lnSelection].sort(), cls: document.querySelector(lnSeries).classList.contains('series-selected')};
        clearLnSelection();
        out.lnSeriesMenu = actions(await menu(`${lnSeries} .ln-series-head`)); await __key('Escape');
        out.lnBookMenu = actions(await menu('#lightNovelsContent [data-ln-book="9"]')); await __key('Escape');
        out.lnStats = __text(`${lnSeries} .ln-series-stats`);

        setPage('manga'); await __tick(100);
        const mangaSeries = '#mangaContent [data-manga-series-key="food"]';
        const selected = () => PudgeMangaReaderV2.selectedBookIds().map(Number).sort();
        await __click(`${mangaSeries} .ln-series-head strong`); await __tick(50);
        out.mangaClick = {panel: __visible('#lnVolumePanel'), selected: selected()};
        await __key('Escape'); await __tick(50);
        await __click(`${mangaSeries} .ln-series-head strong`, {metaKey: true}); await __tick(10);
        out.mangaCmd = {selected: selected(), cls: document.querySelector(mangaSeries).classList.contains('series-selected')};
        PudgeMangaReaderV2.clearSelection();
        await __click(`${mangaSeries} [data-manga-book="12"] h3`, {metaKey: true}); await __tick(10);
        out.mangaVolumeCmd = {selected: selected(), cls: document.querySelector(mangaSeries).classList.contains('series-selected')};
        PudgeMangaReaderV2.clearSelection();
        out.mangaSeriesMenu = actions(await menu(`${mangaSeries} .ln-series-head`));
        await __click('#contextMenu [data-manga-context-action="ocr-series"]'); await __tick(50);
        out.mangaBookMenu = actions(await menu('#mangaContent [data-manga-book="20"]')); await __key('Escape');
        out.mangaStats = __text(`${mangaSeries} .ln-series-stats`);
        out.mangaSolo = __text('#mangaContent [data-manga-book="20"] .ln-card-meta');
        out.chips = [PudgeAniListScore.text(84, 'percent'), PudgeAniListScore.text(61, 'percent')];
        return out;
        """,
        api={
            **LIBRARY,
            "start_manga_ocr_book": {"running": True},
            "manga_ocr_book_status": {"running": True},
        },
    )
    out = result.value
    series_chip, solo_chip = out["chips"]
    # A click on a series opens all its volumes; Cmd+click selects all of them.
    assert out["lnClick"] == {"panel": True, "volumes": [1, 2], "selected": []}
    assert out["lnCmd"] == {"selected": [1, 2], "cls": True}
    assert out["mangaClick"] == {"panel": True, "selected": []}
    assert out["mangaCmd"] == {"selected": [11, 12], "cls": True}
    # Cmd+click on one volume inside a series selects only that volume.
    assert out["mangaVolumeCmd"] == {"selected": [12], "cls": False}
    # Scores belong to the series: the series menus set them, volume menus have no score item.
    for series_menu in (out["lnSeriesMenu"], out["mangaSeriesMenu"]):
        assert {"select-series", "score-series"} <= set(series_menu) and "score" not in series_menu
    for book_menu in (out["lnBookMenu"], out["mangaBookMenu"]):
        assert not {"score", "score-series"} & set(book_menu)
    # OCR of a series runs for every volume.
    assert "ocr-series" in out["mangaSeriesMenu"]
    assert sorted(call[0] for call in result.calls_of("start_manga_ocr_book")) == [11, 12]
    # The series header shows AniList's score once; a single volume shows it on its card.
    assert out["lnStats"].strip() == series_chip and out["mangaStats"].strip() == series_chip
    assert out["mangaSolo"].endswith(solo_chip)
    rules = css_rules("index.html")
    assert rules.declarations(
        ".ln-series-group:hover:not(:has(.ln-entry:hover))"
    )  # hovering a volume does not light the series
    assert rules.get(".ln-series-group.series-selected", "border-color") == "var(--accent)"


def test_anilist_scores_are_ten_point_face_chips_everywhere(page) -> None:
    planned = [
        {
            "media_id": 5,
            "title": "Plan",
            "status": "PLANNING",
            "episodes": 12,
            "progress": 0,
            "mean_score": 84,
            "media_status": "RELEASING",
        }
    ]
    result = page.run(
        r"""
        const chips = root => [...document.querySelectorAll(`${root} .anilist-score-chip`)].map(n => n.innerText);
        setPage('planned'); await __tick(100);
        const out = {planned: chips('#planned [data-media-id="5"]')};
        setPage('lightnovels'); await __tick(100);
        out.ln = chips('#lightNovelsContent');
        setPage('manga'); await __tick(100);
        out.manga = chips('#mangaContent');
        out.pages = [$('planned').innerText, $('lightnovels').innerText, $('manga').innerText].join('\n');
        out.faces = Object.fromEntries([90, 80, 79, 75, 74, 68, 67, 61, 60, 54, 53, 30].map(score => [score, PudgeAniListScore.emoji(score, 'percent')]));
        const hue = score => { const box = document.createElement('div'); box.innerHTML = PudgeAniListScore.chip(score, 'percent');
                               return box.firstChild.style.getPropertyValue('--score-hue'); };
        out.hues = [40, 54, 69, 84, 95].map(hue);
        return out;
        """,
        api=LIBRARY,
        state={"planned": planned},
    )
    out = result.value
    assert out["planned"] == ["8.4 🤩"]
    assert sorted(out["ln"]) == ["7.0 🙂", "8.4 🤩"]  # the series header and the single volume
    assert sorted(out["manga"]) == ["6.1 😐", "8.4 🤩"]
    assert "84%" not in out["pages"] and "/10" not in out["pages"]
    # Faces follow how AniList scores are spread (sextiles of popular titles), not even steps.
    assert out["faces"] == {
        "90": "🤩",
        "80": "🤩",
        "79": "😊",
        "75": "😊",
        "74": "🙂",
        "68": "🙂",
        "67": "😐",
        "61": "😐",
        "60": "🙁",
        "54": "🙁",
        "53": "😞",
        "30": "😞",
    }
    # Colour runs red (5.4 and below) to green (8.4 and above).
    assert out["hues"] == ["0", "0", "60", "120", "120"]
    chip = css_rules("index.html").declarations(".anilist-score-chip")
    assert chip["color"] == "hsl(var(--score-hue),82%,78%)"


def test_series_header_score_sits_on_the_full_width_line_below_the_title() -> None:
    rules = css_rules("index.html")
    # The volume count is pinned to the top right; the title leaves room for it.
    count = rules.declarations(".ln-series-head>.ln-series-count")
    assert (count["position"], count["right"], count["top"], count["white-space"]) == (
        "absolute",
        "0",
        "0",
        "nowrap",
    )
    assert rules.get(".ln-series-title-block>strong", "padding-right") == "120px"
    # Stats use the whole card width and wrap only between chips, never inside the Jiten chips.
    stats = rules.declarations(".ln-series-stats")
    assert (stats["display"], stats["width"], stats["max-width"], stats["flex-wrap"]) == (
        "flex",
        "100%",
        "none",
        "wrap",
    )
    jiten = rules.declarations(".ln-series-stats>.planned-jiten")
    assert (jiten["flex"], jiten["width"]) == ("0 0 auto", "max-content")
    assert rules.get(".ln-series-stats>.planned-jiten button", "flex-wrap") == "nowrap"


def test_manga_progress_hover_shows_read_pages_like_light_novels(page) -> None:
    assert en("manga.readOfPages", count=10, read=3) == "3 / 10 pages"
    assert ru("manga.readOfPages", count=10, read=3) == "3 / 10 страниц"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('manga'); await __tick(100);
            await __fire('#mangaContent [data-manga-book="20"] .ln-card-progress', 'mouseover', {}, 'MouseEvent');
            const tip = document.getElementById('titleTooltip');
            return {shown: tip.classList.contains('show') ? tip.innerText : null, native: document.querySelector('#mangaContent [data-manga-book="20"] .ln-card-progress').title,
                    expected: PudgeI18n.tn('manga.readOfPages', 10, {read: 3})};
            """,
            lang=lang,
            api=LIBRARY,
        )
        out = result.value
        assert out["shown"] == out["expected"] and out["native"] == ""


def test_light_novel_volume_cards_show_no_chapter_count(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels'); await __tick(100);
            return {card: __text('#lightNovelsContent [data-ln-book="9"]'), chip: PudgeAniListScore.text(70, 'percent')};
            """,
            lang=lang,
            api=LIBRARY,
        )
        card = result.value["card"]
        assert "12" not in card  # 12 chapters are not listed
        assert result.value["chip"] in card  # other facts stay


# --- Manga reader pages -----------------------------------------------------------------------

# A volume of window.NAMES pages; names like 018-019 are physical two-page spreads.
READER = r"""
window.NAMES = ['016.jpg', '017.jpg', '018-019.jpg', '020.jpg', '021.jpg', '022.jpg'];
window.BOOK = {id: 3, title: 'Vol 1', series_title: 'Manga', position: 0, read_pages: 0};
__api.manga_state = async () => ({books: [{...BOOK, page_count: NAMES.length}], ocr_available: false});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: NAMES.length, name: NAMES[index],
                                         spread: NAMES[index].includes('-'), data_uri: 'data:image/png;base64,AAAA', width: 800, height: 1200});
__api.manga_mark_read = async (id, index) => ({id, read_pages: index + 1});
__api.manga_set_position = async () => ({ok: true});
"""
PAGES = r"""
const shown = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-page-frame')].map(f => Number(f.dataset.pageIndex));
const marked = () => __apiCalls.filter(c => c.name === 'manga_mark_read').map(c => c.args[1]);
const turn = async key => { await __key(key, {}, 'body'); await __tick(200); return shown(); };
"""


def _reader_mode(mode: str) -> dict[str, str]:
    return {"pudge.manga.reader.v2": json.dumps({"mode": mode, "direction": "rtl"})}


def test_double_page_mode_shows_physical_spreads_alone(page) -> None:
    result = page.run(
        PAGES
        + r"""
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(200);
        const forward = [shown()];
        forward.push(await turn('ArrowLeft'));
        const spread = document.querySelector('#mangaV2Pages .manga-v2-page-frame').classList.contains('spread');
        for (let k = 0; k < 2; k++) forward.push(await turn('ArrowLeft'));
        const back = [await turn('ArrowRight'), await turn('ArrowRight'), await turn('ArrowRight')];
        return {forward, back, spread};
        """,
        before=READER,
        localStorage=_reader_mode("double"),
    )
    out = result.value
    # 016+017 side by side, the 018-019 spread alone, then 020+021, then 022.
    assert out["forward"] == [[0, 1], [2], [3, 4], [5]]
    # Back: 017 is not paired with the spread that follows it.
    assert out["back"] == [[3, 4], [2], [1]] and out["spread"] is True


def test_manga_progress_counts_only_pages_turned_past_or_the_last_page(page) -> None:
    ten = r"""window.NAMES = Array.from({length: 10}, (_, i) => `${String(i + 1).padStart(3, '0')}.jpg`);"""
    single = page.run(
        PAGES
        + r"""
        BOOK.position = 4;
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300);
        const out = {opened: marked()};                  // loading and preloading pages is not reading them
        await turn('ArrowLeft'); out.forward = marked();
        await turn('ArrowRight'); await turn('ArrowRight'); out.back = marked();
        for (let k = 0; k < 6; k++) await turn('ArrowLeft');
        out.end = {marked: marked(), shown: shown()};    // the last page counts without another turn
        return out;
        """,
        before=READER + ten,
        localStorage=_reader_mode("single"),
    )
    # Each forward turn counts the page left behind; going back counts nothing.
    assert single.value == {
        "opened": [],
        "forward": [4],
        "back": [4],
        "end": {"marked": [4, 5, 6, 7, 8, 9], "shown": [9]},
    }

    double = page.run(
        PAGES
        + r"""
        BOOK.position = 4;
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300);
        const out = {opened: marked(), shown: shown()};
        await turn('ArrowLeft'); out.forward = marked();
        return out;
        """,
        before=READER + ten,
        localStorage=_reader_mode("double"),
    )
    assert double.value == {"opened": [], "shown": [4, 5], "forward": [5]}

    vertical = page.run(
        PAGES
        + r"""
        BOOK.position = 4;
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300);
        const viewport = $('mangaV2Viewport');
        viewport.scrollHeight = 20000; viewport.clientHeight = 500; viewport.scrollTop = 8000;
        const out = {opened: marked()};                  // placeholders of the whole strip are not reading
        const observer = __observers.intersection.find(o => [].concat(o.options?.threshold ?? []).length > 1 && !o.disconnected);
        const see = async index => { observer.__fire([{target: document.querySelector(`#mangaV2Pages [data-page-index="${index}"]`)}]); await __tick(300); };
        await see(5); out.down = marked();               // pages above the visible one are read
        await see(3); out.up = marked();                 // scrolling back changes nothing
        viewport.scrollTop = 19500;
        await see(9); out.end = marked();                // reaching the end of the strip completes the volume
        return out;
        """,
        before=READER + ten,
        localStorage=_reader_mode("vertical"),
    )
    assert vertical.value == {"opened": [], "down": [4], "up": [4], "end": [4, 9]}


# --- Library redraws ------------------------------------------------------------------------

# Layout of the Manga page (`before`): the page scroller #scroll shows 800 px; every tile of
# the library grid is 300 px high and moves up as the page scrolls.
PAGE_LAYOUT = r"""
Object.defineProperty(Element.prototype, '__rect', {
  configurable: true,
  get() {
    if (this.id === 'scroll') return {x: 0, y: 0, width: 1000, height: 800};
    const tile = this.closest && this.closest('#mangaContent .ln-grid > *');
    if (!tile) return undefined;
    const index = [...tile.parentNode.children].indexOf(tile);
    return {x: 0, y: index * 300 - document.getElementById('scroll').scrollTop, width: 300, height: 280};
  },
  set(value) { Object.defineProperty(this, '__rect', {value, writable: true, configurable: true}); },
});
window.BOOKS = [
  {id: 21, title: 'Beta v1', series_title: 'Beta', series_key: 'beta', volume: 1, page_count: 10},
  {id: 22, title: 'Gamma v1', series_title: 'Gamma', series_key: 'gamma', volume: 1, page_count: 10},
  {id: 23, title: 'Delta v1', series_title: 'Delta', series_key: 'delta', volume: 1, page_count: 10},
];
__api.manga_state = async () => ({books: JSON.parse(JSON.stringify(BOOKS)), ocr_available: false});
"""


def test_manga_library_redraws_only_on_changes_and_keeps_the_card_in_view(page) -> None:
    result = page.run(
        r"""
        setPage('manga'); await __tick(200);
        const tiles = () => [...document.querySelectorAll('#mangaContent .ln-grid > *')];
        const order = tiles().map(n => n.dataset.mangaBook);
        const before = tiles(), covers = [...document.querySelectorAll('#mangaContent img')];
        await __settle(PudgeMangaReaderV2.renderLibrary(true)); await __tick(100);
        const same = tiles().every((n, i) => n === before[i]) && [...document.querySelectorAll('#mangaContent img')].every((n, i) => n === covers[i]);
        // The user looks at the third tile; a new volume that sorts first arrives.
        const scroll = document.getElementById('scroll');
        scroll.scrollTop = 600;
        const third = order[2];
        BOOKS.push({id: 20, title: 'Alpha v1', series_title: 'Alpha', series_key: 'alpha', volume: 1, page_count: 10});
        await __settle(PudgeMangaReaderV2.renderLibrary(true)); await __tick(100);
        const card = document.querySelector(`#mangaContent [data-manga-book="${third}"]`);
        return {same, rebuilt: !before[0].isConnected, order: tiles().map(n => n.dataset.mangaBook),
                top: card.getBoundingClientRect().top, scrollTop: scroll.scrollTop};
        """,
        before=PAGE_LAYOUT,
    )
    out = result.value
    # Same data: nothing is redrawn (no flicker, covers stay decoded).
    assert out["same"] is True
    # New data: the library is redrawn, and the card that was at the top of the view stays there.
    assert out["rebuilt"] is True and out["order"][0] == "20"
    assert out["top"] == 0 and out["scrollTop"] == 900


# Series scrollers (`before`): the box at y=100 shows 195 px; volume cards are 120 px high,
# 7 px apart, and move up as the box scrolls.
SERIES_LAYOUT = r"""
Object.defineProperty(Element.prototype, '__rect', {
  configurable: true,
  get() {
    if (this.hasAttribute('data-series-scroll')) return {x: 0, y: 100, width: 300, height: 195};
    const box = this.classList.contains('ln-entry') ? this.closest('[data-series-scroll]') : null;
    if (!box) return undefined;
    const index = [...box.querySelectorAll('.ln-entry')].indexOf(this);
    return {x: 0, y: 100 + index * 127 - box.scrollTop, width: 300, height: 120};
  },
  set(value) { Object.defineProperty(this, '__rect', {value, writable: true, configurable: true}); },
});
"""
DUNGEON = [
    {
        "id": 11 + i,
        "title": f"Dungeon Food v{i + 1}",
        "series_title": "Dungeon Food",
        "series_key": "dungeon",
        "volume": i + 1,
        "page_count": 100,
        "read_pages": read,
        "anilist_id": 86082,
    }
    for i, read in enumerate([100, 40, 0])
]


def test_late_jiten_stats_do_not_move_a_series_row_the_user_scrolled(page) -> None:
    result = page.run(
        r"""
        JITEN_DELAY = 1500;                                    // Jiten answers late
        setPage('manga'); await __tick(200);
        const box = document.querySelector('[data-manga-series-key="dungeon"] .ln-series-books');
        const opened = box.scrollTop;                          // the series opens on volume 2
        box.scrollTop = 0;                                     // the user scrolls back to volume 1
        await __tick(2500);
        return {opened, after: box.scrollTop, hydrated: document.querySelectorAll('[data-manga-series-key="dungeon"] .planned-jiten-chip').length};
        """,
        before=SERIES_LAYOUT + JITEN,
        api={"manga_state": {"books": DUNGEON, "ocr_available": False}},
    )
    assert result.value["opened"] == 127
    assert result.value["hydrated"] > 0
    assert result.value["after"] == 0
