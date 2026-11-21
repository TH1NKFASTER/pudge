"""iOS review round 5: the API contract between the phone and the Mac, and round 4's leftovers.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac, so its Codable
structs are read as text: their stored properties (required: not optional, no default; a struct
with its own decoder: what it decodes with `decode(…forKey:)`) are checked against the answers
the Mac's real code builds from invented data, and the app's wiring of this round is guarded.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from pudge import ln_read_marker
from pudge.database import Database
from pudge.iphone_finale import _sequel_view, finale
from pudge.iphone_grammar import IPhoneGrammar, _Job, explanation_view
from pudge.iphone_http import IPhoneHTTPServer
from pudge.iphone_media import IPhoneMedia, _phone_highlights, paragraph_view, subtitle_look
from pudge.iphone_service import IPhoneService
from source_policy import source, sources
from test_iphone_audio_repository import archived, audio_manifest, paired
from test_web_app import make_api

APP = "ios/Pudge/"
CORE = "ios/PudgeCore/Sources/PudgeCore/"


# --------------------------------------------------------------------- the Swift side, as text
_SCALARS = {"Int", "Int64", "Double", "String", "Bool", "UUID", "Data"}


def _swift_types() -> dict[str, dict[str, Any]]:
    """Every Codable/Decodable/Encodable struct of the app: name → {fields: {name: type},
    required: set, rows: bool (decoded from a JSON array), lenient: bool}."""
    found: dict[str, dict[str, Any]] = {}
    texts = {**sources(APP + "**/*.swift"), **sources(CORE + "*.swift")}
    header = re.compile(r"\b(?:public\s+)?struct\s+(\w+)(?:<[^>]*>)?\s*:\s*([^{]*)\{")
    for text in texts.values():
        spans: list[tuple[int, int, str]] = []
        for match in header.finditer(text):
            depth, index = 1, match.end()
            while depth and index < len(text):
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            # A nested type ("ReadingRevisions.Item") is named with its enclosing one.
            outer = [owner for start, end, owner in spans if start < match.start() < end]
            qualified = ".".join(outer[-1:] + [match[1]])
            spans.append((match.start(), index, qualified))
            if not re.search(r"\b(Codable|Decodable|Encodable)\b", match[2]):
                continue
            body = text[match.end() : index - 1]
            # Only this struct's own level (nested types and functions are left out).
            level, flat = 0, []
            for char in body:
                if char == "{":
                    level += 1
                elif char == "}":
                    level -= 1
                elif level == 0:
                    flat.append(char)
            own = "".join(flat)
            fields: dict[str, str] = {}
            required: set[str] = set()
            for statement in re.split(r"[;\n]", own):
                field = re.match(
                    r"\s*(?:@\w+\s+)?(?:public\s+|private\(set\)\s+|public private\(set\)\s+)*(let|var)\s+(\w+)\s*:\s*([^=]+?)\s*(=.*)?$",
                    statement,
                )
                if not field:
                    continue
                kind = field[3].strip()
                fields[field[2]] = kind
                if not kind.endswith("?") and field[4] is None:
                    required.add(field[2])
            # A computed property ("var id: String { … }") left a bare declaration: not stored.
            for name in list(fields):
                if re.search(rf"\bvar\s+{name}\s*:[^\n;=]*\{{", body):
                    fields.pop(name)
                    required.discard(name)
            custom = "init(from decoder: Decoder)" in body
            rows = custom and "unkeyedContainer()" in body
            if custom and not rows:
                required = set(re.findall(r"\.decode\([^)]*?forKey:\s*\.(\w+)\)", body))
            found[qualified] = {"fields": fields, "required": required, "rows": rows, "custom": custom}
    return found


TYPES = _swift_types()


def _check(kind: str, value: Any, path: str, errors: list[str]) -> None:
    """Whether `value` (a JSON value built by the Mac) decodes as the Swift type `kind`."""
    kind = kind.strip()
    if kind.endswith("?"):
        if value is not None:
            _check(kind[:-1], value, path, errors)
        return
    if value is None:
        errors.append(f"{path}: null for {kind}")
        return
    if kind.startswith("[") and kind.endswith("]"):
        inner = kind[1:-1]
        depth = 0
        for at, char in enumerate(inner):
            depth += {"[": 1, "]": -1}.get(char, 0)
            if char == ":" and depth == 0:
                if not isinstance(value, dict):
                    errors.append(f"{path}: {type(value).__name__} for {kind}")
                    return
                for key, item in value.items():
                    _check(inner[at + 1 :], item, f"{path}.{key}", errors)
                return
        if not isinstance(value, list):
            errors.append(f"{path}: {type(value).__name__} for {kind}")
            return
        for number, item in enumerate(value):
            _check(inner, item, f"{path}[{number}]", errors)
        return
    if kind in {"Int", "Int64"}:
        if type(value) is not int:
            errors.append(f"{path}: {value!r} is no integer")
    elif kind == "Double":
        if type(value) not in (int, float):
            errors.append(f"{path}: {value!r} is no number")
    elif kind == "String":
        if not isinstance(value, str):
            errors.append(f"{path}: {value!r} is no string")
    elif kind == "Bool":
        if type(value) is not bool:
            errors.append(f"{path}: {value!r} is no true/false")
    elif kind in TYPES or "." in kind:
        if kind not in TYPES:
            return
        spec = TYPES[kind]
        if spec["rows"]:
            if not isinstance(value, list):
                errors.append(f"{path}: {kind} is a JSON array, got {type(value).__name__}")
            return
        if not isinstance(value, dict):
            errors.append(f"{path}: {kind} is an object, got {type(value).__name__}")
            return
        for name in sorted(spec["required"] - value.keys()):
            errors.append(f"{path}: {kind} needs {name!r}")
        if spec["custom"]:
            return  # its own decoder is lenient about the rest
        for name, field in spec["fields"].items():
            if name in value:
                # A nested type of this one first ("Item" in ReadingRevisions).
                nested = re.sub(
                    r"\b(\w+)\b", lambda m: f"{kind}.{m[1]}" if f"{kind}.{m[1]}" in TYPES else m[1], field
                )
                _check(nested, value[name], f"{path}.{name}", errors)


def _assert_decodes(kind: str, value: Any) -> None:
    assert kind in TYPES, kind
    # As the phone gets it: through JSON.
    value = json.loads(json.dumps(value))
    errors: list[str] = []
    _check(kind, value, kind, errors)
    assert errors == []


def test_the_swift_reader_finds_the_contract_structs() -> None:
    assert {"LightNovelItem", "EpisodeItem", "AudioManifest", "ProgressState", "StateAnswer"} <= TYPES.keys()
    assert TYPES["AudioManifest"]["required"] == {
        "protocolMajor",
        "assetId",
        "editionId",
        "timelineRevision",
        "title",
        "durationMs",
        "parts",
        "chapters",
    }
    assert "id" not in TYPES["EpisodeItem"]["fields"] or TYPES["EpisodeItem"]["fields"]["id"] == "Int"
    assert TYPES["StudyToken"]["rows"] and TYPES["AnimeFinale"]["required"] == {"mediaId"}
    # The catalog's lists are read item by item (one unreadable book never fails the others).
    assert TYPES["CatalogResponse"]["required"] == {"protocolMajor", "serverId", "items"}
    errors: list[str] = []
    _check("LightNovelItem", {"id": 1}, "x", errors)
    assert "x: LightNovelItem needs 'title'" in errors
    errors = []
    _check("EpisodeItem", {**_episode(), "watched": 0}, "x", errors)
    assert errors == ["x.watched: 0 is no true/false"]


# ----------------------------------------------------------------- the Mac's answers, for real
@pytest.fixture
def service(tmp_path):
    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def test_pairing_answers(service) -> None:
    invite = service.invite(["https://192.168.1.2:8766"], "0" * 64)
    _assert_decodes("PairingPayload", invite)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Invented phone",
    }
    _assert_decodes("PairRequest", request)
    _assert_decodes("PairReceipt", service.pair(request))
    service.approve_pairing(request["deviceId"])
    _assert_decodes("PairReceipt", service.pair(request))


def test_audiobook_sync_answers(service) -> None:
    device, manifest = archived(service)
    asset = manifest["assetId"]
    catalog = service.catalog()
    _assert_decodes(
        "CatalogResponse", {**catalog, "errors": [{"title": "x", "error": "OSError"}], "preparing": False}
    )
    _assert_decodes("AudioManifest", catalog["items"][0])
    _assert_decodes(
        "PendingBook", {"assetId": asset, "title": "x", "needsPreparation": True, "durationMs": 1000}
    )
    part = manifest["parts"][0]
    checkpoint = {
        "checkpointId": str(uuid.uuid4()),
        "assetId": asset,
        "editionId": manifest["editionId"],
        "timelineRevision": manifest["timelineRevision"],
        "playbackSessionId": str(uuid.uuid4()),
        "deviceId": device,
        "sequence": 1,
        "baseProgressRevision": 0,
        "positionMs": 10,
        "partId": part["partId"],
        "partPositionMs": 10,
        "reason": "pause",
        "observedAt": int(time.time() * 1000),
    }
    _assert_decodes("AudioCheckpoint", checkpoint)
    _assert_decodes("CheckpointReceipt", service.record_checkpoint(device, checkpoint))
    _assert_decodes("ProgressState", service.progress_view(asset))
    bookmark = {
        "bookmarkId": str(uuid.uuid4()),
        "eventId": str(uuid.uuid4()),
        "assetId": asset,
        "editionId": manifest["editionId"],
        "positionMs": 5,
        "title": "印",
        "deleted": False,
        "baseRevision": 0,
    }
    _assert_decodes("Bookmark", bookmark)
    _assert_decodes("BookmarkReceipt", service.record_bookmark(device, bookmark))
    _assert_decodes("BookmarkEnvelope", service.bookmarks(asset))
    answer = service.sync_state([asset])
    _assert_decodes("StateAnswer", answer)
    _assert_decodes("StateAnswer", service.sync_state([asset], answer["revision"]))
    _assert_decodes("Commands", service.commands(device))
    _assert_decodes(
        "ArchiveReceipt", service.verify_archive(device, part["resourceId"], manifest["editionId"])
    )


def test_archive_answers(service) -> None:
    request, _ = paired(service)
    manifest = audio_manifest(b"other audio")
    plan = service.begin_archive(request["deviceId"], manifest)
    _assert_decodes("ArchivePlan", plan)
    resource = manifest["parts"][0]["resourceId"]
    _assert_decodes(
        "ChunkReceipt",
        service.archive_chunk(request["deviceId"], plan["transferId"], resource, 0, b"other audio"),
    )
    _assert_decodes("CommitReceipt", service.commit_archive(request["deviceId"], plan["transferId"]))


def _episode() -> dict[str, Any]:
    return {
        "id": 3,
        "mediaId": 9,
        "number": 2,
        "title": "Invented show",
        "episodeTitle": "",
        "watched": False,
        "position": 12.5,
        "duration": 1400.0,
        "prepared": False,
        "preparing": False,
    }


def _library(tmp_path):
    """The Mac's window API (its light-novel and manga tables) with the phone service on it."""
    api = make_api(tmp_path)
    mobile = IPhoneService(api.manager.db, tmp_path / "iphone")
    mobile.api = api
    return api, mobile


