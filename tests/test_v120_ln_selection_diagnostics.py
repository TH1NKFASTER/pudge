from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "pudge" / "web" / "index.html"

def source() -> str:
    return INDEX.read_text(encoding="utf-8")

def test_previous_selection_experiments_are_reverted():
    text=source()
    assert ".ln-word{cursor:pointer;border-radius:3px;padding:0}" in text
    assert ".ln-word{cursor:pointer;border-radius:3px;padding:0;user-select:text;-webkit-user-select:text}" not in text
    assert "pudge-v0.7.25-ln-webkit-ruby-selection-v2" not in text
    assert ".ln-reader-shell .ln-reader rt,.ln-reader-shell .ln-reader rp{user-select:none;-webkit-user-select:none}" in text

def test_selection_diagnostics_capture_geometry_and_hit_testing():
    text=source()
    for token in ["pudge-v0.7.25-ln-selection-diagnostics-v1","document.caretPositionFromPoint","document.caretRangeFromPoint","document.elementsFromPoint","range.getClientRects()","webkit_user_select:style.webkitUserSelect","webkit_text_fill_color:style.webkitTextFillColor"]:
        assert token in text

def test_selection_trace_records_pointer_and_selectionchange():
    text=source()
    for token in ["lnSelectionTracePush('pointerdown'","lnSelectionTracePush('pointermove'","lnSelectionTracePush('pointerup'","lnSelectionTracePush('selectionchange'","ui.lnSelectionTrace.length>120"]:
        assert token in text

def test_debug_export_contains_selection_debug():
    text=source()
    assert "selection_debug:{history:Array.isArray(ui.lnSelectionTrace)?ui.lnSelectionTrace:[],current:lnSelectionState()}" in text
    assert "async function openLightNovel(bookId){installLnSelectionDiagnostics();" in text
