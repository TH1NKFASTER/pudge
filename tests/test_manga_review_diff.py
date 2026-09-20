from __future__ import annotations

import json
from pathlib import Path

from PIL import Image


def _page_payload(regions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "pudge-manga-ocr-review-v1",
        "backend_ocr": {"regions": regions},
        "diagnostics": [],
    }


def _write_review(root: Path, pages: dict[int, list[dict[str, object]]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for page, regions in pages.items():
        (root / f"page_{page:03d}.json").write_text(
            json.dumps(_page_payload(regions), ensure_ascii=False), encoding="utf-8"
        )
        Image.new("RGB", (200, 300), "white").save(root / f"page_{page:03d}.jpg")
        Image.new("RGB", (200, 300), "white").save(root / f"page_{page:03d}_overlay.jpg")


def _region(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {"text": text, "x": x, "y": y, "width": width, "height": height}


def test_compare_review_directories_classifies_text_geometry_added_and_removed(tmp_path: Path) -> None:
    from pudge.manga_review_diff import compare_review_directories

    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_review(
        old,
        {
            3: [
                _region("そのまま", 0.10, 0.10, 0.20, 0.10),
                _region("旧テキスト", 0.40, 0.10, 0.20, 0.10),
                _region("移動", 0.10, 0.40, 0.20, 0.10),
                _region("削除", 0.70, 0.70, 0.10, 0.10),
            ]
        },
    )
    _write_review(
        new,
        {
            3: [
                _region("そのまま", 0.10, 0.10, 0.20, 0.10),
                _region("新テキスト", 0.40, 0.10, 0.20, 0.10),
                _region("移動", 0.115, 0.40, 0.20, 0.10),
                _region("追加", 0.70, 0.20, 0.10, 0.10),
            ]
        },
    )

    report = compare_review_directories(old, new)
    assert report["schema"] == "pudge_manga_review_diff/v1"
    assert report["summary"] == {
        "pages": 1,
        "unchanged": 1,
        "text_changed": 1,
        "geometry_changed": 1,
        "added": 1,
        "removed": 1,
    }
    page = report["pages"][0]
    assert page["page"] == 3
    assert [item["kind"] for item in page["changes"]] == [
        "text_changed",
        "geometry_changed",
        "removed",
        "added",
    ]
    assert page["changes"][0]["old"]["text"] == "旧テキスト"
    assert page["changes"][0]["new"]["text"] == "新テキスト"


def test_write_review_diff_report_emits_json_markdown_html_and_changed_crops(tmp_path: Path) -> None:
    from pudge.manga_review_diff import write_review_diff_report

    old = tmp_path / "old"
    new = tmp_path / "new"
    output = tmp_path / "report"
    _write_review(old, {7: [_region("旧", 0.10, 0.10, 0.20, 0.20)]})
    _write_review(new, {7: [_region("新", 0.10, 0.10, 0.20, 0.20)]})

    report = write_review_diff_report(old, new, output)

    assert report["summary"]["text_changed"] == 1
    assert (output / "report.json").is_file()
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "page 007" in markdown
    assert "旧" in markdown and "新" in markdown
    html = (output / "index.html").read_text(encoding="utf-8")
    assert "page_007_overlay.jpg" in html
    crops = sorted((output / "crops").glob("*.jpg"))
    assert len(crops) == 2
