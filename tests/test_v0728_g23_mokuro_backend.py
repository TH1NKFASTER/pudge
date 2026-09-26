from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.config import load_config, write_config
from pudge.database import Database
from pudge.manga import MangaService


def _png(path: Path, size=(120, 160)) -> bytes:
    image = Image.new("RGB", size, "white")
    image.save(path, format="PNG")
    return path.read_bytes()


def _book(tmp_path: Path) -> tuple[MangaService, int, Path]:
    db = Database(tmp_path / "pudge.db")
    archive = tmp_path / "book.cbz"
    image_path = tmp_path / "01.png"
    data = _png(image_path)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("01.png", data)
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book = service.import_file(archive)
    return service, int(book["id"]), archive


def _fake_mokuro_python(tmp_path: Path) -> Path:
    path = tmp_path / "fake-mokuro-python"
    path.write_text(
        """#!/usr/bin/env python3
import json, sys
from pathlib import Path
volume = Path(sys.argv[-1])
images = sorted(p for p in volume.rglob('*') if p.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.avif'})
pages=[]
for image in images:
    pages.append({
      'img_path': image.relative_to(volume).as_posix(),
      'img_width': 120, 'img_height': 160,
      'blocks': [{'box':[10,20,80,120],'vertical':True,'font_size':20,'lines':['日本語'],'lines_coords':[[10,20,80,120]]}],
    })
out={'version':'0.2.5','title':'fixture','title_uuid':'t','volume':'volume','volume_uuid':'v','pages':pages}
(volume.parent / 'volume.mokuro').write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_mokuro_backend_runs_only_in_private_staging_and_publishes(tmp_path: Path) -> None:
    service, book_id, archive = _book(tmp_path)
    fake = _fake_mokuro_python(tmp_path)
    progress: list[tuple[int, int, str]] = []

    result = service.ocr_book_mokuro(
        book_id,
        mokuro_python=fake,
        progress=lambda done, total, _page, phase: progress.append((done, total, phase)),
    )

    assert result["ok"] is True
    assert result["backend"] == "mokuro"
    assert result["complete"] is True
    assert archive.is_file()
    assert not archive.with_suffix("").exists(), "Mokuro must not unpack next to the user's archive"
    assert any(phase == "preparing" for _done, _total, phase in progress)
    assert any(phase == "publishing" for _done, _total, phase in progress)
    artifact = service.ocr_artifact(book_id)
    assert artifact["pages"][0]["regions"][0]["provenance"]["source"] == "mokuro"
    assert not list((tmp_path / "cache" / "manga-ocr" / "mokuro-runs").glob("*"))


def test_mokuro_backend_requires_managed_python(tmp_path: Path) -> None:
    service, book_id, _archive = _book(tmp_path)
    missing = tmp_path / "does-not-exist"
    try:
        service.ocr_book_mokuro(book_id, mokuro_python=missing)
    except RuntimeError as exc:
        assert "Mokuro OCR backend is not installed" in str(exc)
    else:
        raise AssertionError("missing Mokuro backend must fail")


def test_manga_ocr_backend_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    assert cfg.tools.manga_ocr_backend == "pudge"
    cfg.tools.manga_ocr_backend = "mokuro"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.tools.manga_ocr_backend == "mokuro"
    text = path.read_text(encoding="utf-8")
    assert 'manga_ocr_backend = "mokuro"' in text


def test_g23_settings_and_backend_dispatch_are_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    index = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (root / "pudge/web/media.js").read_text(encoding="utf-8")
    app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    manga = (root / "pudge/manga.py").read_text(encoding="utf-8")

    assert 'id="s_manga_ocr_backend"' in index
    assert "manga_ocr_backend:v('s_manga_ocr_backend')||'pudge'" in index
    assert '"manga_ocr_backend": cfg.tools.manga_ocr_backend' in app
    assert 'if self._manga_ocr_backend() == "mokuro"' in app
    assert "ocr_book_mokuro(" in app
    assert '"mokuro==0.2.5"' in app
    assert "legacy_html=False" in manga
    assert "disable_confirmation=True" in manga
    assert "Install Mokuro" in media
