"""Escape closes the word card even with leftover selections; jpdb's five grades fit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"


def _run_escape(*, library_selected: bool, native_selected: bool, event_type: str = 'keydown', study_dom_open: bool = True) -> dict:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    dispatcher = "function isEscapeKey" + html.split("function isEscapeKey", 1)[1].split(
        "// pudge-r1-escape-dispatch-end", 1
    )[0]
    script = f"""
const vm=require('vm');
const dispatcher={dispatcher!r};
const state={{study:true,closes:[],libraryCleared:false,nativeCleared:false}};
const windowTarget=new EventTarget();global.window=windowTarget;
global.logs=[];global.logUiEvent=(name,payload)=>{{logs.push(name)}};
global.document={{getElementById:id=>id==='pudgeStudyCard'?{{classList:{{contains:()=>state.study&&{'true' if True else 'false'}}}}}:null}};
global.ui={{page:'lightnovels',lnSelection:new Set({'[1,2]' if library_selected else '[]'}),lnStudyTriggerCapturing:false,shortcutCaptureTarget:null,onboardingForced:false,state:{{settings:{{escape_exits_fullscreen:true}}}}}};
global.$=id=>({{hidden:id==='globalSearchOverlay',classList:{{contains:()=>false,remove(){{}}}},click(){{}}}});
global.clearLnSelection=()=>{{state.libraryCleared=true;ui.lnSelection.clear();}};
window.getSelection=()=>({{isCollapsed:{'false' if native_selected else 'true'},removeAllRanges(){{state.nativeCleared=true;}}}});
global.finishLnStudyTriggerCapture=()=>{{}};global.stopShortcutCapture=()=>{{}};global.closeLnFind=()=>{{}};
global.closeGlobalSearch=()=>{{}};global.closeModal=()=>{{}};global.hideContextMenu=()=>{{}};
global.closeLnChapterPicker=()=>{{}};global.hideLnStudyStateMenu=()=>{{}};global.hideLnTranslation=()=>{{}};
global.pywebview={{api:{{exit_fullscreen:async()=>{{state.closes.push('fullscreen')}}}}}};
window.PudgeSelect={{closeIfOpen:()=>false}};window.PudgeConfirm={{closeIfOpen:()=>false}};window.PudgeCoverPreview={{closeIfOpen:()=>false}};
window.PudgeReadingTools={{study:{{closeIfOpen:()=>{{if(!state.study)return false;state.study=false;state.closes.push('study');return true;}}}},closeIfOpen:()=>false}};
window.PudgeReviewGate={{handleEscape:()=>false}};window.PudgeMangaReaderV2={{selectedBookIds:()=>[],closeEscapeSurface:()=>false}};window.PudgeAudiobookSelection={{selectedBookIds:()=>[]}};
vm.runInThisContext(dispatcher);
const event=new Event({event_type!r},{{cancelable:true}});Object.defineProperties(event,{{key:{{value:'Escape'}},code:{{value:'Escape'}},repeat:{{value:false}},isComposing:{{value:false}}}});
window.dispatchEvent(event);
setImmediate(()=>process.stdout.write(JSON.stringify({{...state,logs}})));
"""
    return json.loads(subprocess.check_output(["node", "-e", script], text=True))


def test_escape_closes_word_card_despite_leftover_library_selection() -> None:
    result = _run_escape(library_selected=True, native_selected=False)
    assert result["study"] is False and result["closes"] == ["study"]
    assert result["libraryCleared"] is False


def test_escape_closes_word_card_despite_native_text_selection() -> None:
    result = _run_escape(library_selected=False, native_selected=True)
    assert result["study"] is False and result["closes"] == ["study"]
    assert result["nativeCleared"] is False


def test_keyup_fallback_closes_word_card_when_webkit_ate_keydown() -> None:
    result = _run_escape(library_selected=True, native_selected=True, event_type="keyup")
    assert result["study"] is False and result["closes"] == ["study"]
    assert result["libraryCleared"] is False and result["nativeCleared"] is False
    assert result["logs"] == ["escape.study_keyup_fallback"]


def test_five_grade_row_gets_its_own_line_and_labels_cannot_overlap() -> None:
    js = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert '<div class="pudge-study-action-row" data-count="${activeToken.reviewActions.actions.length}">' in js
    css = (WEB / "reading_tools.css").read_text(encoding="utf-8")
    assert ".pudge-study-review-actions .pudge-study-grade{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" in css
    assert '.pudge-study-action-row[data-count="5"]>.pudge-study-review-actions{flex:1 1 100%}' in css
    assert '.pudge-study-action-row[data-count="5"]>.pudge-study-add-wrap{flex:1 1 100%;justify-content:flex-end;padding-left:0;border-left:0}' in css


def test_escape_closes_context_menu_before_search_overlay_exists() -> None:
    """Regression: the lazily created search overlay counted as "open" while absent."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    dispatcher = "function isEscapeKey" + html.split("function isEscapeKey", 1)[1].split(
        "// pudge-r1-escape-dispatch-end", 1
    )[0]
    script = f"""
const vm=require('vm');
const dispatcher={dispatcher!r};
const state={{menu:true,closes:[]}};
global.window=new EventTarget();global.logUiEvent=()=>{{}};
global.document={{getElementById:()=>null}};
global.ui={{page:'current',lnSelection:new Set(),lnStudyTriggerCapturing:false,shortcutCaptureTarget:null,onboardingForced:false,state:{{settings:{{escape_exits_fullscreen:true}}}}}};
global.$=id=>id==='globalSearchOverlay'?null:({{hidden:false,style:{{}},classList:{{contains:name=>id==='contextMenu'&&name==='open'?state.menu:false,remove(){{}}}},click(){{}}}});
window.getSelection=()=>({{isCollapsed:true}});
global.clearLnSelection=()=>{{}};global.finishLnStudyTriggerCapture=()=>{{}};global.stopShortcutCapture=()=>{{}};global.closeLnFind=()=>{{}};
global.closeGlobalSearch=()=>{{state.closes.push('search')}};global.closeModal=()=>{{}};global.hideContextMenu=()=>{{state.menu=false;state.closes.push('menu')}};
global.closeLnChapterPicker=()=>{{}};global.hideLnStudyStateMenu=()=>{{}};global.hideLnTranslation=()=>{{}};
global.pywebview={{api:{{exit_fullscreen:async()=>{{state.closes.push('fullscreen')}}}}}};
window.PudgeSelect={{closeIfOpen:()=>false}};window.PudgeConfirm={{closeIfOpen:()=>false}};window.PudgeCoverPreview={{closeIfOpen:()=>false}};
window.PudgeReadingTools={{study:{{closeIfOpen:()=>false}},closeIfOpen:()=>false}};
window.PudgeReviewGate={{handleEscape:()=>false}};window.PudgeMangaReaderV2={{selectedBookIds:()=>[],closeEscapeSurface:()=>false,closeSettingsIfOpen:()=>false}};window.PudgeAudiobookSelection={{selectedBookIds:()=>[]}};
vm.runInThisContext(dispatcher);
const event=new Event('keydown',{{cancelable:true}});Object.defineProperties(event,{{key:{{value:'Escape'}},code:{{value:'Escape'}},repeat:{{value:false}},isComposing:{{value:false}}}});
window.dispatchEvent(event);
setImmediate(()=>process.stdout.write(JSON.stringify(state)));
"""
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result == {"menu": False, "closes": ["menu"]}


def test_grade_key_settings_are_collapsible() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<details id="s_review_shortcuts_group"' in html
    group = html[html.index('<details id="s_review_shortcuts_group"'):]
    assert group.index('<div id="s_review_shortcuts"') < group.index("</details>")
    assert '<div id="s_review_shortcuts" style="display:contents">' not in html