def test_reading_and_study_answers(tmp_path) -> None:
    _api, service = _library(tmp_path)
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(id,title,file_path,file_type,created_at,updated_at,volume) VALUES(4,'本',?, 'epub',0,0,2)",
            (str(tmp_path / "b.epub"),),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash,char_count) VALUES(4,0,'一','猫だ。','h',3)"
        )
        conn.execute(
            "INSERT INTO manga_books(id,title,path,page_count,position,read_pages,reading_direction,created_at,updated_at) "
            "VALUES(5,'Invented manga 2',?,20,3,3,'rtl',0,0)",
            (str(tmp_path / "m.cbz"),),
        )
    media = service.media
    for item in media.light_novels()["items"]:
        _assert_decodes("LightNovelItem", item)
    _assert_decodes("LightNovelDetail", media.light_novel(4))
    for item in media.manga_list()["items"]:
        _assert_decodes("MangaItem", item)
    view = paragraph_view("猫だ", [{"start": 0, "end": 1, "wordId": 7, "readingIndex": 0}], [], None)
    _assert_decodes("TextParagraph", view)
    _assert_decodes(
        "ChapterText",
        IPhoneMedia._chapter_view(
            {
                "chapter_index": 0,
                "title": "一",
                "paragraphs": ["猫だ", "[[PUDGE_LN_IMAGE_URL:a.png]]"],
                "tokens": [[]],
            }
        ),
    )
    marker = ln_read_marker.phone_view(
        {"chapter_index": 0, "char_offset": 1, "source": "auto", "updated_at": 5.0, "device_id": "mac"},
        "猫だ。",
    )
    _assert_decodes("ReadMarker", marker)
    _assert_decodes("ReaderLook", media.reader_settings())
    _assert_decodes("SubtitleLook", subtitle_look(tmp_path / "none.json"))
    word = IPhoneMedia._word_view(
        7, 0, {"spelling": "猫", "meanings": ["cat"], "dictionary_complete": True}, {}, ""
    )
    _assert_decodes("WordDetail", word)
    _assert_decodes("PlanningSummary", media.planning())
    _assert_decodes("ReadingRevisions", media.reading.check({"items": [{"kind": "ln", "id": 99}]}))
    _assert_decodes("ReadingPackageState", media.reading.state("ln", 4))
    _assert_decodes(
        "MangaPageText",
        {
            "page": 0,
            "status": "ready",
            "regions": [{"x": 0, "y": 0, "width": 1, "height": 1, "text": "あ", "vertical": True}],
        },
    )


