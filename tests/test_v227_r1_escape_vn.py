from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from pudge.web_app import WebAppApi, _media_identity_anilist_kind


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"
INDEX = WEB / "index.html"
COVER = WEB / "cover_preview.js"
VN = WEB / "visual_novels.js"


def _escape_fixture() -> dict[str, object]:
    html = INDEX.read_text(encoding="utf-8")
    dispatcher = "function isEscapeKey" + html.split("function isEscapeKey", 1)[1].split(
        "// pudge-r1-escape-dispatch-end", 1
    )[0]
    cover_source = COVER.read_text(encoding="utf-8")

    script = f"""
const vm = require('vm');
const coverSource = {json.dumps(cover_source)};
const dispatcher = {json.dumps(dispatcher)};

const state = {{
  overlay:false, confirm:false, modal:false, manga:false,
  fullscreenCalls:0, closes:[]
}};

const windowTarget = new EventTarget();
global.window = windowTarget;
window.innerWidth = 1200;
window.innerHeight = 800;

const fakeImage = {{
  style:{{setProperty(){{}},transform:'',transition:''}},
  releasePointerCapture(){{}},
}};
const fakeOverlay = {{
  isConnected:true,
  querySelector(selector){{ return selector === 'img' ? fakeImage : null; }},
  remove(){{ state.overlay=false; this.isConnected=false; }},
}};
const classListFor = (id) => ({{
  contains(name) {{
    if (id === 'modalBackdrop' && name === 'open') return state.modal;
    return false;
  }},
  remove(){{}}, add(){{}}
}});

global.document = {{
  body: {{}},
  activeElement:null,
  addEventListener(){{}},
  querySelector(selector) {{
    if (!state.overlay) return null;
    if (selector === '.pudge-cover-preview') return fakeOverlay;
    if (selector === '.pudge-cover-preview img') return fakeImage;
    return null;
  }},
  createElement() {{ throw new Error('fixture does not open previews'); }},
  getElementById() {{ return null; }},
}};
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {{}};

global.ui = {{
  page:'lightnovels',
  lnSelection:new Set(),
  lnStudyTriggerCapturing:false,
  shortcutCaptureTarget:null,
  state:{{settings:{{escape_exits_fullscreen:true}}}},
}};

global.$ = id => ({{
  id,
  hidden: id === 'globalSearchOverlay' ? true : false,
  classList: classListFor(id),
  click() {{ state.closes.push(id + ':click'); }},
}});

global.clearLnSelection = () => {{ state.closes.push('selection'); ui.lnSelection.clear(); }};
global.finishLnStudyTriggerCapture = () => state.closes.push('ln-trigger');
global.stopShortcutCapture = () => state.closes.push('shortcut');
global.closeLnFind = () => state.closes.push('ln-find');
global.closeGlobalSearch = () => state.closes.push('search');
global.closeOnboarding = () => state.closes.push('onboarding-close');
global.skipOnboarding = async () => state.closes.push('onboarding-skip');
global.closeModal = () => {{ state.closes.push('modal'); state.modal=false; }};
global.hideContextMenu = () => state.closes.push('context');
global.closeLnChapterPicker = () => state.closes.push('ln-chapter');
global.hideLnStudyStateMenu = () => state.closes.push('ln-state');
global.hideLnTranslation = () => state.closes.push('ln-translation');

global.pywebview = {{api:{{exit_fullscreen:async()=>{{state.fullscreenCalls += 1;}}}}}};
window.PudgeSelect = {{closeIfOpen:()=>false}};
window.PudgeConfirm = {{closeIfOpen:()=>{{if(!state.confirm)return false;state.confirm=false;state.closes.push('confirm');return true;}}}};
window.PudgeDebug = {{close:()=>state.closes.push('debug')}};
window.PudgeReadingTools = {{closeIfOpen:()=>false}};
window.PudgeAudiobookSelection = {{selectedBookIds:()=>[],clearSelection:()=>{{}}}};
window.PudgeMangaReaderV2 = {{
  selectedBookIds:()=>[],
  clearSelection:()=>{{}},
  closeEscapeSurface:()=>{{if(!state.manga)return false;state.manga=false;state.closes.push('manga');return true;}}
}};

global.performance = global.performance || {{now:()=>0}};
vm.runInThisContext(coverSource);
vm.runInThisContext(dispatcher);

function keyEvent(type, key='Escape', code='Escape', repeat=false, composing=false) {{
  const event = new Event(type, {{cancelable:true}});
  Object.defineProperties(event, {{
    key:{{value:key}}, code:{{value:code}}, repeat:{{value:repeat}}, isComposing:{{value:composing}}
  }});
  return event;
}}
function reset() {{
  state.overlay=false;state.confirm=false;state.modal=false;state.manga=false;
  state.fullscreenCalls=0;state.closes=[];ui.lnSelection=new Set();
  fakeOverlay.isConnected=true;
}}
async function tick() {{ await Promise.resolve(); await new Promise(resolve=>setImmediate(resolve)); }}

(async()=>{{
  const result={{}};

  reset(); state.overlay=true; state.modal=true; ui.lnSelection=new Set([1]);
  let event=keyEvent('keydown'); window.dispatchEvent(event); await tick();
  result.previewOverModalSelection={{overlay:state.overlay,modal:state.modal,selection:ui.lnSelection.size,fullscreen:state.fullscreenCalls,prevented:event.defaultPrevented}};

  reset(); state.overlay=true;
  event=keyEvent('keydown','Escape','Escape',true,false); window.dispatchEvent(event); await tick();
  result.repeat={{overlay:state.overlay,fullscreen:state.fullscreenCalls,prevented:event.defaultPrevented}};

  reset(); state.overlay=true; ui.lnSelection=new Set([11]);
  event=keyEvent('keyup'); window.dispatchEvent(event); await tick();
  result.keyup={{overlay:state.overlay,selection:ui.lnSelection.size,prevented:event.defaultPrevented,closes:[...state.closes]}};

  reset(); state.overlay=true;
  event=keyEvent('keydown','Esc','',false,false); window.dispatchEvent(event); await tick();
  result.escAlias={{overlay:state.overlay}};

  reset(); state.overlay=true;
  event=keyEvent('keydown','x','Escape',false,false); window.dispatchEvent(event); await tick();
  result.codeAlias={{overlay:state.overlay}};

  reset(); ui.lnSelection=new Set([7]);
  event=keyEvent('keydown'); window.dispatchEvent(event); await tick();
  result.selection={{selection:ui.lnSelection.size,fullscreen:state.fullscreenCalls,closes:[...state.closes]}};

  reset(); state.manga=true;
  event=keyEvent('keydown'); window.dispatchEvent(event); await tick();
  result.reader={{manga:state.manga,fullscreen:state.fullscreenCalls,closes:[...state.closes]}};

  reset(); state.overlay=true;
  event=keyEvent('keydown','Escape','Escape',false,true); window.dispatchEvent(event); await tick();
  result.composing={{overlay:state.overlay,prevented:event.defaultPrevented,fullscreen:state.fullscreenCalls}};

  reset(); state.confirm=true; state.overlay=true;
  event=keyEvent('keydown'); window.dispatchEvent(event); await tick();
  result.confirmPriority={{confirm:state.confirm,overlay:state.overlay,closes:[...state.closes]}};

  reset();
  event=keyEvent('keydown'); window.dispatchEvent(event); await tick();
  result.fullscreen={{fullscreen:state.fullscreenCalls}};

  process.stdout.write(JSON.stringify(result));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    return json.loads(subprocess.check_output(["node", "-e", script], text=True, stderr=subprocess.STDOUT))


def test_escape_dispatch_closes_exactly_one_uppermost_surface() -> None:
    result = _escape_fixture()

    assert result["previewOverModalSelection"] == {
        "overlay": True,
        "modal": True,
        "selection": 0,
        "fullscreen": 0,
        "prevented": True,
    }
    assert result["repeat"] == {"overlay": True, "fullscreen": 0, "prevented": True}
    assert result["keyup"] == {"overlay": True, "selection": 0, "prevented": True, "closes": ["selection"]}
    assert result["escAlias"] == {"overlay": False}
    assert result["codeAlias"] == {"overlay": False}
    assert result["selection"] == {"selection": 0, "fullscreen": 0, "closes": ["selection"]}
    assert result["reader"] == {"manga": False, "fullscreen": 0, "closes": ["manga"]}
    assert result["composing"] == {"overlay": True, "prevented": False, "fullscreen": 0}
    assert result["confirmPriority"] == {"confirm": False, "overlay": True, "closes": ["confirm"]}
    assert result["fullscreen"] == {"fullscreen": 1}


def test_escape_has_one_primary_dispatcher_and_selection_keyup_fallback() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert html.count("window.addEventListener('keydown',dispatchEscapeEvent,true)") == 1
    assert html.count("window.addEventListener('keyup',captureEscapeCardSelection,true)") == 1
    assert "if(!clearSelectionOnEscape('keyup'))return;" in html

    for name in (
        "cover_preview.js",
        "pudge_select.js",
        "pudge_confirm.js",
        "reading_tools.js",
        "manga_reader_v2.js",
        "debug.js",
    ):
        source = (WEB / name).read_text(encoding="utf-8")
        assert "event.key === 'Escape'" not in source
        assert "event.key==='Escape'" not in source


def test_visual_novel_frontend_has_no_anilist_identity_surface() -> None:
    source = VN.read_text(encoding="utf-8")
    assert "vnIdentity" not in source
    assert "refreshIdentity" not in source
    assert "media_identity_current('visual_novel'" not in source
    assert "showMediaIdentity?.('visual_novel'" not in source
    assert "pudge-media-identity-changed" not in source

    # Capture/OCR/Jiten reading functionality remains present.
    assert "visual_novel_windows" in source
    assert "visual_novel_start" in source
    assert "visual_novel_parse" in source
    assert "PudgeReadingTools" in source


def test_backend_rejects_vn_and_unknown_anilist_identity_before_routing() -> None:
    for kind in ("visual_novel", "game", "anime", "", "unknown"):
        with pytest.raises(ValueError):
            _media_identity_anilist_kind(kind)

    api = object.__new__(WebAppApi)
    for method, args in (
        (api.media_identity_search, ("visual_novel", "x")),
        (api.media_identity_current, ("visual_novel", 1)),
        (api.media_identity_bind, ("visual_novel", 1, 2, {})),
        (api.media_identity_unbind, ("visual_novel", 1)),
    ):
        with pytest.raises(ValueError, match="visual novels"):
            method(*args)


def test_backend_preserves_ln_manga_audiobook_anilist_search_routes() -> None:
    calls: list[tuple[str, str]] = []

    class LightNovels:
        def search_anilist_novels(self, query: str) -> list[dict[str, str]]:
            calls.append(("literature", query))
            return [{"route": "literature"}]

    api = object.__new__(WebAppApi)
    api.light_novels = LightNovels()
    api.manga_search_anilist = lambda query: calls.append(("manga", query)) or [{"route": "manga"}]
    api.planning_search_anilist = lambda query: calls.append(("planning", query)) or [{"route": "planning"}]

    assert api.media_identity_search("ln", "  wolf  ") == [{"route": "literature"}]
    assert api.media_identity_search("audiobook", "  audio  ") == [{"route": "literature"}]
    assert api.media_identity_search("manga", "  piece  ") == [{"route": "manga"}]
    assert calls == [
        ("literature", "wolf"),
        ("literature", "audio"),
        ("manga", "piece"),
    ]
