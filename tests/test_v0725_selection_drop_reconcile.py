from __future__ import annotations

import io
import zipfile
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from test_web_app import make_api
from tests.css_support import css_rules
from tests.i18n_support import en, ru


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_diagnose_materializes_seihantai_episode_7_from_persisted_complete_download(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            status="CURRENT",
            progress=6,
            episodes=13,
            media_status="RELEASING",
        )
    )
    old = tmp_path / "episode6.mkv"
    old.write_bytes(b"old")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=6,
            media_episode=6,
            release_episode=18,
            video_path=old,
            state="ready",
        )
    )
    folder = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    item = DownloadItem(
        torrent_hash="f3a099e5c9a9519b1b406a23202f94e1e9d07157",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=210031,
        episode=7,
        media_episode=7,
        release_episode=19,
        completed_on=123,
        raw={"backend": "aria2"},
    )
    manager.db.upsert_download(item)
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    diagnosis = manager.diagnose_episode(210031, 7)
    row = manager.db.episode_by_path(video.resolve())

    assert row is not None
    assert row.episode == 7 and row.media_episode == 7 and row.release_episode == 19
    assert diagnosis["checks"][2]["detail"] == str(video.resolve())
    assert diagnosis["checks"][2]["ok"] is True
    assert diagnosis["checks"][4]["detail"] != "Missing job"


def test_home_does_not_use_completed_episode_6_download_as_episode_7_state(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    anime = LibraryAnime(
        media_id=200637,
        title="100 Girlfriends S3",
        status="CURRENT",
        progress=6,
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=8,
        next_airing_at=2_000_000_000,
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "S03E06.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title=anime.title,
            episode=6,
            media_episode=6,
            release_episode=6,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="9" * 40,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(tmp_path),
            content_path=str(video),
            media_id=200637,
            episode=6,
            media_episode=6,
            release_episode=6,
            raw={},
        )
    )

    sections = api._home_sections([anime], {anime.media_id: anime})
    card = next(row for row in sections["waiting"] if row["media_id"] == 200637)

    assert card["next_episode"] == 7
    assert "download" not in card
    assert card["presentation"]["status"] != "waiting_preparation"
    api.close()


def _write_cbz(path: Path) -> None:
    image = Image.new("RGB", (2, 2), "white")
    data = io.BytesIO()
    image.save(data, format="PNG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", data.getvalue())


def test_manga_remove_books_is_exact_and_keeps_user_archives(tmp_path: Path) -> None:
    service = MangaService(_manager(tmp_path).db, cache_dir=tmp_path / "cache")
    first = tmp_path / "Series Vol 1.cbz"
    second = tmp_path / "Series Vol 2.cbz"
    _write_cbz(first)
    _write_cbz(second)
    a = service.import_file(first)
    b = service.import_file(second)

    assert service.remove_books([a["id"]]) == 1
    remaining = {int(row["id"]) for row in service.state()["books"]}
    assert int(a["id"]) not in remaining and int(b["id"]) in remaining
    assert first.is_file() and second.is_file()


SELECTION = """
const bar = () => ({visible: __visible('#pageSelectionActions'), text: __text('#pageSelectionActions'), importVisible: __visible('#pageImportButton'),
                    actions: [...document.querySelectorAll('#pageSelectionActions [data-selection-action]')].map(b => b.dataset.selectionAction)});
const selected = root => [...document.querySelectorAll(`${root} .ln-entry.selected`)].map(n => Number(n.dataset.lnBook || n.dataset.mangaBook));
const cards = root => [...document.querySelectorAll(`${root} .ln-entry`)].map(n => Number(n.dataset.lnBook || n.dataset.mangaBook)).sort();
const confirmDelete = async () => {
  await __click('#pageSelectionActions [data-selection-action="delete"]'); await __flush();
  const question = __text('#pudgeConfirmMessage');
  await __click('[data-pudge-confirm="ok"]'); await __flush();
  return question;
};
const texts = {cancel: PudgeI18n.t('ui.cancelSelection'), remove: PudgeI18n.t('ui.delete')};
"""


