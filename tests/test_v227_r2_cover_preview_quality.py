from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import subprocess
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


ROOT = Path(__file__).resolve().parents[1]
COVER_JS = ROOT / "pudge" / "web" / "cover_preview.js"


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

    target, media_type, revision = service.cover_preview_asset(
        book_id, expected_revision=ref.source_revision
    )
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
        def cover_preview_asset(
            self, book_id: int, *, expected_revision: str = ""
        ) -> tuple[Path, str, str]:
            assert book_id == 9
            assert expected_revision == revision
            return target, "image/jpeg", revision

    class Logger:
        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

    web_root = tmp_path / "web"
    web_root.mkdir()
    api = SimpleNamespace(manga=Manga(), logger=Logger())
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _asset_handler_for_api(api, web_root)
    )
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


def _preview_race_fixture() -> dict[str, object]:
    source = COVER_JS.read_text(encoding="utf-8")
    script = f"""
const vm=require('vm');
const source={json.dumps(source)};
let overlay=null;
const pending=new Map();
const classList=()=>({{add(){{}},remove(){{}},toggle(){{}},contains(){{return false;}}}});
class Button{{constructor(kind){{this.kind=kind;this.hidden=kind==='retry';this.isConnected=true;}}focus(){{document.activeElement=this;}}closest(selector){{return selector.includes(this.kind)?this:null;}}}}
class PreviewImage{{
  constructor(){{this.style={{setProperty(k,v){{this[k]=v;}}}};this.dataset={{}};this.classList=classList();this.naturalWidth=80;this.naturalHeight=120;this.complete=true;this._src='';this.isConnected=true;}}
  set src(value){{this._src=String(value);}}
  get src(){{return this._src;}}
  get currentSrc(){{return this._src;}}
  get offsetWidth(){{return parseFloat(this.style.width)||80;}}
  get offsetHeight(){{return parseFloat(this.style.height)||120;}}
  addEventListener(){{}}
  releasePointerCapture(){{}}
  getBoundingClientRect(){{const w=this.offsetWidth,h=this.offsetHeight;return {{left:600-w/2,right:600+w/2,top:400-h/2,bottom:400+h/2,width:w,height:h}};}}
}}
class Overlay{{
  constructor(){{this.isConnected=true;this.classList=classList();this.close=new Button('close');this.retry=new Button('retry');this.image=new PreviewImage();this.listeners={{}};}}
  setAttribute(){{}}
  set innerHTML(value){{const m=String(value).match(/<img[^>]+src=\"([^\"]*)\"/);if(m)this.image.src=m[1].replaceAll('&amp;','&').replaceAll('&quot;','\"');}}
  querySelector(selector){{if(selector==='img'||selector.includes('preview img'))return this.image;if(selector.includes('retry'))return this.retry;if(selector.includes('close'))return this.close;return null;}}
  querySelectorAll(selector){{return selector==='button'?[this.close,this.retry]:[];}}
  addEventListener(type,fn){{this.listeners[type]=fn;}}
  remove(){{this.isConnected=false;if(overlay===this)overlay=null;}}
}}
class SourceImage{{
  constructor(id,fallback){{this.dataset={{pudgeCoverKind:'manga',pudgeCoverId:String(id)}};this.src=fallback;this.currentSrc=fallback;this.isConnected=true;}}
  closest(){{return this;}}
  focus(){{}}
}}
class Candidate{{
  constructor(){{this.naturalWidth=0;this.naturalHeight=0;this.complete=true;this._src='';}}
  set src(value){{this._src=String(value);this.naturalWidth=this._src.includes('small')?100:1600;this.naturalHeight=this._src.includes('small')?50:2400;}}
  get src(){{return this._src;}}
  async decode(){{await Promise.resolve();}}
}}
global.Image=Candidate;
const windowTarget=new EventTarget();global.window=windowTarget;window.innerWidth=1200;window.innerHeight=800;window.devicePixelRatio=2;
window.pywebview={{api:{{cover_preview_resolve:(kind,id)=>new Promise(resolve=>pending.set(Number(id),resolve))}}}};
global.pywebview=window.pywebview;
global.document={{
  body:{{appendChild(node){{overlay=node;node.isConnected=true;}}}},activeElement:null,
  addEventListener(){{}},querySelector(selector){{if(!overlay)return null;if(selector==='.pudge-cover-preview')return overlay;if(selector==='.pudge-cover-preview img')return overlay.image;return null;}},
  createElement(tag){{if(tag==='div')return new Overlay();if(tag==='img')return new PreviewImage();throw new Error(tag);}}
}};
global.requestAnimationFrame=fn=>{{setImmediate(fn);return 1;}};global.cancelAnimationFrame=()=>{{}};
vm.runInThisContext(source);
const tick=async()=>{{await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));await Promise.resolve();}};
(async()=>{{
  const result={{}};
  window.PudgeCoverPreview.open(new SourceImage(1,'thumb-A'));
  window.PudgeCoverPreview.open(new SourceImage(2,'thumb-B'));
  pending.get(1)({{preview_url:'high-A',source_kind:'local_page',source_revision:'a'}});await tick();
  result.lateA=overlay?.image?.src;
  pending.get(2)({{preview_url:'high-B',source_kind:'local_page',source_revision:'b'}});await tick();
  result.bApplied=overlay?.image?.src;
  result.fitWidth=parseFloat(overlay?.image?.style?.width||'0');
  window.PudgeCoverPreview.zoom(10);result.maxZoom=overlay?.image?.style?.transform||'';
  window.PudgeCoverPreview.zoom(.1);result.minZoom=overlay?.image?.style?.transform||'';
  window.PudgeCoverPreview.open(new SourceImage(3,'thumb-C'));
  window.PudgeCoverPreview.close();
  pending.get(3)({{preview_url:'high-C',source_kind:'local_page',source_revision:'c'}});await tick();
  result.closedStaysClosed=overlay===null;
  window.PudgeCoverPreview.openRef({{preview_url:'small-source',thumbnail_url:'small-thumb',source_kind:'remote',source_revision:'d'}},'small-thumb');await tick();
  result.smallUpscaled=parseFloat(overlay?.image?.style?.width||'0')>100;
  process.stdout.write(JSON.stringify(result));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    return json.loads(subprocess.check_output(["node", "-e", script], text=True))


def test_cover_preview_generation_guards_late_responses_and_fits_sources() -> None:
    result = _preview_race_fixture()
    assert result["lateA"] == "thumb-B"
    assert result["bApplied"] == "high-B"
    assert float(result["fitWidth"]) > 0
    assert "scale(6)" in str(result["maxZoom"])
    assert "scale(0.5)" in str(result["minZoom"])
    assert result["closedStaysClosed"] is True
    assert result["smallUpscaled"] is True
