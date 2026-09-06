from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_audiobook_series_card_click_selects_or_clears_whole_series():
    js = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert "const audioSeriesBooks = key =>" in js
    assert "const toggleAudioSeriesSelection = books =>" in js
    assert "const allSelected=ids.every(id=>audioSelection.has(id))" in js
    assert "#audiobooksContent .audiobook-series-card[data-audiobook-series]" in js
    assert "if(event.target.closest?.('.audiobook-card[data-audiobook-id]'))return" in js
    assert "toggleAudioSeriesSelection(books)" in js
    assert "series-selected" in js
    assert "series-partial" in js


def test_audiobook_selectable_inner_and_outer_cards_use_pointer_cursor():
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")

    assert ".audiobook-card{position:relative;cursor:pointer}" in css
    assert ".audiobook-series-card{" in css
    series = css.split(".audiobook-series-card{", 1)[1].split("}", 1)[0]
    assert "cursor:pointer" in series
    assert ".audiobook-series-card.series-selected" in css
    assert ".audiobook-series-card.series-partial" in css