def test_media_answers_built_by_the_macs_helpers(service) -> None:
    media = service.media
    episode = _episode()
    _assert_decodes("EpisodeItem", episode)
    shelves = IPhoneMedia._home_shelves(
        {
            "caught_up": [
                {"media_id": 4, "title": "x", "progress": 3, "episodes": 12, "next_airing_at": 1.5e9}
            ],
            "needs_action": [
                {"media_id": 5, "title": "y", "action_code": "missing", "extra_files": [{"kind": "OVA"}]}
            ],
        }
    )
    _assert_decodes("MacHomeShelves", shelves)
    _assert_decodes(
        "AnimeListResponse",
        {
            "items": [
                {
                    "mediaId": 9,
                    "title": "x",
                    "progress": 1,
                    "total": None,
                    "schedule": None,
                    "episodes": [episode],
                }
            ],
            "home": shelves,
            "deleteWatchedAfterHours": 0.0,
        },
    )
    highlights = _phone_highlights(
        {
            "longest_streak_days": 3,
            "busiest_day": {"day": "2026-01-02", "seconds": 60},
            "words": {"total": 3, "by_kind": {"anime": {"total": 3}}},
        }
    )
    _assert_decodes("StatsHighlights", highlights)
    _assert_decodes(
        "StatsSummary",
        {
            "highlights": highlights,
            "period": "7d",
            "totalSeconds": 1.0,
            "activeDays": 1,
            "sessions": 1,
            "streak": 1,
            "breakdown": {"anime": 1.0},
            "days": [{"day": "2026-01-02", "seconds": 1.0, "breakdown": {}}],
            "works": [{"title": "x", "kind": "anime", "seconds": 1.0}],
            "stale": False,
            "builtAt": 1.0,
        },
    )
    gate = media._gate_view(
        {
            "enabled": True,
            "review_actions": {"actions": [{"id": "pass", "label_en": "Pass"}]},
            "cards": [{"wordId": 7, "readingIndex": 0, "readings": [{"text": "ねこ", "readingIndex": 0}]}],
        }
    )
    _assert_decodes("GateStatus", gate)
    media.service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary")
        ),
        study_decks=lambda backend: [{"id": 1, "name": "Main"}],
        study_deck_preference=lambda backend: {"deck_id": "1"},
        study_action=lambda payload: {"ok": True},
        study_states=lambda payload: {"states": [{"wordId": 7, "readingIndex": 0, "states": ["young"]}]},
    )
    _assert_decodes("StudyOptions", media.study_options())
    result = media.study_action({"action": "add", "wordId": 7, "readingIndex": 0, "attemptId": "a" * 16})
    _assert_decodes("StudyActionResult", result)
    _assert_decodes("StudyStates", media.study_states({"pairs": [[7, 0]]}))


