"""Leftovers of the last review round.

Mac: the phone catalog has an ETag (304 to If-None-Match); WebVTT subtitles are converted to
SRT for the sync engines; aria2's metadata.json forgets finished downloads whose files are
gone; the window's cached episode-numbering offsets follow saved/forgotten numbering rules.
iPhone/iPad (built in Xcode on the Mac; checked here by their source, the logic by PudgeCore's
CatalogCacheTests): the catalog is kept with its ETag, read-along texts download straight to a
file, subtitle files are decoded/encoded off the main thread, going to the background sends
the last place after a running sync, the manga two-page view asks only while aligning is pending.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from pudge.iphone_http import IPhoneHTTPServer
from pudge.providers.aria2 import Aria2Client
from pudge.subtitle_formats import convert_to_plain_srt, parse_srt, parse_webvtt

from source_policy import source


def _swift(path: str) -> str:
    return source(path)


# --- 1. GET /api/v2/catalog: ETag + If-None-Match → 304 ---------------------------------------


class _CatalogService:
    server_id = "server"

    def __init__(self) -> None:
        self.items = [{"assetId": "a1", "title": "Invented book"}]
        self.preparing = False
        self.walks = 0

    def authenticate(self, token):
        return "phone"

    def still_authorized(self, device):
        return True

    def refresh_catalog(self, *, lazy=False):
        self.walks += 1
        return {"errors": [], "preparing": self.preparing}

    def catalog(self, *, lazy=False):
        return {"protocolMajor": 2, "serverId": self.server_id, "items": list(self.items)}


def _ask(port: int, route: str, etag: str | None = None):
    headers = {"Authorization": "Bearer token"}
    if etag is not None:
        headers["If-None-Match"] = etag
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", route, None, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.getheader("ETag"), data


def test_an_unchanged_catalog_is_answered_304_and_a_change_in_full() -> None:
    service = _CatalogService()
    server = IPhoneHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        status, etag, data = _ask(port, "/api/v2/catalog?lazy=1")
        assert status == 200 and etag and json.loads(data)["items"] == service.items
        # The same library: no body, the same tag; the walk still ran (preparation goes on).
        assert _ask(port, "/api/v2/catalog?lazy=1", etag) == (304, etag, b"")
        assert service.walks == 2
        # Still preparing is another answer (the phone must ask again shortly).
        service.preparing = True
        status, preparing, data = _ask(port, "/api/v2/catalog?lazy=1", etag)
        assert status == 200 and preparing != etag and json.loads(data)["preparing"] is True
        service.preparing = False
        # A book added on the Mac: the whole catalog with a new tag.
        service.items.append({"assetId": "a2", "title": "Another invented book"})
        status, changed, data = _ask(port, "/api/v2/catalog", etag)
        assert status == 200 and changed not in {etag, preparing} and len(json.loads(data)["items"]) == 2
        # An older phone (no If-None-Match) always gets it in full.
        assert _ask(port, "/api/v2/catalog")[0] == 200
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_the_catalog_is_listed_in_a_stable_order(tmp_path) -> None:
    from pudge.database import Database
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    with service.db.connect() as conn:
        for asset in ("c", "a", "b"):
            conn.execute(
                "INSERT INTO iphone_assets(id,book_id,manifest) VALUES(?,?,?)",
                (asset, None, json.dumps({"assetId": asset})),
            )
    assert [item["assetId"] for item in service.catalog()["items"]] == ["a", "b", "c"]
    service._catalog_pending = {"z": {"assetId": "z"}, "y": {"assetId": "y"}}
    assert [item["assetId"] for item in service.catalog(lazy=True)["pending"]] == ["y", "z"]


def test_the_capability_and_the_protocol_name_the_catalog_etag() -> None:
    service = _CatalogService()
    server = IPhoneHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _etag, data = _ask(server.server_port, "/api/v2/capabilities")
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
    assert status == 200 and "catalog-etag" in json.loads(data)["capabilities"]
    doc = _swift("ios/PROTOCOL.md")
    assert "capability `catalog-etag`" in doc and "If-None-Match" in doc


def test_ios_the_phone_keeps_the_catalog_and_reuses_it_on_304() -> None:
    model = _swift("ios/Pudge/App/PudgeModel.swift")
    assert "let cache = CatalogCache(directory: store.directory), server = paired?.serverId" in model
    assert "var asked = try await connection.dataIfChanged(route, etag: sentTag)" in model
    # Unchanged: only manifests missing here are saved again.
    assert "let saved = try answer.unchanged ? store.manifestIds() : Set<String>()" in model
    assert "for book in catalog.items where !saved.contains(book.id) {" in model
    # Kept only after it was applied.
    assert "cache.keep(route: route, serverId: server, etag: etag, body: body)" in model
    connection = _swift("ios/Pudge/Networking/MacConnection.swift")
    assert (
        "func dataIfChanged(_ route: String, etag: String?) async throws -> (body: Data?, etag: String?)"
        in connection
    )
    core = _swift("ios/PudgeCore/Sources/PudgeCore/CatalogCache.swift")
    assert "public struct CatalogCache" in core
    tests = _swift("ios/PudgeCore/Tests/PudgeCoreTests/CatalogCacheTests.swift")
    assert "func testAKeptAnswerIsAskedWithItsTagAndReusedOn304()" in tests


# --- 2-5. iPhone/iPad --------------------------------------------------------------------------


def test_ios_read_along_texts_download_straight_to_a_file() -> None:
    model = _swift("ios/Pudge/App/PudgeModel.swift")
    covers = model[
        model.index("private func prefetchCovers(") : model.index("func syncAfterRunning() async {")
    ]
    assert "connection.bytes(" not in covers and "data.write(to: target" not in covers
    assert "try await connection.download(" in covers
    connection = _swift("ios/Pudge/Networking/MacConnection.swift")
    assert "let (location, response) = try await session.download(for: request(route))" in connection


def test_ios_subtitle_files_are_decoded_and_written_off_the_main_thread() -> None:
    media = _swift("ios/Pudge/Features/Media/MediaModel.swift")
    assert "func subtitles(_ id: Int) async -> SubtitleTrack? {" in media
    assert "try JSONEncoder().encode(track).write(to: cuesURL(" not in media
    assert "try await writeSubtitlesNow(item.id, track: track)" in media
    assert "try await writeSubtitlesNow(episode.id, track: track)" in media
    # A delete cancels a write still running (the file is not brought back).
    assert "subtitleWrites.removeValue(forKey: id)?.cancel()" in media
    watch = _swift("ios/Pudge/UI/WatchScreen.swift")
    assert "track = await media.subtitles(episode.id)" in watch
    assert "guard !playerClosed else { return }" in watch


def test_ios_going_to_the_background_sends_the_place_after_a_running_sync() -> None:
    app = _swift("ios/Pudge/App/PudgeApp.swift")
    assert "await model.syncAfterRunning(); await model.media.sendVideoPositions()" in app
    model = _swift("ios/Pudge/App/PudgeModel.swift")
    assert "if syncing { await withCheckedContinuation { syncWaiters.append($0) } }" in model
    assert "for waiter in waiters { waiter.resume() }" in model


def test_ios_two_pages_ask_only_while_alignment_is_pending() -> None:
    read = _swift("ios/Pudge/UI/ReadScreen.swift")
    assert "let working = align && ((alignment?.pending ?? 0) > 0 || alignAskPending)" in read
    assert "guard twoPages, working else { continue }" in read
    assert "waited >= 30" not in read


# --- 6. WebVTT → SRT -------------------------------------------------------------------------

VTT = """﻿WEBVTT - invented captions
Kind: captions

