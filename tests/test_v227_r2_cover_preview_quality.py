from __future__ import annotations

import base64
import io
from pathlib import Path
import time
import zipfile
import http.server
import threading
import urllib.request
from types import SimpleNamespace

from PIL import Image
import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manga import MangaService
from pudge.web_app import WebAppApi, _asset_handler_for_api


def _jpeg(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (80, 120, 160)).save(output, format="JPEG", quality=94)
    return output.getvalue()


def _png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (60, 100, 140)).save(output, format="PNG")
    return output.getvalue()


def test_manga_thumbnail_stays_small_but_preview_is_original_bytes(tmp_path: Path) -> None:
    original = _jpeg(1600, 2400)
    archive = tmp_path / "Volume 1.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("001.jpg", original)
        zf.writestr("002.jpg", _jpeg(1000, 1600))

    db = Database(tmp_path / "library.sqlite3")
    now = time.time()
    with db.connect() as conn:
        book_id = int(
            conn.execute(
                "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING id",
                (str(archive), "Series 1", 2, 0, "rtl", now, now),
            ).fetchone()[0]
        )

    service = MangaService(db, cache_dir=tmp_path / "cache")
    payload = service._payload(service._book(book_id))
    assert payload["cover_source"] == "first_page"
    encoded = str(payload["cover_url"]).split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as thumb:
        assert thumb.size == (320, 480)

    ref = service.cover_ref(book_id, asset_base="http://127.0.0.1:7777")
    assert ref.source_kind == "local_page"
    assert ref.asset_id == f"manga:{book_id}"
    assert ref.preview_url.startswith(f"http://127.0.0.1:7777/api/covers/manga/{book_id}/")
    assert not ref.preview_url.startswith("data:")

    target, media_type, revision = service.cover_preview_asset(book_id, expected_revision=ref.source_revision)
    assert revision == ref.source_revision
    assert media_type == "image/jpeg"
    assert target.parent.name == "manga-cover-previews"
    assert target.read_bytes() == original
    with Image.open(target) as preview:
        assert preview.size == (1600, 2400)
    assert not list(target.parent.glob("*.tmp"))


def test_light_novel_preview_reuses_stored_original_cover_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)

    source = tmp_path / "volume.epub"
    source.write_bytes(b"placeholder")
    original = _png(900, 1350)
    monkeypatch.setattr(
        "pudge.light_novels._epub_metadata",
        lambda _path: ("日本語の本 1", [("1", "これは本文です。")], (original, ".png")),
    )
    book = service.import_file(source)
    stored = str(service.book(int(book["id"]))["cover_url"])
    assert stored.startswith("covers/ln-")
    assert (cfg.library.cover_cache_dir / Path(stored).name).read_bytes() == original

    ref = service.cover_ref(int(book["id"]), asset_base="http://127.0.0.1:8888")
    assert ref.source_kind == "embedded"
    assert ref.preview_url == f"http://127.0.0.1:8888/{stored}"
    assert ref.thumbnail_url == ref.preview_url
    assert not ref.preview_url.startswith("data:")


def test_anilist_literature_uses_official_extralarge_variant_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "test"
    service = LightNovelService(cfg)

    def fake_post(query: str, _variables: dict[str, object]) -> dict[str, object]:
        if "Viewer" in query:
            return {"Viewer": {"id": 1}}
        assert "coverImage{extraLarge large}" in query
        return {
            "MediaListCollection": {
                "lists": [
                    {
                        "entries": [
                            {
                                "status": "CURRENT",
                                "media": {
                                    "id": 42,
                                    "format": "NOVEL",
                                    "title": {"userPreferred": "Book"},
                                    "coverImage": {
                                        "extraLarge": "https://img/extra.jpg",
                                        "large": "https://img/large.jpg",
                                    },
                                },
                            }
                        ]
                    }
                ]
            }
        }

    monkeypatch.setattr(service, "_anilist_post", fake_post)
    rows = service.anilist_literature(force=True)
    assert rows[0]["cover"] == "https://img/extra.jpg"