def test_ln_cmd_click_and_context_menu_select_books_for_bulk_delete(page) -> None:
    assert en("ui.cancelSelection") == "Cancel selection" and ru("ui.cancelSelection") == "Отменить выделение"
    books = [
        {"id": 4, "title": "One", "volume": 1},
        {"id": 5, "title": "Two", "volume": 1},
        {"id": 6, "title": "Three", "volume": 1},
    ]
    for lang in ("en", "ru"):
        result = page.run(
            SELECTION
            + """
            setPage('lightnovels', true); await __tick(50);
            const root = '#lightNovelsContent';
            const out = {start: bar()};
            await __click('#lightNovelsContent [data-ln-book="4"]', {metaKey: true});
            out.cmd = {bar: bar(), selected: selected(root), reader: $('lnReaderShell').classList.contains('open')};
            await __fire('#lightNovelsContent [data-ln-book="5"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
            await __click('#contextMenu [data-ln-context-action="select"]');
            out.two = {bar: bar(), selected: selected(root)};
            await __click('#pageSelectionActions [data-selection-action="clear"]');
            out.cleared = {bar: bar(), selected: selected(root)};
            await __click('#lightNovelsContent [data-ln-book="4"]', {metaKey: true});
            await __click('#lightNovelsContent [data-ln-book="6"]', {metaKey: true});
            out.question = await confirmDelete();
            out.deleted = {cards: cards(root), bar: bar()};    // gone before the backend answers
            out.expectedQuestion = t('ui.removeSelectedFromPudgeSource', {count: 2});
            out.texts = texts;
            return out;
            """,
            lang=lang,
            api={"light_novel_state": {"version": 1, "settings": {}, "books": books}},
            before="__api.light_novel_delete = () => new Promise(() => {});",
        )
        out = result.value
        cancel, remove = out["texts"]["cancel"], out["texts"]["remove"]
        assert out["start"] == {"visible": False, "text": "", "importVisible": True, "actions": []}
        assert out["cmd"]["selected"] == [4] and out["cmd"]["reader"] is False
        assert out["cmd"]["bar"] == {
            "visible": True,
            "text": f"1\n{cancel}\n{remove}",
            "importVisible": False,
            "actions": ["clear", "delete"],
        }
        assert out["two"]["selected"] == [4, 5] and out["two"]["bar"]["text"].startswith("2\n")
        assert out["cleared"] == {"bar": out["start"], "selected": []}
        assert out["question"] == out["expectedQuestion"]
        assert out["deleted"] == {"cards": [5], "bar": out["start"]}
        assert sorted(result.calls_of("light_novel_delete")) == [[4], [6]]


