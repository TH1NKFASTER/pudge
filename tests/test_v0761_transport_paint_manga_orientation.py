from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pudge.manga import _normalize_region_orientation
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
MANGA_JS = ROOT / "pudge/web/manga_reader_v2.js"


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, name
    opening = source.find("{", match.end())
    assert opening >= 0
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


def test_pause_bridge_does_not_build_the_full_audiobook_library_state() -> None:
    calls: list[tuple[object, ...]] = []

    class FakeAudiobooks:
        def set_paused(self, book_id: int, paused: bool) -> dict[str, object]:
            calls.append(("set_paused", book_id, paused))
            return {"ok": True, "book_id": book_id, "paused": paused}

        def state(self) -> dict[str, object]:
            raise AssertionError("latency-sensitive pause must not build library state")

    api = WebAppApi.__new__(WebAppApi)
    api.audiobooks = FakeAudiobooks()

    assert api.audiobook_set_paused(75, True) == {
        "ok": True,
        "book_id": 75,
        "paused": True,
    }
    assert calls == [("set_paused", 75, True)]


def test_coalesced_pause_resume_does_not_freeze_text_when_mpv_never_pauses() -> None:
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
  light_novel_paired_state:async()=>{{await sleep(25);return {{...actual}};}},
  audiobook_set_paused:async(_id,paused)=>{{calls.push(['pause',paused]);actual={{...actual,playing:!paused,paused}};return {{ok:true}};}},
  light_novel_play_paired:async()=>{{throw new Error('unexpected restart');}},
  light_novel_prepare_audio_alignment:async()=>{{}},
}}}};
{toggle}
(async()=>{{
  const first=toggleLnPairedPlayback();
  const second=toggleLnPairedPlayback();
  await Promise.all([first,second]);
  console.log(JSON.stringify({{calls,actual}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    pause_calls = [
        row for row in result["calls"]
        if isinstance(row, list) and row[0] == "pause"
    ]
    assert pause_calls == [["pause", True], ["pause", False]]
    assert result["calls"].count("cancel") == 0
    assert result["actual"]["playing"] is True


def test_multicolumn_japanese_bubble_is_normalized_to_vertical() -> None:
    region = _normalize_region_orientation(
        {
            "text": "おれの財宝か？欲しけりゃくれてやる",
            "raw_text": "ざい",
            "orientation": "horizontal",
            "confidence": 0.3,
            "detector": "vision-contrast+vision-original+vision-rectangles",
            "x": 0.697947,
            "y": 0.5315,
            "width": 0.180421,
            "height": 0.0945,
        }
    )
    assert region["orientation"] == "vertical"
    assert region["orientation_reason"] == "japanese-multicolumn-geometry"


def test_shallow_horizontal_caption_stays_horizontal() -> None:
    region = _normalize_region_orientation(
        {
            "text": "冒険の夜明け",
            "orientation": "horizontal",
            "detector": "vision-contrast+vision-original",
            "width": 0.212,
            "height": 0.043667,
        }
    )
    assert region["orientation"] == "horizontal"


def test_frontend_applies_vertical_writing_mode_to_existing_cached_artifact() -> None:
    source = MANGA_JS.read_text(encoding="utf-8")
    function = _function(source, "effectiveRegionOrientation")
    script = f"""
{function}
const target=effectiveRegionOrientation({{
  text:'おれの財宝か？欲しけりゃくれてやる',
  orientation:'horizontal',
  detector:'vision-contrast+vision-original+vision-rectangles'
}},.180421,.0945);
const caption=effectiveRegionOrientation({{
  text:'冒険の夜明け',
  orientation:'horizontal',
  detector:'vision-contrast+vision-original'
}},.212,.043667);
console.log(JSON.stringify({{target,caption}}));
"""
    result = _run_node(script)
    assert result["target"] == {
        "vertical": True,
        "reason": "low-confidence-vision-japanese",
    }
    assert result["caption"] == {
        "vertical": False,
        "reason": "horizontal",
    }
    assert "data-effective-orientation" in source
    assert "data-orientation-reason" in source
