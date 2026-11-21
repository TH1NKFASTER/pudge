"""IOS-7: the first catalog request after pairing hashed every audiobook file and converted every
FLAC/Opus/Ogg book to AAC (hours of CPU, ~1.4 GB per 20 h book), oldest book first, also for
books never downloaded; those copies were all protected from the 20 GB budget.

Now: a phone that says so (`?lazy=1`) gets manifests only for books already prepared and the
others as `pending`; `POST /api/v2/assets/prepare` prepares one book first of all. Catalog walks
take books already prepared and asked-for ones first, then the newest. The size budget applies
to prepared copies no paired device downloaded."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from pudge import iphone_audio
from pudge.cache_management import _iphone_prepared_in_use
from pudge.database import Database


@pytest.fixture
def service(tmp_path, monkeypatch):
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    service.prepared_calls = []

    def prepare(source, root, **_kwargs):
        service.prepared_calls.append(Path(source).name)
        return {
            "path": Path(source),
            "originalSHA256": service.file_sha256(Path(source)),
            "sha256": service.file_sha256(Path(source)),
            "canonicalDurationMs": 1000,
            "playbackDurationMs": 1000,
            "sizeBytes": Path(source).stat().st_size,
        }

    monkeypatch.setattr(iphone_audio, "prepare_audio", prepare)
    return service


def _book(service, tmp_path, name: str) -> int:
    source = tmp_path / "lib" / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(name.encode() * 50)
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,0,0)",
            (str(source), name, 1),
        ).lastrowid
        conn.execute(
            "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,?,1,0,1)",
            (book, str(source), name),
        )
    return int(book)


def test_a_lazy_catalog_prepares_nothing_and_lists_the_books_pending(service, tmp_path):
    for name in ("old.flac", "new.opus"):
        _book(service, tmp_path, name)
    assert service.refresh_catalog(lazy=True)["errors"] == []
    catalog = service.catalog(lazy=True)
    assert service.prepared_calls == [] and catalog["items"] == []
    assert sorted(item["title"] for item in catalog["pending"]) == ["new.opus", "old.flac"]
    assert all(item["needsPreparation"] and item["durationMs"] == 1000 for item in catalog["pending"])
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_hashes").fetchone()[0] == 0


def test_asking_for_one_book_prepares_it_first_and_returns_its_manifest(service, tmp_path):
    _book(service, tmp_path, "first.flac")
    _book(service, tmp_path, "second.flac")
    service.refresh_catalog(lazy=True)
    wanted = next(item for item in service.catalog(lazy=True)["pending"] if item["title"] == "first.flac")
    answer = service.prepare_asset(wanted["assetId"])
    assert answer["status"] == "ready" and answer["manifest"]["assetId"] == wanted["assetId"]
    assert service.prepared_calls == ["first.flac"]
    catalog = service.catalog(lazy=True)
    assert [item["title"] for item in catalog["items"]] == ["first.flac"]
    assert [item["title"] for item in catalog["pending"]] == ["second.flac"]
    # Once prepared it is cheap: the next lazy walk keeps its manifest without preparing again.
    service.refresh_catalog(lazy=True)
    assert service.prepared_calls == ["first.flac"]
    assert [item["title"] for item in service.catalog(lazy=True)["items"]] == ["first.flac"]
    with pytest.raises(KeyError):
        service.prepare_asset(str(uuid.uuid4()))


def test_a_failed_preparation_is_reported(service, tmp_path, monkeypatch):
    _book(service, tmp_path, "broken.flac")
    service.refresh_catalog(lazy=True)
    asset = service.catalog(lazy=True)["pending"][0]["assetId"]

    def fail(*_args, **_kwargs):
        raise ValueError("No audio stream")

    monkeypatch.setattr(iphone_audio, "prepare_audio", fail)
    assert service.prepare_asset(asset) == {"status": "failed", "error": "ValueError"}


def test_older_phones_still_get_every_book_newest_first(service, tmp_path):
    for name in ("a.flac", "b.flac", "c.flac"):
        _book(service, tmp_path, name)
    service.refresh_catalog()
    assert service.prepared_calls == ["c.flac", "b.flac", "a.flac"]
    assert "pending" not in service.catalog()
    assert len(service.catalog()["items"]) == 3


def test_prepared_copies_nobody_downloaded_fall_under_the_budget(tmp_path):
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")
    copies = {}
    for name in ("kept", "free"):
        source = tmp_path / f"{name}.flac"
        source.write_bytes(name.encode())
        copy = service.root / "prepared" / f"{name}-aac-v1.m4a"
        copy.parent.mkdir(exist_ok=True)
        copy.write_bytes(b"aac " + name.encode())
        stat = source.stat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?)",
                (
                    str(source),
                    stat.st_size,
                    stat.st_mtime_ns,
                    json.dumps({"path": str(copy), "sizeBytes": 5}),
                ),
            )
            conn.execute(
                "INSERT INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                (name, "0" * 64, 5, str(copy)),
            )
        copies[name] = copy.resolve()
    with db.connect() as conn:
        conn.execute("INSERT INTO iphone_devices(id,name,token_hash) VALUES('phone','iPhone','x')")
    service.record_download("phone", "kept")
    assert _iphone_prepared_in_use(db) == {copies["kept"]}
    # A revoked device no longer keeps its copies.
    service.revoke("phone")
    assert _iphone_prepared_in_use(db) == set()


def test_copies_served_before_downloads_were_recorded_stay_protected(tmp_path):
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "db.sqlite")
    with db.connect() as conn:
        # A library from before this version: resources but no download records yet.
        conn.execute(
            "CREATE TABLE iphone_resources(id TEXT PRIMARY KEY,sha TEXT NOT NULL,size INTEGER NOT NULL,path TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO iphone_resources VALUES('old','0',1,'/x')")
    IPhoneService(db, tmp_path / "mobile")
    with db.connect() as conn:
        assert conn.execute("SELECT resource,device FROM iphone_downloads").fetchall()[0][:] == (
            "old",
            "legacy",
        )


def test_a_complete_download_is_recorded(tmp_path):
    import http.client
    import ssl

    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService
    from test_iphone_audio_repository import allowed

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    data = tmp_path / "part.mp3"
    data.write_bytes(b"0123456789")
    sha = service.file_sha256(data)
    with service.db.connect() as conn:
        service.register_resource(conn, "11111111-1111-4111-8111-111111111111", sha, 10, data)
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Phone",
    }
    token = allowed(service, request)["token"]
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        route = f"/api/v2/resources/11111111-1111-4111-8111-111111111111/content?sha256={sha}"
        for byte_range, recorded in (("bytes=0-4", False), ("bytes=5-", True)):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=10
            )
            connection.request(
                "GET", route, headers={"Authorization": "Bearer " + token, "Range": byte_range}
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 206
            deadline = time.monotonic() + 3
            while True:
                with service.db.connect() as conn:
                    rows = conn.execute("SELECT device FROM iphone_downloads").fetchall()
                if bool(rows) == recorded or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            assert [row[0] for row in rows] == ([request["deviceId"]] if recorded else [])
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
