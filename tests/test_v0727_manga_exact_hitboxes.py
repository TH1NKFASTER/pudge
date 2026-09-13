from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_manga_debug_words_use_same_exact_boxes_as_click_hit_testing() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pudge-v0.7.27-manga-exact-hitboxes-v2" in js
    assert "function mangaTokenHitboxes(regionNode, region)" in js
    assert "const boxes = mangaTokenHitboxes(regionNode, region);" in js
    assert js.count("mangaTokenHitboxes(regionNode, region)") >= 3
    assert "MANGA_TOKEN_HIT_SLOP_PX = 8" in js
    assert "manga-v2-debug-token-core" in js
    assert "manga-v2-debug-token-slop" in js


def test_manga_debug_words_no_longer_style_dom_words_as_fake_hitboxes() -> None:
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    assert "pudge-v0.7.27-manga-exact-hitboxes-v2" in css
    marker = css.index("pudge-v0.7.27-manga-exact-hitboxes-v2")
    tail = css[marker:]
    assert ".manga-v2-debug-token-core" in tail
    assert ".manga-v2-debug-token-slop" in tail
    assert "outline:none!important" in tail
    assert "background:transparent!important" in tail


def test_exact_hitboxes_preserve_backend_geometry_without_hidden_word_grids() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    start = js.index("function mangaTokenHitboxes(regionNode, region)")
    end = js.index("function mangaTokenBoxClientRect", start)
    body = js[start:end]
    assert "segment-surface-map" in body
    assert "vertical-grid-fallback" not in body
    assert "horizontal-region-fallback" not in body
    assert "region-single-token" in body


def test_generated_vertical_regions_never_get_fake_grid_hitboxes() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "expanded-vision-rectangle" in js
    assert "expanded-vertical-seed" in js
    assert "dark-block-proposal" in js
    assert "return null; // do not invent token geometry" in js
    assert "generatedVerticalSource && segments.length <= 1" in js
