from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_exact_manga_hitboxes_map_parser_tokens_monotonically_to_ocr_surface() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pudge-v0.7.27-manga-exact-surface-map-v1" in js
    assert "function mangaMapTokenSurfaces(stream, surfaces)" in js
    assert "mangaMapTokenSurfaces(stream, tokens.map(token => token?.textContent))" in js
    assert "segmentCharacters.slice(mapping.start, mapping.end)" in js
    assert "mangaTokenCharacterRuns(matched, vertical)" in js
    assert "if (found < 0) found = mangaFindCharacterSequence(stream, surface, 0);" not in js


def test_generated_manga_regions_do_not_proportionally_remap_dictionary_tokens() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    start = js.index("function mangaTokenHitboxes(regionNode, region)")
    end = js.index("function mangaTokenBoxClientRect", start)
    body = js[start:end]
    assert "if (boxes.length || generatedVerticalSource) return boxes;" in body
    assert "return null; // do not invent token geometry" in body
    assert "totalWeight" not in body
    assert "beforeWeight" not in body
    assert "if (segments.length) return [];" in body


def test_clicks_and_debug_still_share_one_hitbox_function_without_grid_fallbacks() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert js.count("mangaTokenHitboxes(regionNode, region)") >= 3
    assert "MANGA_TOKEN_HIT_SLOP_PX = 8" in js
    start = js.index("function mangaTokenHitboxes(regionNode, region)")
    end = js.index("function mangaTokenBoxClientRect", start)
    body = js[start:end]
    assert "vertical-grid-fallback" not in body
    assert "horizontal-region-fallback" not in body
