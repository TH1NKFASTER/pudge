import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"


def test_study_header_row_is_vertically_centred():
    css = (WEB / "reading_tools.css").read_text(encoding="utf-8")
    assert 'grid-template-areas:"term actions close";align-items:center' in css
    assert ".pudge-study-close{grid-area:close;align-self:center" in css


def test_grade_buttons_have_colour_only_hover_and_grey_unavailable_state():
    css = (WEB / "reading_tools.css").read_text(encoding="utf-8")
    for tone in ("again", "hard", "good", "easy"):
        rule = re.search(r"\.pudge-study-grade\.grade-%s:hover\{([^}]*)\}" % tone, css)
        assert rule and "background" in rule.group(1)
        assert "transform" not in rule.group(1) and "filter" not in rule.group(1)
    assert ".pudge-study-grade.is-unavailable" in css and "cursor:not-allowed" in css


def test_unavailable_grades_keep_hover_tooltip():
    js = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    # aria-disabled keeps the native title tooltip in WebKit; [disabled] drops it.
    assert "data-pudge-study-review=\"${esc(action.id)}\"${hint} aria-disabled=\"false\"" in js
    assert "${current.reviewable ? '' : 'disabled'}" not in js
    assert "setStudyButtonAvailability(button, reason" in js


def test_reader_auto_follow_scroll_does_not_defer_paired_highlight():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "ui.lnPairedAutoScrollUntil=now+(seekJump?250:1100);target.scrollIntoView(" in html
    assert "function lnPairedAutoFollowScroll(now,wasScrolling)" in html
    assert "if(lnPairedAutoFollowScroll(now,wasScrolling)){" in html
    # Real user input ends the auto-follow window immediately.
    assert "addEventListener('wheel',()=>{ui.lnPairedAutoScrollUntil=0;" in html
    assert "addEventListener('pointerdown',()=>{ui.lnPairedAutoScrollUntil=0;" in html


def test_study_deck_choice_survives_app_relaunch(tmp_path):
    from types import SimpleNamespace
    from pudge.database import Database
    from pudge.web_app import WebAppApi
    db = Database(tmp_path / "library.sqlite3")
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    assert api.study_deck_preference("jiten") == {"ok": True, "deck_id": ""}
    assert api.set_study_deck_preference("jiten", "5")["ok"] is True
    relaunched = WebAppApi.__new__(WebAppApi)
    relaunched.manager = SimpleNamespace(db=Database(tmp_path / "library.sqlite3"))
    assert relaunched.study_deck_preference("Jiten")["deck_id"] == "5"
    assert relaunched.study_deck_preference("jpdb")["deck_id"] == ""


def test_reader_restores_persisted_deck_not_only_local_storage():
    js = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert "API()?.set_study_deck_preference?.(key, value)" in js
    assert "const remembered = await persistedStudyDeck(activeToken.backend);" in js


def test_sidebar_live_text_survives_hidden_window():
    js = (WEB / "sidebar_companion.js").read_text(encoding="utf-8")
    body = js.split("function powerChanged() {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!liveTextAllowed() && (energySaving() || (!floating && audioState?.float_open))) {" in body
    assert "document.hidden" not in body.split("host.replaceChildren()", 1)[0]


def test_open_select_menu_is_not_rebuilt_under_the_pointer():
    js = (WEB / "pudge_select.js").read_text(encoding="utf-8")
    sync = js.split("function sync(select) {", 1)[1].split("\n  function ", 1)[0]
    assert "if (state.menuSignature !== menuSignature(select)) {" in sync
    assert "state.menuSignature = menuSignature(select);" in js


def test_ln_speed_choice_is_not_reverted_by_stale_poll_and_pause_keeps_highlight():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "ui.lnPairedSpeedPending={value:String(speed),until:performance.now()+4000}" in html
    assert "if(speedSelect&&!ui.lnPairedSpeedPending&&globalThis.document?.activeElement!==speedSelect" in html
    assert "if(speedOnly&&!updated.playing){" in html


def test_review_refresh_retries_until_jiten_state_changes_and_recolours_ln():
    js = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "const JITEN_POST_MUTATION_RETRY_MS = [1200, 3000, 7000];" in js
    assert "if (!row || studyStateFingerprint(row) === prior) retry();" in js
    assert "new CustomEvent('pudge-study-pairs-updated'" in js
    assert "window.addEventListener('pudge-study-pairs-updated'" in html
    assert "function applyLnLiveStateRows(rows){" in html


def test_music_videos_are_never_announced():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    py = (WEB.parent / "web_app.py").read_text(encoding="utf-8")
    assert 'and str(row.get("format") or "").upper() != "MUSIC"' in py
    assert "!anilistRelatedUi.shown.has(Number(item.id))&&String(item?.format||'').toUpperCase()!=='MUSIC'" in html
    assert "!plannedReleaseUi.shown.has(Number(item.media_id))&&String(item?.format||'').toUpperCase()!=='MUSIC'" in html


def test_select_menu_open_close_reasons_are_exported():
    js = (WEB / "pudge_select.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "trace: () => traceRows.slice()" in js and "trace('open-refused'" in js
    assert "select_debug:window.PudgeSelect?.trace?.()||[]," in html


def test_select_toggles_on_press_because_webkit_can_drop_the_click():
    js = (WEB / "pudge_select.js").read_text(encoding="utf-8")
    assert "if (openSelect === select) close(select, 'toggle-press'); else open(select);" in js
    assert "if (now() - pressedAt < 800) { pressedAt = -Infinity; return; }" in js


def test_mangaocr_install_log_button_is_fully_removed():
    for name in ("web/index.html", "web/media.js", "web_app.py"):
        text = (WEB.parent / name).read_text(encoding="utf-8")
        assert "openMangaOcrLog" not in text and "reveal_manga_ocr_install_log" not in text


def test_sidebar_cover_click_opens_audiobook_and_zoom_uses_cover_preview_gestures():
    js = (WEB / "sidebar_companion.js").read_text(encoding="utf-8")
    preview = (WEB / "cover_preview.js").read_text(encoding="utf-8")
    assert "event.target.closest?.('.sidebar-due-image img');" in js
    assert "'.sidebar-audio-cover img,.sidebar-due-image img'" not in js
    assert 'data-sc-audio="open"' in js
    assert "'.sidebar-audio-cover img'," in preview  # pinch/drag zoom still available


def test_repository_root_has_no_leftover_tool_references():
    root = WEB.parents[1]
    assert not (root / "....").exists()
    for path in (root / "tests").glob("test_*.py"):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        assert 'ROOT / "scripts" / "compare_subtitle_alignment' not in text, path.name
        assert "ROOT / 'scripts' / 'compare_subtitle_alignment" not in text, path.name