def test_manga_cmd_click_and_context_menu_select_volumes_for_ocr_and_bulk_delete(page) -> None:
    books = [
        {"id": 3, "title": "Alpha", "page_count": 180, "position": 0},
        {"id": 8, "title": "Beta", "page_count": 200, "position": 0},
        {"id": 9, "title": "Gamma", "page_count": 10, "position": 0},
    ]
    library = """
    window.__books = BOOKS;
    __api.manga_state = async () => ({books: window.__books.map(book => ({...book})), ocr_available: true});
    __api.manga_remove_books = ids => new Promise(resolve => { window.__finishRemove = () => {
      window.__books = window.__books.filter(book => !ids.includes(book.id)); resolve({removed: ids.length}); }; });
    __api.start_manga_ocr_book = async () => ({ok: true});
    __api.manga_ocr_book_status = async () => ({running: false});
    """.replace("BOOKS", json.dumps(books))
    result = page.run(
        SELECTION
        + """
        setPage('manga', true); await __tick(50);
        const root = '#mangaContent';
        const out = {};
        await __click('#mangaContent [data-manga-book="3"]', {metaKey: true});
        out.cmd = {bar: bar(), selected: selected(root), reader: Boolean($('mangaReaderV2')?.classList.contains('open'))};
        await __fire('#mangaContent [data-manga-book="8"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
        await __click('#contextMenu [data-manga-context-action="select"]');
        out.two = selected(root);
        await __click('#pageSelectionActions [data-selection-action="ocr"]'); await __tick(10);
        out.question = await confirmDelete();
        out.deleted = {cards: cards(root), bar: bar()};        // gone before the backend answers
        window.__finishRemove(); await __tick(10);
        out.after = cards(root);
        out.texts = texts;
        return out;
        """,
        before=library,
    )
    out = result.value
    cancel, remove = out["texts"]["cancel"], out["texts"]["remove"]
    assert out["cmd"]["selected"] == [3] and out["cmd"]["reader"] is False
    assert out["cmd"]["bar"] == {
        "visible": True,
        "text": f"1\n{cancel}\nOCR\n{remove}",
        "importVisible": False,
        "actions": ["clear", "ocr", "delete"],
    }
    assert out["two"] == [3, 8]
    assert result.calls_of("start_manga_ocr_book") == [[3, False], [8, False]]
    assert out["deleted"]["cards"] == [9] and out["deleted"]["bar"]["visible"] is False
    assert result.calls_of("manga_remove_books") == [[[3, 8]]]
    assert out["after"] == [9]
    # Selected manga volumes use the light novel card, highlighted the same way.
    assert css_rules("index.html").get(".ln-entry.selected", "box-shadow")


