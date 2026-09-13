from __future__ import annotations

from pathlib import Path

from pudge.manga_ocr_artifact import MANGA_OCR_ARTIFACT_SCHEMA, normalize_region

ROOT = Path(__file__).parents[1]


def test_artifact_v3_preserves_alignment_diagnostics_and_marks_unlabeled_geometry_unknown() -> None:
    region = normalize_region(
        {
            "text": "海",
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
            "geometry_source": "vision-accurate-aligned-v4+mangaocr-line-v1",
            "line_ocr_text": "海",
            "line_ocr_alignment": {"cost": 0.2, "ops": ["match"]},
            "line_ocr_cost": 0.2,
            "detector_geometry": {"x": 0.09, "y": 0.19, "width": 0.31, "height": 0.41},
            "segments": [{"text": "", "x": 0.1, "y": 0.2, "width": 0.1, "height": 0.1}],
        },
        page_index=4,
        order=2,
    )
    assert MANGA_OCR_ARTIFACT_SCHEMA == "pudge-manga-ocr-v3"
    assert region["geometry_status"] == "unknown"
    assert region["word_geometry"] == "unlabeled_segments"
    assert region["geometry_source"] == "vision-accurate-aligned-v4+mangaocr-line-v1"
    assert region["line_ocr_alignment"] == {"cost": 0.2, "ops": ["match"]}
    assert region["detector_geometry"]["x"] == 0.09


def test_artifact_does_not_call_synthetic_grid_observed_geometry() -> None:
    region = normalize_region(
        {
            "text": "小さな",
            "x": 0.1,
            "y": 0.2,
            "width": 0.1,
            "height": 0.3,
            "segments": [
                {"text": "小", "x": 0.1, "y": 0.4, "width": 0.05, "height": 0.05, "source": "ink-grid-v1"},
                {"text": "さ", "x": 0.1, "y": 0.35, "width": 0.05, "height": 0.05, "source": "ink-grid-v1"},
            ],
        },
        page_index=1,
        order=0,
    )
    assert region["geometry_status"] == "synthetic"


def test_frontend_mapping_is_monotonic_and_has_no_reset_to_zero_or_word_grids() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pudge-manga-recovery-hitbox-contract-v2" in js
    assert "function mangaMapTokenSurfaces(stream, surfaces)" in js
    assert "if (found < 0) found = mangaFindCharacterSequence(stream, surface, 0);" not in js
    start = js.index("function mangaTokenHitboxes(regionNode, region)")
    end = js.index("function mangaTokenBoxClientRect", start)
    body = js[start:end]
    assert "vertical-grid-fallback" not in body
    assert "horizontal-region-fallback" not in body
    assert "if (segments.length) return [];" in body
    assert "region-single-token" in body


def test_debug_hitboxes_export_geometry_status() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "core.dataset.geometryStatus = mangaGeometryStatus" in js
