from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, name
    opening = source.find("){", match.end())
    assert opening >= 0
    opening += 1
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "{":
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


def test_button_has_listen_then_stop_resume_session_states() -> None:
    source = HTML.read_text(encoding="utf-8")
    sync = _function(source, "syncLnPairedTray")
    script = f"""
const classes={{}};
const button={{disabled:false,textContent:'',title:'',classList:{{toggle(name,value){{classes[name]=Boolean(value);}}}}}};
const tray={{hidden:false,querySelectorAll(){{return [];}}}};
global.ui={{lang:'en',lnBook:{{paired_audio:{{book:{{duration:0,speed:1}}}}}},lnPairedExpanded:false,lnPairedStarted:false}};
global.syncLnFuriganaReadingSetting=()=>{{}};
global.ensureLnPairedControls=()=>button;
global.cancelLnPairedInterpolation=()=>{{}};
global.lnPairedPreparationText=()=>'';
global.$=id=>id==='lnPairedTray'?tray:null;
{sync}
const ready={{alignment:{{ready:true,status:'ready'}},playing:false,player_running:false,position:0,duration:0,speed:1}};
syncLnPairedTray(ready); const initial=button.textContent;
ui.lnPairedStarted=true; syncLnPairedTray(ready); const resumed=button.textContent;
syncLnPairedTray({{...ready,playing:true,player_running:true}}); const active=button.textContent;
console.log(JSON.stringify({{initial,resumed,active,classes}}));
"""
    result = _run_node(script)
    assert result["initial"] == "Listen"
    assert result["resumed"] == "Resume"
    assert result["active"] == "Pause"


def test_live_mpv_transition_never_restarts_from_reader_progress() -> None:
    source = HTML.read_text(encoding="utf-8")
    toggle = _function(source, "toggleLnPairedPlayback")
    script = f"""
const calls=[];
global.ui={{lnBook:{{id:6,paired_audio:{{}}}},lnChapter:{{chapter_index:1}},lnPairedState:{{audiobook_id:75,alignment:{{ready:true}},playing:false,player_running:true,paused:false,speed:1}},lnPairedStarted:false,lnPairedExpanded:false}};
global.invalidateLnPairedPoll=()=>calls.push('invalidate');
global.syncLnPairedTray=state=>calls.push(['sync',state.playing,state.player_running]);
global.applyLnPairedPosition=async()=>calls.push('apply');
global.pollLnPaired=()=>calls.push('poll');
global.stopLnPairedPoll=()=>calls.push('stopPoll');
global.lnReaderAudioProgress=()=>.42;
global.$=()=>({{value:'1'}});
global.pywebview={{api:{{
  audiobook_set_paused:async()=>{{calls.push('unpause');return {{ok:true}};}},
  light_novel_paired_state:async()=>{{calls.push('state');return {{audiobook_id:75,alignment:{{ready:true}},playing:true,player_running:true,paused:false,speed:1}};}},
  light_novel_play_paired:async()=>{{calls.push('restart');return {{audiobook_id:75,alignment:{{ready:true}},playing:true,player_running:true,paused:false,speed:1}};}},
  audiobook_stop:async()=>calls.push('stop'),
  light_novel_prepare_audio_alignment:async()=>{{}},
}}}};
{toggle}
(async()=>{{const result=await toggleLnPairedPlayback();console.log(JSON.stringify({{calls,result,started:ui.lnPairedStarted}}));}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    assert "unpause" in result["calls"]
    assert "restart" not in result["calls"]
    assert "state" in result["calls"]
    assert "apply" in result["calls"]
    assert "poll" in result["calls"]
    assert result["result"]["playing"] is True
    assert result["started"] is True


def test_furigana_previews_100ms_early_and_lingers_100ms() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "estimatedTime+.1*speed" in source
    assert "clockPosition+.1*speed" in source
    assert "ln-paired-furigana-preview" in source
    assert "ln-paired-furigana-linger" in source
    assert "},100)" in source

    clear = _function(source, "clearLnPairedFuriganaTiming")
    linger = _function(source, "lingerLnPairedFurigana")
    script = f"""
const names=new Set();
const word={{isConnected:true,classList:{{add(name){{names.add(name);}},remove(...items){{for(const item of items)names.delete(item);}}}}}};
global.ui={{}};
global.$=()=>null;
{clear}
{linger}
(async()=>{{
  lingerLnPairedFurigana(word);
  const immediate=names.has('ln-paired-furigana-linger');
  await new Promise(resolve=>setTimeout(resolve,130));
  const later=names.has('ln-paired-furigana-linger');
  console.log(JSON.stringify({{immediate,later}}));
}})();
"""
    result = _run_node(script)
    assert result == {"immediate": True, "later": False}


def test_inline_javascript_parses() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(r"<script([^>]*)>(.*?)</script>", source, flags=re.I | re.S)
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts
    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-v0749-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)