def test_finale_and_grammar_answers() -> None:
    anime = SimpleNamespace(media_id=4, episodes=12, format="TV", title="x", user_score=None)
    db = SimpleNamespace(get_anime=lambda media: anime, rating_prompted=lambda media, episode: False)
    _assert_decodes("AnimeFinale", finale(db, 4, 12))
    sequel = _sequel_view({"media_id": 5, "title": "y", "episodes": 12, "released": True})
    _assert_decodes("FinaleSequelAnswer", {"sequel": sequel})
    _assert_decodes(
        "FinaleActionAnswer", {"ok": True, "reason": "", "anilistPending": False, "sequel": sequel}
    )
    _assert_decodes("FinaleScoreReceipt", {"ok": True, "score": 8.0, "anilistPending": False})
    _assert_decodes(
        "EpisodeWatchedAnswer", {"ok": True, "outcome": "counted", "message": "", "finale": finale(db, 4, 12)}
    )
    explanation = explanation_view(
        {"translation": "A cat.", "points": [{"pattern": "だ", "meaning_in_context": "is"}]}, "en"
    )
    _assert_decodes("GrammarExplanation", explanation)
    job = _Job(id="j", device="d", kind="explain", started=0.0)
    _assert_decodes("GrammarJob", IPhoneGrammar._view(job))
    job.result = {"kind": "explain", "explanation": explanation}
    job.done.set()
    _assert_decodes("GrammarJob", IPhoneGrammar._view(job))


