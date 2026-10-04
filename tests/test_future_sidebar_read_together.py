from pathlib import Path

ROOT = Path(__file__).parents[1]
INDEX = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
ASSISTANT = (ROOT / "pudge/web/reading_assistant.js").read_text(encoding="utf-8")
ASSISTANT_CSS = (ROOT / "pudge/web/reading_assistant.css").read_text(encoding="utf-8")
SIDEBAR = (ROOT / "pudge/web/sidebar_companion.js").read_text(encoding="utf-8")
SIDEBAR_CSS = (ROOT / "pudge/web/sidebar_companion.css").read_text(encoding="utf-8")
WEB_APP = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
READING_TOOLS = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
REVIEW_GATE = (ROOT / "pudge/web/review_gate.js").read_text(encoding="utf-8")


def test_assistant_geometry_is_persistent_resizable_and_resettable() -> None:
    assert "pudge.assistant.geometry.v1" in ASSISTANT
    assert "installGeometry(root)" in ASSISTANT
    assert "data-pa-reset-geometry" in ASSISTANT
    assert "resetGeometry" in ASSISTANT
    assert "ResizeObserver" in ASSISTANT
    assert "window.addEventListener?.('resize'" in ASSISTANT
    assert "cursor:ns-resize" in ASSISTANT_CSS
    assert "cursor:ew-resize" in ASSISTANT_CSS
    assert "cursor:nwse-resize" in ASSISTANT_CSS
    assert "cursor:nesw-resize" in ASSISTANT_CSS
    assert "cursor:move" in ASSISTANT_CSS


def test_read_together_context_action_persists_and_returns_to_audiobooks() -> None:
    assert 'data-ln-context-action="read-together"' in INDEX
    assert "pudge.readTogether.v1" in INDEX
    assert "async function openLightNovel(bookId,options={})" in INDEX
    # Plan §4: transport UI opens for explicit Read and listen OR a live linked
    # session (nested backend link shape); behaviour is executed in
    # tests/js/ln_entry_contract.cjs (nested_playing/paused_player/linked_idle).
    assert "ui.lnPairedExpanded=readTogether||entry.active" in INDEX
    assert "function lnPairedLinkSession(link)" in INDEX
    assert "returnPage:'audiobooks'" in INDEX
    assert "setPage('audiobooks')" in INDEX
    assert "storedReadTogetherSession()" in INDEX


def test_sidebar_audiobook_controls_and_ln_live_text_contract() -> None:
    assert "sidebar_companion.css" in INDEX
    assert "sidebar_companion.js" in INDEX
    assert "audiobook_state" in SIDEBAR
    assert "audiobook_seek" in SIDEBAR
    assert "audiobook_set_speed" in SIDEBAR
    assert "audiobook_add_bookmark" in SIDEBAR
    # Selection must pause the live player, not terminate it.
    selection = SIDEBAR.split("document.addEventListener('mouseup'", 1)[1]
    assert "audiobook_set_paused(Number(book.id), true)" in selection
    assert "audiobook_stop" not in selection
    assert "light_novel_paired_state" in SIDEBAR
    assert "light_novel_chapter" in SIDEBAR
    assert "data-pudge-translate-root" in SIDEBAR
    assert "data-pudge-study-hover" in SIDEBAR
    assert "sidebar-ln-live" in SIDEBAR_CSS
    assert "height:7.75em" in SIDEBAR_CSS


def test_sidebar_due_review_uses_wall_clock_and_cannot_be_disabled() -> None:
    assert "pudge.sidebarReview.lastAt.v1" in SIDEBAR
    assert "pudge.sidebarReview.interval.v1" in SIDEBAR
    assert "Date.now() - lastReviewAt()" in SIDEBAR
    assert "180000" in SIDEBAR
    assert "continuous" in SIDEBAR
    assert "sidebar_due_review_card" in SIDEBAR
    assert "sidebar_due_review_submit" in SIDEBAR
    assert 'id=\"s_sidebar_review_interval\"' in INDEX
    assert 'data-settings-category=\"advanced\"' in INDEX
    assert "option value=\"continuous\"" in INDEX
    setting = INDEX[INDEX.index('id="s_sidebar_review_interval"'):INDEX.index('id="s_sidebar_review_interval"') + 1200]
    assert 'option value="off"' not in setting
    assert "def sidebar_due_review_card" in WEB_APP
    assert "def sidebar_due_review_submit" in WEB_APP
    assert "pudge-review-completed" in READING_TOOLS
    assert "pudge-review-completed" in REVIEW_GATE
    assert "pudge-review-completed" in INDEX
