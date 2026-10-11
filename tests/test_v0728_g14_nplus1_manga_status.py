from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.t71g06_support import STUDY, column, reader, vertical

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def _run_node(script: str) -> dict:
    out = subprocess.check_output(["node", "-e", script], cwd=ROOT, text=True)
    return json.loads(out)


def test_nplus1_accepts_jiten_young_as_context_but_not_target() -> None:
    tools = json.dumps(str(WEB / "reading_tools.js"))
    script = f"""
    global.window=global;
    global.document={{documentElement:{{lang:'en'}},addEventListener(){{}},querySelectorAll(){{return []}},querySelector(){{return null}}}};
    global.requestAnimationFrame=()=>0; global.getComputedStyle=()=>({{}});
    require({tools});
    const study=PudgeReadingTools.study;
    const words=Array.from({{length:10}},(_,i)=>`語${{i}}`);
    const text=words.join(' ')+'。'; let cursor=0;
    const rows=words.map((word,i)=>{{const start=text.indexOf(word,cursor);cursor=start+word.length;
      const token={{wordId:i+1,readingIndex:0,surface:word,sentence:text,contextStart:start,contextEnd:cursor,
        card:{{states:i===4?['new']:['young'],frequencyRank:i===4?800:100,studyDeckIds:i===4?[]:[9]}}}};
      return {{token,node:{{dataset:{{}},classList:{{add(){{}},remove(){{}}}}}}}};
    }});
    const eligible=study.evaluateOptimalTargets(rows,{{frequencyLimit:1000}});
    process.stdout.write(JSON.stringify({{size:eligible.size,target:eligible.has(rows[4].token),youngTarget:eligible.has(rows[0].token)}}));
    """
    result = _run_node(script)
    assert result == {"size": 1, "target": True, "youngTarget": False}


def test_nplus1_large_chapter_has_bounded_short_sentence_fallback() -> None:
    tools = json.dumps(str(WEB / "reading_tools.js"))
    script = f"""
    global.window=global;
    global.document={{documentElement:{{lang:'en'}},addEventListener(){{}},querySelectorAll(){{return []}},querySelector(){{return null}}}};
    global.requestAnimationFrame=()=>0; global.getComputedStyle=()=>({{}});
    require({tools});
    const study=PudgeReadingTools.study;
    const sentenceParts=[]; for(let s=0;s<8;s++)sentenceParts.push(Array.from({{length:5}},(_,i)=>`語${{s}}_${{i}}`).join(' ')+'。');
    const text=sentenceParts.join(''); const rows=[]; let cursor=0; let id=1;
    for(let s=0;s<8;s++){{for(let i=0;i<5;i++){{const word=`語${{s}}_${{i}}`;const start=text.indexOf(word,cursor);cursor=start+word.length;
      const target=s===3&&i===2; const token={{wordId:id++,readingIndex:0,surface:word,sentence:text,contextStart:start,contextEnd:cursor,
        card:{{states:target?['new']:['mature'],frequencyRank:target?900:100,studyDeckIds:target?[]:[1]}}}};
      rows.push({{token,node:{{dataset:{{}},classList:{{add(){{}},remove(){{}}}}}}}});
    }}}}
    const eligible=study.evaluateOptimalTargets(rows,{{frequencyLimit:1000}});
    process.stdout.write(JSON.stringify({{rows:rows.length,size:eligible.size,target:eligible.has(rows[17].token)}}));
    """
    result = _run_node(script)
    assert result == {"rows": 40, "size": 1, "target": True}


