from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import MokuroImportError, convert_mokuro


def page(name: str, *, blocks: list[dict] | None = None, width: int = 100, height: int = 200) -> dict:
    return {
        "img_path": name, "img_width": width, "img_height": height,
        "blocks": blocks if blocks is not None else [
            {"box": [10, 20, 40, 80], "vertical": True, "lines": ["日本", "語"]},
        ],
    }


def test_img_path_is_authoritative_not_position() -> None:
    rows = convert_mokuro(
        {"pages": [page("02.png"), page("01.png", blocks=[])]},
        ["01.png", "02.png"],
    )
    assert rows[0] == []
    assert rows[1][0]["text"] == "日本語"
    assert rows[1][0]["orientation"] == "vertical"
    assert rows[1][0]["y"] == 0.6
    assert rows[1][0]["height"] == 0.3
    assert rows[1][0]["geometry_status"] == "approximate"
    assert "segments" not in rows[1][0]  # No fabricated character positions.


def test_line_coordinates_are_line_level_not_character_boxes() -> None:
    item = page("02.png", blocks=[{
        "box": [10, 20, 40, 80], "vertical": False,
        "lines": ["test", "123"],
        "lines_coords": [
            [[10, 20], [30, 20], [30, 35], [10, 35]],
            [[10, 40], [35, 40], [35, 65], [10, 65]],
        ],
    }])
    region = convert_mokuro({"pages": [item]}, ["02.png"])[0][0]
    assert region["text"] == "test 123"
    assert "segments" not in region
    assert [segment["text"] for segment in region["provenance"]["line_boxes"]] == ["test", "123"]
    assert region["provenance"]["line_boxes"][0]["y"] == 0.825


@pytest.mark.parametrize("bad", [
    {"pages": [page("missing.png")]},
    {"pages": [page("01.png"), page("01.png")]},
    {"pages": [page("01.png", blocks=[{"box": [0, 0, 101, 5], "vertical": True, "lines": ["X"]}])]},
    {"pages": [page("01.png", blocks=[{"box": [0, 0, 2, 5], "vertical": True, "lines": "X"}])]},
    {"pages": [page("01.png", blocks=[{"box": [0, 0, 2, 5], "lines": ["X"]}])]},
    {"pages": [page("../01.png")]},
    {"pages": [page("01.png", width=101)]},
    {"pages": []},
    {"pages": "not a list"},
])
def test_invalid_mokuro_fails_before_publication(bad: dict) -> None:
    with pytest.raises(MokuroImportError):
        convert_mokuro(bad, ["01.png"], image_sizes={0: (100, 200)})


def test_basename_fallback_requires_unambiguous_member() -> None:
    with pytest.raises(MokuroImportError, match="uniquely"):
        convert_mokuro({"pages": [page("01.png")]}, ["first/01.png", "second/01.png"])
    assert list(convert_mokuro({"pages": [page("./vol/01.png")]}, ["01.png"])) == [0]


def fixture(tmp_path: Path) -> tuple[MangaService, int, Path]:
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        for number in (1, 2):
            image = tmp_path / f"{number:02}.png"
            Image.new("RGB", (100, 200), "white").save(image)
            cbz.write(image, arcname=f"{number:02}.png")
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book = service.import_file(archive)
    return service, book["id"], tmp_path / "book.mokuro"


def test_import_is_visible_to_reader_and_jiten_without_running_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, book_id, sidecar = fixture(tmp_path)
    sidecar.write_text(json.dumps({"pages": [page("02.png"), page("01.png", blocks=[])]}), encoding="utf-8")
    monkeypatch.setattr(service, "_vision_text_regions", lambda _image: pytest.fail("OCR must not run on import"))
    result = service.import_mokuro(book_id, sidecar)
    assert result["complete"] and result["imported_pages"] == 2
    assert service.ocr_cache_status(book_id)["completed_pages"] == 2
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "日本語"
    assert service.text_regions(book_id, 0, cached_only=True)["status"] == "empty_verified"
    assert (1, "日本語") in service.cached_region_texts(book_id)
    artifact = service.ocr_artifact(book_id)
    assert artifact["source"]["fingerprint"]
    assert artifact["pages"][1]["regions"][0]["detector"] == "mokuro-import"
    rerun = service.import_mokuro(book_id, sidecar)
    assert rerun["imported_pages"] == 0 and rerun["skipped_pages"] == 2


def test_mismatched_image_rejects_entire_import_without_cache_changes(tmp_path: Path) -> None:
    service, book_id, sidecar = fixture(tmp_path)
    sidecar.write_text(json.dumps({"pages": [page("01.png"), page("02.png", width=99)]}), encoding="utf-8")
    with pytest.raises(MokuroImportError, match="dimensions differ"):
        service.import_mokuro(book_id, sidecar)
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    assert service.cached_region_texts(book_id) == []


def test_import_does_not_replace_existing_native_ocr(tmp_path: Path) -> None:
    service, book_id, sidecar = fixture(tmp_path)
    sidecar.write_text(json.dumps({"pages": [page("01.png"), page("02.png")]}), encoding="utf-8")
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id, source_fingerprint=fingerprint, generation=generation,
        updates=[(0, [{"text": "Native OCR", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False)],
    )
    result = service.import_mokuro(book_id, sidecar)
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "Native OCR"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "日本語"


def test_missing_pages_are_not_marked_complete(tmp_path: Path) -> None:
    service, book_id, sidecar = fixture(tmp_path)
    sidecar.write_text(json.dumps({"pages": [page("01.png")]}), encoding="utf-8")
    status = service.import_mokuro(book_id, sidecar)
    assert status["imported_pages"] == 1
    assert status["completed_pages"] == 1 and not status["complete"]
    assert service.text_regions(book_id, 1, cached_only=True)["status"] == "missing"


def test_reader_has_native_import_action() -> None:
    source = (Path(__file__).resolve().parents[1] / "pudge/web/manga_reader_v2.js").read_text()
    assert 'data-manga-v2-action="import-mokuro"' in source
    assert "API().choose_manga_mokuro(bookId)" in source
    assert "textRegionCache.clear(); textRegionResultCache.clear();" in source


def test_mokuro_line_geometry_survives_cache_artifact_and_reader(tmp_path: Path) -> None:
    service, book_id, sidecar = fixture(tmp_path)
    line_block = {"box": [10, 20, 60, 120], "vertical": True,
                  "lines": ["日本", "語"], "lines_coords": [
                      [[10, 20], [40, 20], [40, 65], [10, 65]],
                      [[10, 75], [40, 75], [40, 120], [10, 120]],
                  ]}
    sidecar.write_text(json.dumps({"pages": [page("01.png", blocks=[line_block]), page("02.png", blocks=[])]}), encoding="utf-8")
    assert service.import_mokuro(book_id, sidecar)["complete"]
    cached = service.text_regions(book_id, 0, cached_only=True)["regions"][0]
    artifact = service.ocr_artifact(book_id)["pages"][0]["regions"][0]
    for region in (cached, artifact):
        assert region["geometry_status"] == "approximate"
        assert region["provenance"]["source"] == "mokuro"
        assert [line["text"] for line in region["provenance"]["line_boxes"]] == ["日本", "語"]
        assert all(line["width"] > 0 and line["height"] > 0 for line in region["provenance"]["line_boxes"])
        assert "segments" not in region  # line polygons must not be misrepresented as word boxes