def test_the_requests_the_phone_sends_are_the_ones_the_mac_reads() -> None:
    # The fields the Mac's handlers read (iphone_media / iphone_service), named as the phone sends them.
    reads = {
        "NovelPositionRequest": {
            "chapter",
            "offset",
            "finished",
            "observedAt",
            "sentAt",
            "paragraph",
            "finishOnly",
        },
        "ReadMarkerRequest": {
            "chapter",
            "paragraph",
            "paragraphOffset",
            "kind",
            "observedAt",
            "sentAt",
            "markerId",
        },
        "MangaPositionRequest": {"page", "read", "observedAt", "sentAt", "readThrough"},
        "EpisodePositionRequest": {"seconds", "duration", "observedAt", "ranges", "sentAt"},
        "EpisodeWatchedRequest": {"duration", "activeSeconds"},
        "StudyActionRequest": {
            "action",
            "grade",
            "wordId",
            "readingIndex",
            "deckId",
            "sentence",
            "attemptId",
        },
        "GateReviewRequest": {"wordId", "readingIndex", "grade", "attemptId"},
        "HandoffRequest": {"operationId", "assetId", "expectedRevision", "sourceSessionId", "targetReady"},
        "ActivityInterval": {
            "id",
            "kind",
            "item",
            "start",
            "end",
            "active",
            "from",
            "to",
            "timezone",
            "offset",
        },
        "DeviceInventory": {"idiom", "episodes", "lightNovels", "manga", "audiobooks"},
        "FinaleScoreRequest": {"score", "skip", "episode"},
        "SubtitleDelayRequest": {"ms"},
    }
    for name, fields in reads.items():
        assert set(TYPES[name]["fields"]) == fields, name


# ---------------------------------------------------------------- capabilities and routes
class _Mac:
    server_id = "server"

    def authenticate(self, token):
        return "phone"

    def still_authorized(self, device):
        return True


def test_every_capability_the_phone_asks_for_is_advertised() -> None:
    server = IPhoneHTTPServer(("127.0.0.1", 0), _Mac())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("GET", "/api/v2/capabilities")
        answer = json.loads(connection.getresponse().read())
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
    advertised = set(answer["capabilities"])
    asked = set()
    for text in sources(APP + "**/*.swift").values():
        asked |= set(re.findall(r'macSupports\("([a-z-]+)"\)', text))
    assert {"ln-finish", "video-stream", "state-batch"} <= asked
    assert asked <= advertised
    doc = source("ios/PROTOCOL.md")
    for name in asked:
        assert f"`{name}`" in doc, name
    # Every media route the phone names is in the protocol's list.
    for route in ("ln-image/", "streams/<id>/keepalive", "grammar/", "anime/<id>/score", "/marker"):
        assert route in doc, route


# ----------------------------------------------------------------- Part A: round 4 leftovers
def test_mark_light_novel_read_leaves_the_place_and_the_marker(tmp_path) -> None:
    _api, service = _library(tmp_path)
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(id,title,file_path,file_type,created_at,updated_at) VALUES(6,'本',?,'epub',0,0)",
            (str(tmp_path / "c.epub"),),
        )
        for index in range(2):
            text = "猫が鳴いた。" * 100
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash,char_count) VALUES(6,?,?,?,?,?)",
                (index, f"{index}", text, f"h{index}", len(text)),
            )
    # A volume never opened, marked read from its finished audiobook.
    answer = service.media.handle(
        "POST",
        "/api/v2/media/ln/6/position",
        {},
        lambda: {
            "chapter": 1,
            "offset": 0.5,
            "finished": True,
            "finishOnly": True,
            "observedAt": time.time(),
        },
        device="phone",
    )
    assert answer["ok"] is True and answer["finished"] is True
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT finished,current_chapter,position_updated_at FROM ln_books WHERE id=6"
        ).fetchone()
        marker = conn.execute("SELECT 1 FROM ln_bookmarks WHERE book_id=6").fetchone()
    assert row[0] == 1 and int(row[1] or 0) == 0 and not row[2] and marker is None
    media = source(APP + "Features/Media/MediaModel.swift")
    finish = media[media.index("func markNovelFinished(") : media.index("func liveRefreshLoop(")]
    assert (
        'if app.macSupports("ln-finish") { request.finishOnly = true } else { request.observedAt = 1 }'
        in finish
    )


