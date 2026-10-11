from __future__ import annotations

import subprocess
import threading

import pytest

from pudge.database import Database
from media_tools import requires_ffmpeg


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _book(service, tmp_path, name="one.wav", seconds=1):
    source = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=duration={seconds}", str(source)], check=True
    )
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,0,0)",
            (str(source), "Test", seconds),
        ).lastrowid
        conn.execute(
            'INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,"Test",?,0,?)',
            (book, str(source), seconds, seconds),
        )
    return book, source


@requires_ffmpeg
def test_range_requests_do_not_rehash_unchanged_audio(service, tmp_path, monkeypatch):
    import pudge.iphone_service as module

    _book(service, tmp_path)
    service.refresh_catalog()
    part = service.catalog()["items"][0]["parts"][0]
    calls = []
    real = module.digest
    monkeypatch.setattr(module, "digest", lambda path: calls.append(path) or real(path))
    for _ in range(5):
        service.resource(part["resourceId"], part["sha256"])
    assert calls == []


@requires_ffmpeg
def test_changed_original_is_rehashed_and_rejected(service, tmp_path):
    _, source = _book(service, tmp_path)
    service.refresh_catalog()
    part = service.catalog()["items"][0]["parts"][0]
    data = bytearray(source.read_bytes())
    data[-1] ^= 0xFF
    source.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        service.resource(part["resourceId"], part["sha256"])


@requires_ffmpeg
def test_second_catalog_refresh_reuses_prepared_audio(service, tmp_path, monkeypatch):
    import pudge.iphone_audio as audio

    _book(service, tmp_path)
    service.refresh_catalog()
    first = service.catalog()["items"][0]
    monkeypatch.setattr(
        audio, "prepare_audio", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-prepared"))
    )
    assert service.refresh_catalog()["errors"] == []
    assert service.catalog()["items"][0] == first


@requires_ffmpeg
def test_slow_preparation_does_not_block_catalog_request(service, tmp_path, monkeypatch):
    _book(service, tmp_path)
    release = threading.Event()
    real = service._prepared_audio

    def slow(source):
        release.wait(10)
        return real(source)

    monkeypatch.setattr(service, "_prepared_audio", slow)
    result = service.refresh_catalog(wait_seconds=0.05)
    assert result["preparing"] is True
    assert service.catalog()["items"] == []
    release.set()
    assert service.refresh_catalog(wait_seconds=10)["preparing"] is False
    assert len(service.catalog()["items"]) == 1


@requires_ffmpeg
def test_seek_on_stopped_mac_becomes_shared_position(service, tmp_path):
    book, _ = _book(service, tmp_path, seconds=3)
    service.refresh_catalog()
    asset = service.asset_for_book(book)
    assert service.record_mac_seek(book, 2.0) is True
    assert service.progress(asset)["selected"]["positionMs"] == 2000
    with service.db.connect() as conn:
        assert conn.execute("SELECT position FROM audiobooks WHERE id=?", (book,)).fetchone()[0] == 2.0


def test_iphone_data_lives_beside_library_database_not_config(tmp_path, monkeypatch):
    # Archived phone audio can be gigabytes: it goes to the data folder (beside the
    # library database), never into the config folder.
    from types import SimpleNamespace

    import pudge.iphone_http as iphone_http
    from pudge.config import AppConfig, write_config
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config" / "config.toml"
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "data" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    server = SimpleNamespace(
        server_port=8766,
        shutdown=lambda: None,
        server_close=lambda: None,
        wait_for_requests=lambda timeout=5: True,
    )
    monkeypatch.setattr(iphone_http, "start_iphone_server", lambda service: (server, None, "0" * 64))
    api = WebAppApi(cfg.config_path)
    try:
        assert api.iphone_enable()["enabled"] is True
        root = api._iphone_service.root
        assert root == (tmp_path / "data" / "iphone").resolve() and root.is_dir()
        assert not [path for path in (tmp_path / "config").rglob("*") if "iphone" in path.name.lower()]
        assert api._iphone_service.cover_dirs == [tmp_path / "data" / "covers"]
    finally:
        api.close()


@requires_ffmpeg
def test_book_deleted_on_mac_leaves_the_catalog(service, tmp_path):
    book, _ = _book(service, tmp_path)
    service.refresh_catalog()
    assert len(service.catalog()["items"]) == 1
    with service.db.connect() as conn:
        conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book,))
        conn.execute("DELETE FROM audiobooks WHERE id=?", (book,))
    service.refresh_catalog()
    assert service.catalog()["items"] == []


def _image(path, size="1200x1800", color="red"):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )


@requires_ffmpeg
def test_catalog_cover_is_the_mac_cover_in_medium_size(service, tmp_path):
    from types import SimpleNamespace

    covers = tmp_path / "covers"
    covers.mkdir()
    _image(covers / "ln-1.png", color="blue")
    book, source = _book(service, tmp_path)
    service.cover_dirs = [covers]
    service.audiobooks = SimpleNamespace(
        book=lambda _id: {"cover_url": "covers/ln-1.png"},
        _embedded_cover_paths=lambda _p: None,
        cover_cache_dir=tmp_path / "none",
    )
    service.refresh_catalog()
    cover = service.catalog()["items"][0]["cover"]
    assert cover["fileName"] == "cover.jpg"
    path = service.resource(cover["resourceId"], cover["sha256"])
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert probe == "600,900"


@requires_ffmpeg
def test_missing_cover_never_breaks_the_catalog(service, tmp_path):
    from types import SimpleNamespace

    _book(service, tmp_path)
    service.audiobooks = SimpleNamespace(
        book=lambda _id: {"cover_url": "https://127.0.0.1:9/none.jpg"}, _embedded_cover_paths=lambda _p: None
    )
    service.refresh_catalog()
    item = service.catalog()["items"][0]
    assert "cover" not in item


@requires_ffmpeg
def test_catalog_carries_mac_series_grouping(service, tmp_path):
    from types import SimpleNamespace

    _book(service, tmp_path)
    service.audiobooks = SimpleNamespace(
        book=lambda _id, **_k: {"series_title": "狼と香辛料", "volume": 2},
        _embedded_cover_paths=lambda _p: None,
    )
    service.refresh_catalog()
    item = service.catalog()["items"][0]
    assert item["seriesTitle"] == "狼と香辛料" and item["volume"] == 2