def test_asset_server_streams_original_manga_preview_with_revision_cache_headers(
    tmp_path: Path,
) -> None:
    revision = "b" * 20
    raw = _jpeg(640, 960)
    target = tmp_path / "original.jpg"
    target.write_bytes(raw)

    class Manga:
        def cover_preview_asset(self, book_id: int, *, expected_revision: str = "") -> tuple[Path, str, str]:
            assert book_id == 9
            assert expected_revision == revision
            return target, "image/jpeg", revision

    class Logger:
        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

    web_root = tmp_path / "web"
    web_root.mkdir()
    api = SimpleNamespace(manga=Manga(), logger=Logger())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _asset_handler_for_api(api, web_root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/covers/manga/9/{revision}", timeout=3
        ) as response:
            assert response.read() == raw
            assert response.headers.get_content_type() == "image/jpeg"
            assert response.headers["ETag"] == f'"{revision}"'
            assert "immutable" in response.headers["Cache-Control"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_web_cover_resolver_returns_typed_contract_and_clamps_dpr() -> None:
    class Manga:
        def cover_ref(self, book_id: int, *, asset_base: str):
            from pudge.cover_assets import CoverRef

            return CoverRef(
                asset_id=f"manga:{book_id}",
                source_revision="a" * 20,
                thumbnail_url="",
                preview_url=f"{asset_base}/preview",
                source_kind="local_page",
            )

    api = object.__new__(WebAppApi)
    api.asset_base = "http://127.0.0.1:1"
    api.manga = Manga()
    result = api.cover_preview_resolve("manga", 7, 10.0, 1200, 800)
    assert result["asset_id"] == "manga:7"
    assert result["source_kind"] == "local_page"
    assert result["requested_dpr"] == 4.0
    assert result["viewport_width"] == 1200
    assert result["variant"] == "original"
    with pytest.raises(ValueError):
        api.cover_preview_resolve("anime", 7)


# Covers on the page (natural size 1600x2400, or 100x50 for "small" sources); the
# high-resolution answers are held until the test answers them.
COVERS = r"""
const MakeImage = window.Image;
window.Image = function Image() {
  const img = MakeImage();
  img.decode = async () => { const small = String(img.src).includes('small');
    img.__naturalWidth = small ? 100 : 1600; img.__naturalHeight = small ? 50 : 2400; };
  return img;
};
window.__pending = new Map();
__api.cover_preview_resolve = (kind, id) => new Promise(resolve => window.__pending.set(Number(id), resolve));
"""


def test_cover_preview_shows_the_latest_cover_in_full_resolution_fitted_to_the_window(page) -> None:
    result = page.run(
        """
        const shelf = document.createElement('div');
        shelf.innerHTML = [1, 2, 3].map(id => `<div class="cover-shell"><img id="cover${id}" src="thumb-${id}.jpg" data-pudge-cover-kind="manga" data-pudge-cover-id="${id}"></div>`).join('');
        document.body.appendChild(shelf);
        for (const img of shelf.querySelectorAll('img')) img.__rect = {x: 10, y: 10, width: 80, height: 120};
        const shown = () => document.querySelector('.pudge-cover-preview img')?.getAttribute('src') ?? null;
        const size = () => { const img = document.querySelector('.pudge-cover-preview img'); return [parseFloat(img.style.width), parseFloat(img.style.height)]; };
        const answer = async (id, url) => { window.__pending.get(id)({preview_url: url, source_kind: 'local_page', source_revision: `r${id}`}); await __tick(20); };
        const zoom = async deltaY => { await __fire('.pudge-cover-preview img', 'wheel', {ctrlKey: true, deltaY, clientX: 650, clientY: 450}, 'WheelEvent');
          return document.querySelector('.pudge-cover-preview img').style.transform; };
        const out = {};
        // Dragging a cover with the mouse opens it (the thumbnail first).
        await __fire('#cover1', 'pointerdown', {pointerId: 1, pointerType: 'mouse', button: 0, clientX: 50, clientY: 50}, 'PointerEvent');
        for (const dx of [20, 80, 160]) await __fire('#cover1', 'pointermove', {pointerId: 1, pointerType: 'mouse', clientX: 50 + dx, clientY: 50}, 'PointerEvent');
        await __fire('#cover1', 'pointerup', {pointerId: 1, pointerType: 'mouse', clientX: 210, clientY: 50}, 'PointerEvent');
        await __tick(20);
        out.dragged = shown();
        // Another cover opens before the first one's full version arrives: the late answer is ignored.
        PudgeCoverPreview.open(document.querySelector('#cover2')); await __tick(20);
        out.second = shown();
        await answer(1, 'high-1.jpg');
        out.lateFirst = shown();
        await answer(2, 'high-2.jpg');
        out.full = shown(); out.fitted = size();
        out.zoomIn = await zoom(-1000); out.zoomOut = await zoom(1000);
        // Closed before its answer arrives: the preview stays closed.
        await __key('Escape'); await __tick(20);
        PudgeCoverPreview.open(document.querySelector('#cover3')); await __tick(20);
        await __key('Escape'); await __tick(20);
        await answer(3, 'high-3.jpg');
        out.closed = PudgeCoverPreview.isOpen();
        // A small original is scaled up to the window too.
        PudgeCoverPreview.open(document.querySelector('#cover1')); await __tick(20);
        await answer(1, 'small-1.jpg');
        out.small = shown(); out.smallSize = size();
        return out;
        """,
        before=COVERS,
    )
    out = result.value
    assert (out["dragged"], out["second"], out["lateFirst"]) == ("thumb-1.jpg", "thumb-2.jpg", "thumb-2.jpg")
    assert out["full"] == "high-2.jpg"
    assert out["fitted"] == [552, 828]  # 1600x2400 in a 1300x900 window, 36 px margins
    assert out["zoomIn"].endswith("scale(6)") and out["zoomOut"].endswith("scale(0.5)")
    assert out["closed"] is False
    assert out["small"] == "small-1.jpg" and out["smallSize"][0] > 100
    # The window asks for the cover by its stable id, with its pixel ratio and size.
    assert result.calls_of("cover_preview_resolve")[:2] == [
        ["manga", 1, 2, 1300, 900],
        ["manga", 2, 2, 1300, 900],
    ]
