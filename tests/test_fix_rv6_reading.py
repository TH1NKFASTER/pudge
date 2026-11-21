"""Review round 6, reading / audiobooks / manga / study: one test per fix (invented data)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.database_storage import pack_json
from pudge.light_novels import LightNovelService


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _store_parse(service: LightNovelService, digest: str) -> None:
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
            (digest, pack_json({"tokens": [], "vocabulary": [], "paragraphs": []}), "jiten-v1", time.time()),
        )


# --- "Refresh Jiten data": chapters still to parse are found by key, nothing is unpacked ----------


def test_unparsed_chapters_are_found_without_unpacking_stored_parses(tmp_path: Path, monkeypatch) -> None:
    service = LightNovelService(_config(tmp_path))
    for name, text in (
        ("Cats.txt", "猫が好きです。毎日見ています。"),
        ("Dogs.txt", "犬も好きです。散歩に行きます。"),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
        service.import_file(tmp_path / name)
    with service._connection() as conn:
        hashes = [
            str(row[0])
            for row in conn.execute("SELECT text_hash FROM ln_chapters ORDER BY book_id,chapter_index")
        ]
    assert len(hashes) == 2
    _store_parse(service, hashes[0])
    monkeypatch.setattr(
        service, "_cached_parse", lambda *_a, **_k: pytest.fail("a stored parse must not be unpacked")
    )

    pending = service.jiten_unparsed_chapters()

    assert [digest for _text, digest in pending] == hashes[1:]
    assert service.stored_jiten_parses([*hashes, "", hashes[0]]) == {hashes[0]}


# --- "Refresh Jiten data": manga bubbles, not a page's regions JSON, go to Jiten -------------------


def test_jiten_refresh_parses_manga_bubbles_not_the_pages_json(tmp_path: Path, monkeypatch) -> None:
    from pudge.manga import _REGION_CACHE_KEY
    from pudge.web_app import WebAppApi

    config = _config(tmp_path)
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    api.light_novels.save_settings({"jiten_api_key": "key"})
    now = time.time()
    regions = [
        {"text": "猫が来た", "x": 0.1, "y": 0.2, "width": 0.1, "height": 0.3},
        # Served (and looked up) with its dots as an ellipsis.
        {"text": "犬も来た．．．", "x": 0.5, "y": 0.2, "width": 0.1, "height": 0.3},
        {"text": "鳥は来ない", "x": 0.7, "y": 0.2, "width": 0.1, "height": 0.3},
    ]
    with api.manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path / "manga.cbz"), "Manga", 1, 0, "rtl", now, now),
        )
        manga_id = int(conn.execute("SELECT id FROM manga_books WHERE title='Manga'").fetchone()[0])
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (manga_id, 0, _REGION_CACHE_KEY, json.dumps(regions, ensure_ascii=False), now),
        )
    # A bubble parsed before (the reader looked it up) is not asked for again.
    _store_parse(api.light_novels, api.light_novels._study_text_key("鳥は来ない")[1])
    sent: list[str] = []
    monkeypatch.setattr(api.light_novels, "jiten_preparse", lambda text, digest=None: {"tokens": []})
    monkeypatch.setattr(api.light_novels, "parse_study_texts", lambda texts: sent.extend(texts) or len(texts))

    result = api.refresh_jiten_data()
    api._jiten_refresh_thread.join(timeout=10)

    assert result["manga_regions"] == 2
    assert sorted(sent) == sorted(["猫が来た", "犬も来た…"])
    assert not [text for text in sent if "{" in text or "width" in text]


# --- Audiobook player: a failed playlist read is no place in the first file --------------------


def _two_file_service(answers: dict[str, object]) -> AudiobookService:
    service = AudiobookService.__new__(AudiobookService)
    service._file_rows = lambda _book_id: [
        {"file_index": 0, "start": 0.0, "end": 600.0, "duration": 600.0},
        {"file_index": 1, "start": 600.0, "end": 1200.0, "duration": 600.0},
    ]
    service._ipc_get = lambda _path, name: answers.get(name)
    return service


def test_failed_playlist_position_read_gives_no_sample() -> None:
    # mpv answered the clock but not which file plays (an IPC read past its deadline).
    service = _two_file_service({"time-pos": 42.0, "playlist-pos": None})
    assert AudiobookService._global_position(service, 7, Path("/tmp/pudge-x.sock")) is None
    # Known file: the book clock as before.
    service = _two_file_service({"time-pos": 42.0, "playlist-pos": 1})
    assert AudiobookService._global_position(service, 7, Path("/tmp/pudge-x.sock")) == pytest.approx(642.0)


# --- Audiobook player: the mpv socket path fits a Unix socket address --------------------------


class _Process:
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def test_audiobook_ipc_socket_path_is_short(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.Popen",
        lambda command, **_kwargs: commands.append(list(command)) or _Process(),
    )
    monkeypatch.setattr(
        "pudge.audiobooks.threading.Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None, is_alive=lambda: False),
    )
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"audio")
    book = service._upsert(
        path=audio,
        title="Book",
        duration=60.0,
        files=[
            {"index": 0, "path": str(audio), "title": "Book", "duration": 60.0, "start": 0.0, "end": 60.0}
        ],
        chapters=[{"index": 0, "title": "One", "start": 0.0, "end": 60.0}],
    )
    for _ in range(2):
        service.play(int(book["id"]))
    sockets = [
        Path(arg.split("=", 1)[1])
        for command in commands
        for arg in command
        if arg.startswith("--input-ipc-server=")
    ]
    assert len(sockets) == 2 and sockets[0] != sockets[1]
    for path in sockets:
        # A socket address holds 103 bytes on macOS; "/Users/<22 characters>/Library/Caches/
        # pudge/audiobook-ipc/" takes 65 of them (the old names needed 41-47 more).
        assert len(path.name.encode("utf-8")) <= 103 - 65
        # Still recognized (and its player stopped) when its app is gone.
        assert re.fullmatch(r"book-\d+-(\d+)(?:-[0-9a-f]+-[0-9a-f]+)?\.sock", path.name)


# --- Link dialog: a transcript's opening is read once per transcript file -----------------------


def test_link_candidates_read_each_transcript_once(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    novels = LightNovelService(config)
    novel = tmp_path / "Book.txt"
    novel.write_text("第一章\n" + "吾輩は猫である。名前はまだ無い。" * 20, encoding="utf-8")
    ln_book = novels.import_file(novel)
    service = AudiobookService(
        Database(config.library.database_path), ffprobe="ffprobe", mpv="mpv", cache_dir=config.paths.cache_dir
    )
    audio = tmp_path / "Book.m4b"
    audio.write_bytes(b"audio" * 1000)
    book = service._upsert(
        path=audio,
        title="Book",
        duration=60.0,
        files=[
            {"index": 0, "path": str(audio), "title": "Book", "duration": 60.0, "start": 0.0, "end": 60.0}
        ],
        chapters=[{"index": 0, "title": "One", "start": 0.0, "end": 60.0}],
    )
    transcript = service._transcript_path(int(book["id"]))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    segments = [
        {"start": float(i), "end": float(i) + 1, "text": "吾輩は猫である。名前はまだ無い。"}
        for i in range(40)
    ]
    transcript.write_text(json.dumps({"schema": "audiobook-stt-v3", "segments": segments}), encoding="utf-8")
    loads: list[int] = []
    original = service._load_transcript
    monkeypatch.setattr(
        service, "_load_transcript", lambda book_id: loads.append(book_id) or original(book_id)
    )

    for _ in range(3):
        rows = service.link_candidates_for_light_novel(int(ln_book["id"]))
        assert "audio/text" in rows[0]["signals"]
    assert loads == [int(book["id"])]
    # A transcript made again is read again.
    time.sleep(0.01)
    transcript.write_text(
        json.dumps({"schema": "audiobook-stt-v3", "segments": segments[:20]}), encoding="utf-8"
    )
    service.link_candidates_for_light_novel(int(ln_book["id"]))
    assert len(loads) == 2


# --- Audiobooks: a launch does not read every audio file of the library again ------------------


def test_audio_content_identities_are_kept_across_launches(tmp_path: Path, monkeypatch) -> None:
    import pudge.audiobooks as audiobooks

    database = Database(tmp_path / "db.sqlite3")
    cache = tmp_path / "cache"
    first = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=cache)
    folder = tmp_path / "Book"
    folder.mkdir()
    files = []
    for index in range(3):
        audio = folder / f"{index:02d}.mp3"
        audio.write_bytes(bytes([index + 1]) * 700_000)
        files.append(
            {
                "index": index,
                "path": str(audio),
                "title": audio.stem,
                "duration": 60.0,
                "start": 60.0 * index,
                "end": 60.0 * (index + 1),
            }
        )
    book = first._upsert(
        path=folder,
        title="Book",
        duration=180.0,
        files=files,
        chapters=[{"index": 0, "title": "One", "start": 0.0, "end": 180.0}],
    )
    first.state(queue_missing=False)
    expected = first._transcript_path(int(book["id"]))
    assert (cache / AudiobookService.CONTENT_IDENTITY_FILE).is_file()

    # The next launch names the same files without reading them.
    sampled: list[Path] = []
    original = audiobooks._sampled_content_digest
    monkeypatch.setattr(
        audiobooks, "_sampled_content_digest", lambda path, size: sampled.append(path) or original(path, size)
    )
    second = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=cache)
    assert second._transcript_path(int(book["id"])) == expected
    assert sampled == []
    # A file written again is read again (its size and date changed).
    (folder / "01.mp3").write_bytes(b"\x09" * 700_001)
    third = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=cache)
    assert third._transcript_path(int(book["id"])) != expected
    assert sampled == [folder / "01.mp3"]
    # A damaged store is only a miss.
    (cache / AudiobookService.CONTENT_IDENTITY_FILE).write_text("{not json", encoding="utf-8")
    fourth = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=cache)
    assert fourth._transcript_path(int(book["id"])) == third._transcript_path(int(book["id"]))


# --- Global search: manga volumes are matched from their cards, not the Manga tab's state ------


def test_global_search_reads_manga_cards_without_ocr_status(tmp_path: Path, monkeypatch) -> None:
    import io
    import zipfile

    from PIL import Image

    from pudge.manga import MangaService
    from pudge.web_app import WebAppApi

    page = io.BytesIO()
    Image.new("RGB", (60, 90), "white").save(page, "JPEG")
    manga = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    for volume in (1, 2):
        archive = tmp_path / f"Kitsune Days v{volume:02d}.cbz"
        with zipfile.ZipFile(archive, "w") as handle:
            for index in range(3):
                handle.writestr(f"{index:03d}.jpg", page.getvalue())
        manga.import_file(archive)
    monkeypatch.setattr(manga, "ocr_cache_status", lambda *_a: pytest.fail("no OCR status for a search"))
    monkeypatch.setattr(manga, "state", lambda: pytest.fail("the Manga tab's state is not read"))

    cards = manga.search_catalog()
    assert sorted(card["volume"] for card in cards) == [1, 2]
    assert {card["series_title"] for card in cards} == {"Kitsune Days"}

    api = WebAppApi.__new__(WebAppApi)
    database = SimpleNamespace(
        anime_list=lambda *a, **k: [], episodes=lambda *a, **k: [], downloads=lambda: []
    )
    api.manager = SimpleNamespace(
        db=database, incomplete_download_paths=lambda: set(), _path_within=lambda *_: False
    )
    api.light_novels = SimpleNamespace(state=lambda: {"books": []})
    api.manga = manga
    api.audiobooks = SimpleNamespace(search_catalog=lambda: [])
    api._global_anilist_aliases = lambda ids, allow_network=False: {}
    api._cover_uri = lambda item: ""
    rows = api.global_media_search("Kitsune Days", 40)
    assert sorted(row["volume"] for row in rows if row["kind"] == "manga") == [1, 2]