def test_dropped_files_are_routed_to_their_library(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    novel = drop / "時をかける少女.txt"
    novel.write_text("時をかける少女。これは日本語の本文です。" * 40, encoding="utf-8")
    archive = drop / "Series Vol 1.cbz"
    _write_cbz(archive)
    pages = drop / "Pages"
    pages.mkdir()
    for index in (1, 2):
        Image.new("RGB", (2, 2), "white").save(pages / f"{index:03d}.png")
    loose = tmp_path / "Scans"
    loose.mkdir()
    Image.new("RGB", (2, 2), "white").save(loose / "001.png")
    audio, video, other = drop / "book.m4b", drop / "Show - 03.mkv", drop / "notes.doc"
    for path in (audio, video, other):
        path.write_bytes(b"x")
    # A novel removed from Pudge earlier: dropping it again brings it back.
    first = api.light_novels.import_file(novel)
    api.light_novels.delete_book(int(first["id"]), delete_file=False)
    later: list[tuple[float, object]] = []
    monkeypatch.setattr(
        "pudge.web_app.threading.Timer",
        lambda delay, fn: later.append((delay, fn)) or SimpleNamespace(daemon=True, start=lambda: None),
    )
    linked: list[int] = []
    monkeypatch.setattr(api.audiobooks, "auto_link_light_novel", lambda book_id: linked.append(int(book_id)))
    monkeypatch.setattr(api.audiobooks, "import_file", lambda path: {"id": 41, "title": path.stem})
    monkeypatch.setattr(
        api.manager,
        "import_local_video",
        lambda path: SimpleNamespace(media_id=7, episode=3, title="Show", video_path=path),
    )

    result = api.import_dropped_paths(
        [
            str(p)
            for p in (novel, archive, pages, loose / "001.png", audio, video, other, drop / "missing.txt")
        ]
    )

    kinds = [(row["kind"], row["page"], row.get("title")) for row in result["imports"]]
    assert kinds == [
        ("light_novel", "lightnovels", "時をかける少女"),
        ("manga", "manga", "Series Vol 1"),
        ("audiobook", "audiobooks", "book"),
        ("anime", "current", "Show"),
        ("manga", "manga", "Pages"),
        ("manga", "manga", "Scans"),  # image folders and loose images
    ]
    assert result["focus"] == result["imports"][0] and result["ok"] is True
    assert result["focus"]["book"]["id"] == result["focus"]["id"]  # the card travels with the result
    assert [book["title"] for book in api.light_novels.books()] == ["時をかける少女"]
    assert sorted(book["title"] for book in api.manga.state()["books"]) == ["Pages", "Scans", "Series Vol 1"]
    assert result["errors"] == ["notes.doc: unsupported file type"]
    # Linking a matching audiobook waits until after the drop has been answered.
    assert linked == [] and len(later) == 1 and later[0][0] > 0
    later[0][1]()
    assert linked == [result["focus"]["id"]]


DROPPED = """
const imported = detail => window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail}));
const focused = selector => { const node = document.querySelector(selector);
  return node && {highlighted: node.classList.contains('drop-focus'), scrolled: (node.__scrolls || []).length > 0}; };
const loads = name => __apiCalls.filter(call => call.name === name).length;
"""


def test_dropped_file_opens_its_page_and_highlights_the_new_card(page) -> None:
    result = page.run(
        DROPPED
        + """
        const out = {};
        const drop = new Event('drop', {bubbles: true, cancelable: true}); drop.dataTransfer = {files: [{name: 'a.txt'}], types: ['Files']};
        document.body.dispatchEvent(drop); await __flush();
        out.drop = {prevented: drop.defaultPrevented, toasts: __toasts(), importing: t('ui.importing')};

        const novels = loads('light_novel_state');
        const novel = {id: 11, title: 'Dropped novel', volume: 1};
        imported({ok: true, imports: [], focus: {kind: 'light_novel', page: 'lightnovels', id: 11, book: novel}, errors: []});
        await __tick(50);
        out.ln = {page: ui.page, card: focused('[data-ln-book="11"]'), reloads: loads('light_novel_state') - novels};

        const manga = loads('manga_state');
        imported({ok: true, imports: [], focus: {kind: 'manga', page: 'manga', id: 3, book: {id: 3, title: 'Dropped manga', page_count: 10, position: 0}}, errors: []});
        await __tick(50);
        out.manga = {page: ui.page, card: focused('[data-manga-book="3"]'), reloads: loads('manga_state') - manga};
        await __tick(2000);
        out.later = focused('[data-manga-book="3"]');            // the highlight fades

        imported({ok: false, imports: [], focus: null, errors: ['notes.doc: unsupported file type']});
        await __tick(10);
        out.error = __toasts().at(-1);
        return out;
        """,
        # The backend already has the dropped books when the window asks later.
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [{"id": 11, "title": "Dropped novel", "volume": 1}],
            },
            "manga_state": {
                "books": [{"id": 3, "title": "Dropped manga", "page_count": 10, "position": 0}],
                "ocr_available": False,
            },
        },
    )
    out = result.value
    assert out["drop"] == {
        "prevented": True,
        "toasts": [out["drop"]["importing"]],
        "importing": out["drop"]["importing"],
    }
    # The dropped card comes with the event: it is shown at once, without reading the whole library.
    assert out["ln"] == {"page": "lightnovels", "card": {"highlighted": True, "scrolled": True}, "reloads": 0}
    assert out["manga"] == {"page": "manga", "card": {"highlighted": True, "scrolled": True}, "reloads": 0}
    assert out["later"] == {"highlighted": False, "scrolled": True}
    assert out["error"] == "notes.doc: unsupported file type"


def test_dropped_audiobook_card_stays_highlighted(page) -> None:
    result = page.run(
        DROPPED
        + """
        imported({ok: true, imports: [], focus: {kind: 'audiobook', page: 'audiobooks', id: 5}, errors: []});
        await __tick(300);
        return {page: ui.page, card: focused('[data-audiobook-id="5"]')};
        """,
        api={"audiobook_state": {"books": [{"id": 5, "title": "Book", "duration": 60, "position": 0}]}},
    )
    assert result.value == {"page": "audiobooks", "card": {"highlighted": True, "scrolled": True}}