STYLE
::cue { color: yellow }

NOTE a comment
with --> nothing in it? no: notes never hold timings

1
00:00:01.000 --> 00:00:02.500 line:90% align:center
<v Speaker A>こんにちは</v>

intro
00:03.250 --> 00:05.000
<c.yellow>二行目</c>
<i>テスト</i>&amp;

01:00:00.5 --> 01:00:01.000
<00:00:00.600>時間<00:00:00.800>タグ
"""


def test_webvtt_cues_are_parsed_without_header_notes_settings_or_tags() -> None:
    cues = parse_webvtt(VTT)
    assert [(start, end) for start, end, _ in cues] == [(1.0, 2.5), (3.25, 5.0), (3600.5, 3601.0)]
    assert cues[0][2] == "こんにちは"
    assert cues[1][2] == "二行目\nテスト&amp;"
    assert cues[2][2] == "時間タグ"


def test_a_vtt_candidate_is_converted_to_srt(tmp_path) -> None:
    source = tmp_path / "Invented Show - 01.ja.vtt"
    source.write_text(VTT, encoding="utf-8")
    output, result = convert_to_plain_srt(source, tmp_path / "cache")
    assert result["converted"] is True and result["method"] == "webvtt"
    assert output.suffix == ".srt"
    assert [cue[2] for cue in parse_srt(output)] == ["こんにちは", "二行目\nテスト&", "時間タグ"]
    text = output.read_text(encoding="utf-8")
    assert "こんにちは" in text and "テスト&" in text and "<" not in text and "WEBVTT" not in text
    assert "00:00:03,250 --> 00:00:05,000" in text and "01:00:00,500 --> 01:00:01,000" in text
    # Cached the second time.
    assert convert_to_plain_srt(source, tmp_path / "cache") == (
        output,
        {"reason": "cached", "converted": True, "output": str(output)},
    )
    empty = tmp_path / "empty.vtt"
    empty.write_text("WEBVTT\n\nNOTE nothing\n", encoding="utf-8")
    assert convert_to_plain_srt(empty, tmp_path / "cache")[0] == empty


def test_other_formats_are_not_changed(tmp_path) -> None:
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
    assert convert_to_plain_srt(srt, tmp_path / "cache") == (
        srt,
        {"reason": "already_srt", "converted": False},
    )
    other = tmp_path / "a.sub"
    other.write_text("{1}{2}x\n", encoding="utf-8")
    assert convert_to_plain_srt(other, tmp_path / "cache") == (
        other,
        {"reason": "unsupported_format", "converted": False},
    )


# --- 7. aria2 metadata.json of finished downloads --------------------------------------------

HASH_GONE = "1" * 40
HASH_KEPT = "2" * 40
HASH_LIVE_GONE = "3" * 40
HASH_AWAY = "4" * 40


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, (bytes, str)):
        raw = value.encode() if isinstance(value, str) else value
        return b"%d:%s" % (len(raw), raw)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    return b"d" + b"".join(_bencode(key) + _bencode(value[key]) for key in sorted(value)) + b"e"


def _saved_torrent(folder: Path, info_hash: str, name: str, length: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"info": {"name": name, "length": length, "piece length": 16384, "pieces": b"\0" * 20}}
    (folder / f"{info_hash}.torrent").write_bytes(_bencode(payload))


def test_finished_downloads_whose_files_are_gone_leave_metadata_json(tmp_path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    library = tmp_path / "library"
    gone_dir, kept_dir, live_dir = library / "Gone", library / "Kept", library / "Live"
    _saved_torrent(gone_dir, HASH_GONE, "Invented - 01.mkv", 10)  # its video was deleted
    _saved_torrent(kept_dir, HASH_KEPT, "Invented - 02.mkv", 10)
    (kept_dir / "Invented - 02.mkv").write_bytes(b"0123456789")
    live_dir.mkdir(parents=True)
    away = tmp_path / "Volumes" / "Unplugged" / "Show"  # a drive that is not mounted
    record = {"completed_on": 1, "title": "Invented", "media_id": 1}
    with client._metadata_transaction() as current:
        current["gidgone"] = {**record, "info_hash": HASH_GONE, "save_path": str(gone_dir)}
        current["gidparent"] = {
            **record,
            "completed_on": 0,
            "info_hash": HASH_GONE,
            "save_path": str(gone_dir),
        }
        current["gidkept"] = {**record, "info_hash": HASH_KEPT, "save_path": str(kept_dir)}
        current["gidfolder"] = {**record, "info_hash": "5" * 40, "save_path": str(library / "Deleted")}
        current["gidaway"] = {**record, "info_hash": HASH_AWAY, "save_path": str(away)}
        current["gidrunning"] = {
            **record,
            "completed_on": 0,
            "info_hash": "6" * 40,
            "save_path": str(gone_dir),
        }
        current["gidlive"] = {**record, "info_hash": HASH_LIVE_GONE, "save_path": str(live_dir)}
    calls: list[tuple[str, list]] = []
    live = {
        "gid": "gidlive",
        "status": "complete",
        "totalLength": "10",
        "completedLength": "10",
        "dir": str(live_dir),
        "infoHash": HASH_LIVE_GONE,
        "files": [{"path": str(live_dir / "Live - 03.mkv"), "length": "10", "selected": "true"}],
    }

    def rpc(method, params=None):
        calls.append((method, list(params or [])))
        if method == "aria2.tellStopped":
            return [live]
        if method in {"aria2.tellActive", "aria2.tellWaiting"}:
            return []
        if method in {"aria2.removeDownloadResult", "aria2.saveSession", "aria2.forceRemove"}:
            return "OK"
        raise AssertionError(method)

    monkeypatch.setattr(client, "_require_observable", lambda: None)
    monkeypatch.setattr(client, "_rpc_raw", rpc)
    items = client.torrents()
    left = client._load_metadata()
    # Finished and gone: the record (and its magnet parent of the same hash) goes, the live
    # finished task's aria2 row too. Its file there, its drive away, or not finished: kept.
    assert set(left) == {"gidkept", "gidaway", "gidrunning"}
    assert ("aria2.removeDownloadResult", ["gidlive"]) in calls
    assert not any(method == "aria2.forceRemove" for method, _ in calls)  # stopped already
    assert [item.torrent_hash for item in items] == [HASH_KEPT]
    # A second listing changes nothing more.
    calls.clear()
    client.torrents()
    assert set(client._load_metadata()) == {"gidkept", "gidaway", "gidrunning"}
    assert not any(method == "aria2.removeDownloadResult" for method, _ in calls)
    client.close()


# --- 8. numbering rules invalidate the cached offsets ----------------------------------------


def test_saved_or_forgotten_numbering_rules_drop_cached_offsets(tmp_path, monkeypatch) -> None:
    import pudge.web_app as web_app
    from pudge.database import Database, numbering_rules_version

    db = Database(tmp_path / "db.sqlite")
    api = object.__new__(web_app.WebAppApi)
    offsets = iter([12, 24, 36])
    monkeypatch.setattr(
        web_app, "resolve_episode_numbering", lambda *a, **k: type("R", (), {"offset": next(offsets)})
    )
    api._episode_offset_cache = {}
    api.config = api.logger = None
    api.manager = type("M", (), {"db": db})()

    class _Anime:
        media_id = 7

    assert api._episode_numbering_offset(_Anime()) == 12
    assert api._episode_numbering_offset(_Anime()) == 12  # cached
    before = numbering_rules_version()
    db.store_release_numbering_rule(7, "Invented", "s2", 12, example_title="[Invented] Show - 13")
    assert numbering_rules_version() == before + 1
    assert api._episode_numbering_offset(_Anime()) == 24
    db.forget_release_numbering_rules(7)
    assert api._episode_numbering_offset(_Anime()) == 36