def test_optimal_eligibility_survives_visual_highlight_toggle_for_card_star() -> None:
    tools = json.dumps(str(WEB / "reading_tools.js"))
    script = f"""
    global.window=global;
    global.document={{documentElement:{{lang:'en'}},addEventListener(){{}},querySelectorAll(){{return []}},querySelector(){{return null}}}};
    global.requestAnimationFrame=()=>0; global.getComputedStyle=()=>({{}});
    require({tools});
    const study=PudgeReadingTools.study;
    const words=Array.from({{length:10}},(_,i)=>`語${{i}}`);const text=words.join(' ')+'。';let cursor=0;
    const rows=words.map((word,i)=>{{const start=text.indexOf(word,cursor);cursor=start+word.length;
      const node={{dataset:{{}},classList:{{add(){{}},remove(){{}}}}}};
      const token={{wordId:i+1,readingIndex:0,surface:word,sentence:text,contextStart:start,contextEnd:cursor,
        card:{{states:i===2?['new']:['mature'],frequencyRank:i===2?800:100,studyDeckIds:[]}}}};
      return {{token,node}};
    }});
    study.applyOptimalHighlights(rows,{{enabled:false,frequencyLimit:1000}});
    process.stdout.write(JSON.stringify({{target:rows[2].node.dataset.pudgeOptimalEligible||'',other:rows[1].node.dataset.pudgeOptimalEligible||''}}));
    """
    assert _run_node(script) == {"target": "1", "other": ""}


# One page with every kind of OCR geometry; all words have a confirmed Jiten state.
GEOMETRY_KINDS = {
    0: [
        column("猫が好き", 0.80, 0.90),  # approximate, mapped per character
        column("白い犬", 0.70, 0.90, source="vision-accurate", status=None),  # observed (Vision) characters
        column("赤い花", 0.60, 0.90, mapped=False),  # characters not mapped to the text
        vertical("魚", 0.50, 0.70, 0.05, 0.10),  # no characters: the region is one word
        column("青い鳥", 0.40, 0.90, source="ink-grid-v1", status=None),  # synthetic grid
        vertical(
            "海だ",
            0.30,
            0.90,
            0.05,
            0.15,
            geometry_status="approximate",  # Mokuro lines only
            provenance={
                "source": "mokuro",
                "line_boxes": [{"text": "海だ", "x": 0.30, "y": 0.75, "width": 0.05, "height": 0.15}],
            },
        ),
        column(
            "山だ", 0.20, 0.90, source="mokuro", provenance={"source": "mokuro"}
        ),  # Mokuro lines split into characters
    ]
}
GEOMETRY_WORDS = {
    "猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]],
    "白い犬": [["白い", 4, 0], ["犬", 5, 0]],
    "赤い花": [["赤い", 6, 0], ["花", 7, 0]],
    "魚": [["魚", 8, 0]],
    "青い鳥": [["青い", 9, 0], ["鳥", 10, 0]],
    "海だ": [["海", 11, 0], ["だ", 12, 0]],
    "山だ": [["山", 13, 0], ["だ", 12, 0]],
}
LIVE = r"""
window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}, '3:0': {states: ['young', 'due'], normalizedState: 'due'},
  '4:0': {states: ['new'], normalizedState: 'new'}, '5:0': {states: ['known'], normalizedState: 'known'}};
for (const id of [6, 7, 8, 9, 10, 11, 13]) window.__live[`${id}:0`] = {states: ['young'], normalizedState: 'learning'};
"""


def test_word_states_are_painted_only_on_character_geometry_mapped_to_the_words(page) -> None:
    result = reader(
        page,
        "return marks();",
        regions=GEOMETRY_KINDS,
        parse=GEOMETRY_WORDS,
        prefs={"configured": True, "statusHighlight": True},
        before=STUDY + LIVE,
    )
    painted = {
        (mark["region"], mark["word"]): (mark["state"], mark["due"], mark["approximate"])
        for mark in result.value
    }
    # Observed and approximate per-character geometry is painted (approximate is marked as such);
    # a word without a confirmed state (が) is not.
    assert painted == {
        ("猫が好き", "猫"): ("new", False, True),
        ("猫が好き", "好き"): ("learning", True, True),
        ("白い犬", "白い"): ("new", False, False),
        ("白い犬", "犬"): ("known", False, False),
        ("山だ", "山"): ("learning", False, True),
    }


def test_g14_javascript_syntax() -> None:
    for rel in ("pudge/web/reading_tools.js", "pudge/web/manga_reader_v2.js"):
        result = subprocess.run(
            ["node", "--check", str(ROOT / rel)], cwd=ROOT, capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, result.stdout + result.stderr
