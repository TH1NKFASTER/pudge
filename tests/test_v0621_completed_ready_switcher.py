from pathlib import Path
import json
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def _function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(name)


def test_completed_ready_suppresses_readiness_diagnostics():
    source = HTML.read_text()
    assert "completed-ready-section" in source
    assert "readinessDiagnosisSuppressed" in source
    assert "const suppressDiagnosis=!!card.closest('.completed-ready-section, .caught-up-section')" in source
    assert "homeSection('section.caughtUp',home.caught_up||[],caughtUpHomeCard,'caught-up-section')" in source
    assert "!a.readinessDiagnosisSuppressed&&needsReadinessDiagnosis(a)" in source


def test_completed_ready_sequence_has_visible_switch_controls():
    source = HTML.read_text()
    assert 'data-action="ready-sequence-previous"' in source
    assert 'data-action="ready-sequence-next"' in source
    assert 'data-action="select-ready-entry"' in source
    assert "data-ready-sequence-position" in source
    assert "selectReadySequenceEntry(card" in source


def test_sequence_selection_updates_the_playable_entry_in_javascript(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    source = HTML.read_text()
    function = _function(source, "selectReadySequenceEntry")
    script = f"""
const ui={{playStates:new Map()}};
const t=(key)=>key;
const escapeHtml=(value)=>String(value);
const applyOverflowTitleTooltips=()=>{{}};
function classes(){{const values=new Set();return {{remove:(...xs)=>xs.forEach(x=>values.delete(x)),add:(...xs)=>xs.forEach(x=>values.add(x)),toggle:(x,on)=>on?values.add(x):values.delete(x),has:x=>values.has(x)}};}}
const rows=[
  {{dataset:{{mediaId:'11',siteUrl:'u1',title:'First',path:'/first.mkv',coverUrl:'c1',stateText:'ready 1'}},classList:classes()}},
  {{dataset:{{mediaId:'22',siteUrl:'u2',title:'Second',path:'/second.mkv',coverUrl:'c2',stateText:'ready 2'}},classList:classes()}}
];
const media={{innerHTML:''}}, title={{textContent:'',dataset:{{}},title:''}}, state={{textContent:''}}, position={{textContent:''}};
const card={{dataset:{{activeIndex:'0'}},classList:classes(),querySelectorAll:(q)=>rows,querySelector:(q)=>q==='.ready-sequence-cover-media'?media:q==='.ready-sequence-meta strong'?title:q==='.ready-sequence-meta .airing-state'?state:q==='[data-ready-sequence-position]'?position:null}};
{function}
if(!selectReadySequenceEntry(card,1))throw new Error('selection failed');
if(card.dataset.mediaId!=='22'||card.dataset.path!=='/second.mkv'||card.dataset.playPath!=='/second.mkv')throw new Error(JSON.stringify(card.dataset));
if(title.textContent!=='Second'||state.textContent!=='ready 2'||position.textContent!=='2/2')throw new Error('visible state not updated');
if(!rows[1].classList.has('active')||rows[0].classList.has('active'))throw new Error('active row not updated');
"""
    path = tmp_path / "test.js"
    path.write_text(script)
    subprocess.run([node, str(path)], check=True, capture_output=True, text=True)
