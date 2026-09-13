from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
JS = ROOT / "pudge/web/manga_reader_v2.js"


def test_dark_shape_filter_preserves_exact_hitbox_contract() -> None:
    js = JS.read_text(encoding="utf-8")
    assert "pudge-v0.7.27-manga-dark-shape-hitboxes-v2" in js
    assert "function mangaTokenHitboxes(regionNode, region)" in js
    assert "function mangaTokenHitboxes(regionNode, region, frame" not in js
    assert js.count("mangaTokenHitboxes(regionNode, region)") >= 3
    assert "function mangaDarkSupportForHitbox(image, box)" in js
    assert "function mangaFinalizeTokenHitboxes(regionNode, region, boxes)" in js
    assert "support.dark >= .45 && support.light >= .01" in js
    assert "String(region?.source || '') !== 'dark-block-proposal'" in js
    assert "const filteredBoxes = mangaFinalizeTokenHitboxes(regionNode, region, boxes);" in js


def test_dark_shape_filter_does_not_restore_rejected_white_cutout_boxes() -> None:
    js = JS.read_text(encoding="utf-8")
    start = js.index("function mangaFinalizeTokenHitboxes")
    end = js.index("function mangaTokenHitboxes", start)
    block = js[start:end]
    assert "return boxes.filter" in block
    assert "filtered.length ? filtered : boxes" not in block
    assert "support == null ||" in block


def test_dark_shape_filter_runs_before_strict_surface_return() -> None:
    js = JS.read_text(encoding="utf-8")
    start = js.index("function mangaTokenHitboxes(regionNode, region)")
    end = js.index("function mangaTokenBoxClientRect", start)
    body = js[start:end]
    legacy = "if (boxes.length || generatedVerticalSource) return boxes;"
    assert legacy in body
    assert "const filteredBoxes = mangaFinalizeTokenHitboxes(regionNode, region, boxes);" in body
    assert body.index("const filteredBoxes") < body.index(legacy)
