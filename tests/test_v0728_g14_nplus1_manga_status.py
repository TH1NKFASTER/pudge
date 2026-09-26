from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def test_manga_status_overlay_accepts_segment_mapped_approximate_geometry_only() -> None:
    manga = (WEB / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "['observed','approximate'].includes(geometryStatus)" in manga
    assert "String(region.word_geometry || '') !== 'mapped_segments'" in manga
    assert "String(box.source || '') === 'region-single-token'" in manga
    assert "String(box.source || '').startsWith('mokuro-')" in manga
    assert "approximate:geometryStatus === 'approximate'" in manga
    assert "if (box.approximate) node.classList.add('is-approximate')" in manga
    assert "mangaGeometryStatus(region, segments) !== 'observed'" not in manga


def test_g14_javascript_syntax() -> None:
    for rel in ("pudge/web/reading_tools.js", "pudge/web/manga_reader_v2.js"):
        result = subprocess.run(["node", "--check", str(ROOT / rel)], cwd=ROOT, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
