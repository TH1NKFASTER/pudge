from __future__ import annotations

from pathlib import Path

from pudge.language import has_japanese_marker, is_japanese_subtitle


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_watch_order_hover_list_stays_inside_card_cover() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert ".ready-sequence-list { position:absolute; z-index:7; inset:6px;" in html
    assert ".ready-sequence-count-wrap:hover + .ready-sequence-list" in html
    assert '<div class="ready-sequence-list">${rows}</div>' in html
    assert 'data-action="ready-sequence-previous"' in html
    assert 'data-action="ready-sequence-next"' in html
    assert "right:-6px; width:min(300px" not in html


def test_nyaa_release_download_button_never_requires_horizontal_scroll() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html[html.index("async function openRelease"):html.index("function closeModal")]

    assert 'class="release-list"' in source
    assert 'class="release-row"' in source
    assert 'class="release-download-button"' in source
    assert "<table>" not in source
    assert ".release-row { display:grid; grid-template-columns:54px minmax(0,1fr) auto;" in html
    assert ".release-main { min-width:0; }" in html


def test_jimaku_dot_ja_bracket_marker_is_recognized(tmp_path: Path) -> None:
    name = "僕のヒーローアカデミア.SP.I.am.a.hero.too.WEBRip.DMMTV.ja[cc].srt"
    subtitle = tmp_path / name
    # Deliberately too little Japanese text for content-based detection. The
    # explicit Jimaku language marker must still make this a valid JP subtitle.
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nあ\n",
        encoding="utf-8",
    )

    assert has_japanese_marker(name)
    assert is_japanese_subtitle(subtitle)