def test_finished_survives_a_launch_without_the_mac() -> None:
    model = source(APP + "App/PudgeModel.swift")
    assert "macFinished.finished(book.assetId)" in model
    # Every answer of the Mac for a book goes through the kept flag.
    assert "progress[item.assetId] = " not in model and "progress[book.assetId] = " not in model
    assert model.count("noteMacProgress(") >= 5
    core = source(CORE + "LibraryCards.swift")
    assert "public struct MacFinishedFlags" in core
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/ContractLeniencyTests.swift")
    assert "func testTheKeptFinishedFlagCountsOnlyWhenTheMacSaidFinished()" in tests


def test_lock_screen_artwork_is_keyed_by_the_cover_and_decoded_off_main() -> None:
    player = source(APP + "Features/Audio/AudioPlayerService.swift")
    body = player[
        player.index("private func currentArtwork(") : player.index(
            "nonisolated private static let artworkPixels"
        )
    ]
    assert "artworkKey == cover.sha256" in body and "artworkResource" not in player
    assert "Task.detached(priority: .utility)" in body
    assert body.index("Task.detached") < body.index("DownsampledImage.load(")


def test_restore_and_start_take_the_same_record_lock() -> None:
    transfers = source(APP + "Features/Transfers/TransferManager.swift")
    restore = transfers[transfers.index("func restore()") : transfers.index("func updateConnection")]
    assert restore.index("self.recordLock.lock()") < restore.index('record.status = "paused"')
    assert restore.index('record.status = "paused"') < restore.index("self.recordLock.unlock()")
    start = transfers[transfers.index("private func start(") : transfers.index("func pause(")]
    assert start.index("recordLock.lock(); defer { recordLock.unlock() }") < start.index("stateLock.lock()")


def test_the_catalog_is_read_leniently_and_marks_every_listed_book() -> None:
    model = source(APP + "App/PudgeModel.swift")
    assert "LenientList<AudioManifest>" in model
    refresh = model[model.index("private func refreshOnce()") : model.index("private func noteSkipped(")]
    assert "store.remoteIds(serverId: catalog.serverId)" in refresh
    assert "for book in catalog.items where !marked.contains(book.id) { try store.markRemote(" in refresh
    assert "catalog.skipped.isEmpty {" in refresh  # nothing retired while a book was left out
    # The capabilities come before the catalog route is chosen.
    assert refresh.index('connection.send("/api/v2/capabilities"') < refresh.index(
        'macSupports("lazy-catalog")'
    )
    store = source(CORE + "LocalStore.swift")
    assert "compactMap { try? AudioManifest.decode(" in store


def test_the_read_tab_asks_the_mac_until_it_has_the_macs_list() -> None:
    screen = source(APP + "UI/ReadScreen.swift")
    assert ".task { if !media.novelsFromMac { await media.refreshReading() } }" in screen
    assert ".task { if media.novels.isEmpty { await media.refreshReading() } }" not in screen


def test_read_along_line_times_from_the_mac_reach_the_player() -> None:
    from pudge import anime_audiobooks as aa

    chapter = aa.read_along_chapter(
        [(1.0, 3.0, "右だ"), (1.0, 3.0, "左だ"), (6.0, 8.0, "終わり")], index=0, title="Ep", duration=9
    )
    shifted = aa.shift_chapter(chapter, index=1, title="Ep", offset_ms=60_000)
    for value in (chapter, shifted):
        _assert_decodes("ReadAlongChapter", value)
        assert all(type(t) is int for item in value["sentences"] for t in item["t"])
    assert [item["t"][0] for item in shifted["sentences"]] == [61_000, 61_000, 66_000]
    assert "own.count == 2 { return own[0] }" in source(CORE + "ReadAlong.swift")
    assert "chapter.startMs(ofSentence: range.lowerBound)" in source(APP + "UI/PlayerScreen.swift")
