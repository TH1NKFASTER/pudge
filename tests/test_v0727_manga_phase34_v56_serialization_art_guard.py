from __future__ import annotations

import json
import sys
import types
import zipfile
from pathlib import Path

from PIL import Image

import pudge.manga_ocr_worker as worker


def _false_kana(text: str) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "japanese-multicolumn-geometry",
        "x": 0.10,
        "y": 0.10,
        "width": 0.03,
        "height": 0.022,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [{"text": "", "orientation": "horizontal"}],
    }


def _real_region() -> dict[str, object]:
    return {
        "text": "本物",
        "raw_text": "本物",
        "orientation": "horizontal",
        "orientation_reason": "",
        "x": 0.20,
        "y": 0.20,
        "width": 0.10,
        "height": 0.05,
        "confidence": 0.90,
        "detector": "vision-original",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {"text": "本", "orientation": "horizontal"},
            {"text": "物", "orientation": "horizontal"},
        ],
    }


class _FakeMangaOcr:
    pass


def test_v56_finalizer_drops_v55_trace_candidates() -> None:
    regions = [_false_kana("はっ"), _real_region(), _false_kana("クッ")]

    out = worker._finalize_worker_output_regions(regions)

    assert [item["text"] for item in out] == ["本物"]


def test_v56_regions_entrypoint_filters_again_at_serialization_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    fake_module = types.ModuleType("manga_ocr")
    fake_module.MangaOcr = _FakeMangaOcr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "manga_ocr", fake_module)

    monkeypatch.setattr(
        worker,
        "_recognize_regions",
        lambda model, image, regions: [
            _false_kana("はっ"),
            _real_region(),
            _false_kana("クッ"),
        ],
    )

    image_path = tmp_path / "page.png"
    Image.new("RGB", (32, 32), "white").save(image_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"regions": []}), encoding="utf-8")
    output = tmp_path / "out.json"

    assert worker._regions(image_path, manifest, output) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [item["text"] for item in payload["regions"]] == ["本物"]


def test_v56_batch_entrypoint_filters_again_at_serialization_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    fake_module = types.ModuleType("manga_ocr")
    fake_module.MangaOcr = _FakeMangaOcr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "manga_ocr", fake_module)

    monkeypatch.setattr(
        worker,
        "_recognize_regions",
        lambda model, image, regions: [
            _false_kana("はっ"),
            _real_region(),
            _false_kana("クッ"),
        ],
    )

    archive = tmp_path / "book.cbz"
    page = tmp_path / "page.png"
    Image.new("RGB", (32, 32), "white").save(page)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(page, "page.png")

    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "archive": str(archive),
                "pages": [{"page_index": 0, "name": "page.png", "regions": []}],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "rows.jsonl"
    progress = tmp_path / "progress.json"

    assert worker._batch(manifest, output, progress) == 0
    row = json.loads(output.read_text(encoding="utf-8").strip())

    assert [item["text"] for item in row["regions"]] == ["本物"]
    assert row["text"] == "本物"
