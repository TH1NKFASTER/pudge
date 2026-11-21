"""An iPhone light-novel package while Jiten throttles parses (audit JITEN-1).

Packing an unparsed 230k-character volume used to send 14k-character batches every
0.65 s: Jiten's 200k-a-minute parse budget answered 429, Pudge gave up after 1.8 s and the
chapter went to the phone without word marks, never to be rebuilt. Now the package waits
within the shared budget (and for Retry-After) and every chapter has its words; when Jiten
is really down the package says which chapters lack words and parses them later, which
changes its revision so the phone fetches it again.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from jiten_fake_server import install
from pudge import iphone_reading, jiten_client
from pudge.config import AppConfig
from pudge.database import Database
from pudge.iphone_reading import read_package
from pudge.light_novels import LightNovelService

PARAGRAPH = "吾輩は猫である。名前はまだ無い。" * 6


@pytest.fixture
def phone(tmp_path, monkeypatch):
    from pudge.iphone_service import IPhoneService

    clock, server = install(monkeypatch)
    monkeypatch.setattr(iphone_reading, "time", clock.module())
    database = tmp_path / "db.sqlite"
    service = IPhoneService(Database(database), tmp_path / "mobile")
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = database
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    reader = LightNovelService(config)
    reader.save_settings({"jiten_api_key": "jiten-test-key", "parse_ahead": "current"})
    book = tmp_path / "library" / "猫.txt"
    book.write_text(
        "\n\n".join(f"第{i}章\n" + "\n".join(f"{PARAGRAPH}{i}-{n}" for n in range(140)) for i in range(17)),
        encoding="utf-8",
    )
    book_id = int(reader.import_file(book)["id"])
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=reader.settings,
            state=lambda: {"books": []},
            chapter=reader.chapter,
            chapter_fast=reader.chapter_fast,
            parse_chapter_for_statistics=reader.parse_chapter_for_statistics,
            jiten_live_states=reader.jiten_live_states,
            study_provider_capabilities=reader.study_provider_capabilities,
            cover_cache_dir=str(tmp_path / "covers"),
        ),
        logger=None,
    )
    return SimpleNamespace(service=service, reader=reader, book_id=book_id, clock=clock, server=server)


def _build(phone) -> dict:
    reading = phone.service.media.reading
    revision = reading.revision("ln", phone.book_id)
    reading._worker("ln", phone.book_id, revision)  # the background job, run here
    state = reading.state("ln", phone.book_id)
    assert state["state"] == "ready", state
    package = state["package"]
    files = read_package(phone.service.resource(package["resourceId"], package["sha256"]))
    return {"state": state, "files": files, "description": json.loads(files["package.json"])}


def _chapters_without_words(files: dict) -> list[str]:
    missing = []
    for name, data in files.items():
        if name.startswith("chapter-"):
            view = json.loads(data)
            if not any(paragraph.get("tokens") for paragraph in view["paragraphs"]):
                missing.append(name)
    return missing


def test_every_chapter_of_a_package_built_while_jiten_throttles_has_its_words(phone):
    phone.server.use_parse_budget(150_000)  # the Jiten site / JitenMPV parse at the same time
    built = _build(phone)
    chapters = [name for name in built["files"] if name.startswith("chapter-")]
    assert len(chapters) == len(phone.service.media.light_novel(phone.book_id)["chapters"]) >= 10
    assert _chapters_without_words(built["files"]) == []
    assert "chaptersWithoutWords" not in built["description"]
    assert phone.server.statuses("reader/parse").count(429) <= 1
    assert phone.server.max_per_window(chars=True) <= jiten_client.PARSE_CHARS_PER_MINUTE
    assert phone.server.max_per_window() <= jiten_client.REQUESTS_PER_MINUTE


def test_a_package_built_during_an_outage_is_rebuilt_once_jiten_answers(phone, monkeypatch):
    original = phone.server.handle
    down = {"on": True}

    def handle(method, url, **kwargs):
        if down["on"] and url.endswith("reader/parse"):
            phone.server.log.append(
                {"at": phone.clock.time(), "path": "reader/parse", "chars": 0, "status": 503}
            )
            return httpx.Response(503, json={}, request=httpx.Request(method, url))
        return original(method, url, **kwargs)

    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: handle("POST", str(url), **kwargs))
    later: list = []
    monkeypatch.setattr(
        iphone_reading.ReadingPackages,
        "_parse_later",
        lambda self, reader, book_id, indices: later.append((reader, book_id, list(indices))),
    )
    built = _build(phone)
    missing = built["description"]["chaptersWithoutWords"]
    assert missing and len(_chapters_without_words(built["files"])) == len(missing)
    # The outage pause stopped the requests: three failed parses (three tries each), then
    # nothing more was sent for the other chapters.
    chapters = len(built["description"]["book"]["chapters"])
    assert phone.server.count("reader/parse") <= 3 * 3 + 3 < chapters * 3
    # Once Jiten answers, the chapters are parsed in the background and the revision changes.
    # The built package's revision, read before Jiten answers again: once it does, another
    # background reader of the app may parse a chapter at any moment (seen on macOS).
    first = phone.service.media.reading.revision("ln", phone.book_id)
    assert first == built["state"]["revision"]
    down["on"] = False
    phone.clock.sleep(jiten_client.CLIENT.retry_in() + 1)
    reader, book_id, indices = later[0]
    with phone.service.db.connect() as conn:
        hashes = [
            row[0]
            for row in conn.execute(
                f"SELECT text_hash FROM ln_chapters WHERE book_id=? AND chapter_index IN ({','.join('?' * len(indices))})",
                (book_id, *indices),
            )
        ]
    for digest in hashes:
        assert reader.parse_chapter_for_statistics(digest)
    assert phone.service.media.reading.revision("ln", phone.book_id) != first
    rebuilt = _build(phone)
    assert _chapters_without_words(rebuilt["files"]) == []


def test_parse_later_retries_the_missing_chapters_after_a_delay(phone, monkeypatch):
    calls: list[str] = []
    started: list = []

    class Thread:
        def __init__(self, target, name, daemon):
            started.append(name)
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(iphone_reading.threading, "Thread", Thread)
    reader = SimpleNamespace(parse_chapter_for_statistics=lambda digest: calls.append(digest))
    phone.service.media.reading._parse_later(reader, phone.book_id, [0, 2])
    assert len(calls) == 2 and started
    assert phone.clock.sleeps and phone.clock.sleeps[0] >= 120  # after a delay, on the fake clock
