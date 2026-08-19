from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    return source[start:] if next_function < 0 else source[start:next_function]


def test_ln_series_focus_prefers_first_unfinished_volume() -> None:
    html = HTML.read_text(encoding="utf-8")
    function = _function(html, "currentSeriesBook")
    assert "!Boolean(book?.finished)" in function
    assert "return group.find(unfinished)||group[group.length-1];" in function
    assert "reading_progress" not in function


def test_ln_tab_reuses_existing_dom_when_state_is_unchanged() -> None:
    html = HTML.read_text(encoding="utf-8")
    load = html[html.index("async function loadLightNovels"):html.index("\nfunction scheduleLnStatePoll", html.index("async function loadLightNovels"))]
    assert "lnStateRenderSignature" in load
    assert "ui.lnRenderSignature===signature" in load
    cached_branch = load[load.index("if(ui.lnState&&!force&&rendered"):load.index("if(ui.lnState&&!force){")]
    assert "renderLightNovels" not in cached_branch


def test_ln_background_poll_only_rerenders_when_visible_state_changes() -> None:
    html = HTML.read_text(encoding="utf-8")
    poll = _function(html, "scheduleLnStatePoll")
    assert "beforeSignature=lnStateRenderSignature(ui.lnState)" in poll
    assert "nextSignature=lnStateRenderSignature(next)" in poll
    assert "if(nextSignature!==beforeSignature)" in poll
    assert poll.count("renderLightNovels()") == 1


def test_dropped_ln_is_injected_without_full_library_reload() -> None:
    html = HTML.read_text(encoding="utf-8")
    focus = html[html.index("async function focusDroppedImport"):html.index("\nfunction enableFinderDrop", html.index("async function focusDroppedImport"))]
    assert "const canInject=Boolean(focus.book" in focus
    assert "ui.skipActivatedPageLoadOnce=true" in focus
    assert "setPage(page);" in focus
    assert "injectLnBook(focus.book)" in focus
    assert "setTimeout(()=>void loadLightNovels(true)" not in focus
    inject = _function(html, "injectLnBook")
    assert "lnSeriesGroupHtml(group)" in inject
    assert "replaceWith(fresh)" in inject
    assert "renderLightNovels();return;" in inject


def test_drop_bridge_does_not_duplicate_default_event_handling_and_dispatch_is_fire_and_forget() -> None:
    web_app = WEB_APP.read_text(encoding="utf-8")
    assert "drop_handler = DOMEventHandler(dropped, True, True)" in web_app
    assert 'runner = getattr(window, "run_js", None)' in web_app
    assert "if callable(runner):" in web_app
    assert "runner(script)" in web_app
