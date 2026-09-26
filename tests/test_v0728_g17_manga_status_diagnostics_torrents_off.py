from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_confirmed_off_is_presented_as_torrents_off() -> None:
    source = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "live.transition==='off_confirmed'?'':" in source
    assert "button.innerHTML=`<span>${title}</span>${detail?`<small>${detail}</small>`:''}`" in source
    assert "offConfirmed?(ui.lang==='ru'?'Торренты выключены':'Torrents off')" in source
    assert "Shutdown confirmed" not in source
    assert "Traffic shutdown confirmed" not in source
    assert "Остановка подтверждена" not in source
    assert "Остановка трафика подтверждена" not in source
    # An unconfirmed transition is still a warning; only the healthy terminal
    # state gets the compact label.
    assert "Shutdown not confirmed" in source


def test_manga_status_debug_explains_unpainted_tokens() -> None:
    source = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    for marker in (
        "parsed_token_count",
        "mapped_token_count",
        "colored_token_count",
        "ignored_state_count",
        "unverified_state_count",
        "unknown_state_count",
        "category_disabled_count",
        "geometry_unmapped_count",
    ):
        assert marker in source
    assert "knowledge.ignored" in source
    assert "diagnostics.geometryUnmapped" in source
