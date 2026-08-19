from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
MANGA = ROOT / "pudge/web/manga_reader_v2.js"
DEBUG = ROOT / "pudge/web/debug.js"
WEB_APP = ROOT / "pudge/web_app.py"


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, name
    opening = source.find("){", match.end())
    assert opening >= 0
    opening += 1
    depth = 0
    quote: str | None = None
    escaped = False
    index = opening
    while index < len(source):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(name)


def _run_node(source: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_cmd_shift_l_routes_to_active_debug_context_without_ui_buttons() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA.read_text(encoding="utf-8")
    debug = DEBUG.read_text(encoding="utf-8")

    assert "async function exportActiveDebugContext()" in html
    assert "event.metaKey||!event.shiftKey" in html
    assert "event.code!=='KeyL'" in html
    assert "window.PudgeMangaReaderV2.exportDebug()" in html
    assert "exportLnPairedTrace()" in html
    assert "window.PudgeDebug.exportCurrent()" in html
    assert "pywebview.api.export_runtime_debug_bundle" in html
    assert 'data-manga-v2-action="ocr-debug"' not in manga
    assert "event.key.toLowerCase() === 'o' && event.shiftKey" not in manga
    assert "exportDebug: exportMangaOcrDebug" in manga
    assert 'data-debug-action="export"' not in debug
    assert "exportCurrent: exportJson" in debug
    web_app = WEB_APP.read_text(encoding="utf-8")
    assert "def export_runtime_debug_bundle(" in web_app
    assert "debug_log_dir()" in web_app
    assert "Pudge-patch-logs" not in web_app
    assert 'archive.write(DEFAULT_LOG_PATH, "runtime.log")' in web_app


def test_ocr_status_uses_processed_without_page_counter() -> None:
    source = MANGA.read_text(encoding="utf-8")
    assert source.count("ru() ? 'Обработалось' : 'Processed'") == 2
    assert "'Распознаю' : 'Recognizing'" not in source


def test_rapid_space_toggles_are_coalesced_to_final_intent() -> None:
    source = HTML.read_text(encoding="utf-8")
    toggle = _function(source, "toggleLnPairedPlayback")
    script = f"""
const calls=[];
let actual={{audiobook_id:75,alignment:{{ready:true,status:'ready'}},playing:true,player_running:true,paused:false,position:100,speed:1,chapter_char_offset_exact:10}};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
global.ui={{lnBook:{{id:6,paired_audio:{{}}}},lnChapter:{{chapter_index:1}},lnPairedState:{{...actual}},lnPairedStarted:true,lnPairedExpanded:true,lnPairedTransportDesired:null,lnPairedTransportGeneration:0,lnPairedTransportPromise:null,lnPairedLastDisplayOffset:10}};
global.invalidateLnPairedPoll=()=>calls.push('invalidate');
global.syncLnPairedTray=state=>{{ui.lnPairedState={{...state}};calls.push(['sync',Boolean(state.playing)]);}};
global.cancelLnPairedInterpolation=()=>calls.push('cancel');
global.applyLnPairedPosition=async()=>calls.push('apply');
global.pollLnPaired=()=>calls.push('poll');
global.lnPairedTrace=()=>{{}};
global.lnReaderAudioProgress=()=>.5;
global.$=()=>({{value:'1'}});
global.pywebview={{api:{{
  light_novel_paired_state:async()=>{{await sleep(5);return {{...actual}};}},
  audiobook_set_paused:async(_id,paused)=>{{calls.push(['pause',paused]);await sleep(30);actual={{...actual,playing:!paused,paused}};return {{ok:true}};}},
  light_novel_play_paired:async()=>{{calls.push('restart');actual={{...actual,playing:true,player_running:true,paused:false}};return {{...actual}};}},
  light_novel_prepare_audio_alignment:async()=>{{}},
}}}};
{toggle}
(async()=>{{
  const pending=[];
  for(let index=0;index<7;index++)pending.push(toggleLnPairedPlayback());
  const results=await Promise.all(pending);
  console.log(JSON.stringify({{calls,actual,results:results.map(row=>row.playing)}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    pause_calls = [row for row in result["calls"] if isinstance(row, list) and row[0] == "pause"]
    assert result["actual"]["playing"] is False
    assert len(pause_calls) <= 2
    assert "restart" not in result["calls"]
    assert all(value is False for value in result["results"])


def test_toggle_arriving_during_position_apply_is_not_lost() -> None:
    source = HTML.read_text(encoding="utf-8")
    toggle = _function(source, "toggleLnPairedPlayback")
    script = f"""
const calls=[];
let actual={{audiobook_id:75,alignment:{{ready:true,status:'ready'}},playing:false,player_running:true,paused:true,position:100,speed:1,chapter_char_offset_exact:10}};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
global.ui={{lnBook:{{id:6,paired_audio:{{}}}},lnChapter:{{chapter_index:1}},lnPairedState:{{...actual}},lnPairedStarted:true,lnPairedExpanded:true,lnPairedTransportDesired:null,lnPairedTransportGeneration:0,lnPairedTransportPromise:null,lnPairedLastDisplayOffset:10}};
global.invalidateLnPairedPoll=()=>{{}};
global.syncLnPairedTray=state=>{{ui.lnPairedState={{...state}};}};
global.cancelLnPairedInterpolation=()=>calls.push('cancel');
let injected=false;
global.applyLnPairedPosition=async()=>{{calls.push('apply');if(!injected){{injected=true;void toggleLnPairedPlayback();await sleep(10);}}}};
global.pollLnPaired=()=>{{}};
global.lnPairedTrace=()=>{{}};
global.lnReaderAudioProgress=()=>.5;
global.$=()=>({{value:'1'}});
global.pywebview={{api:{{
  light_novel_paired_state:async()=>{{await sleep(2);return {{...actual}};}},
  audiobook_set_paused:async(_id,paused)=>{{calls.push(['pause',paused]);await sleep(4);actual={{...actual,playing:!paused,paused}};return {{ok:true}};}},
  light_novel_play_paired:async()=>{{throw new Error('unexpected restart');}},
  light_novel_prepare_audio_alignment:async()=>{{}},
}}}};
{toggle}
(async()=>{{
  const result=await toggleLnPairedPlayback();
  await sleep(25);
  console.log(JSON.stringify({{calls,actual,result}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    pause_calls = [row for row in result["calls"] if isinstance(row, list) and row[0] == "pause"]
    assert pause_calls == [["pause", False], ["pause", True]]
    assert result["actual"]["playing"] is False
    assert result["result"]["playing"] is False


def test_space_transport_key_is_not_treated_as_manual_scroll() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "['ArrowUp','ArrowDown','PageUp','PageDown','Home','End']" in source
    assert "['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' ']" not in source


def test_manual_navigation_trace_is_debounced() -> None:
    source = HTML.read_text(encoding="utf-8")
    function = _function(source, "markLnPairedManualNavigation")
    assert ">=250" in function
    assert "lnPairedManualNavigationTraceAt" in function
