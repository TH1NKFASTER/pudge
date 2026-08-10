from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_large_relation_graph_uses_global_chronological_x_axis() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html.split("function fullRelationGraphDiagram(graph)", 1)[1].split(
        "function planningRelationDiagram", 1
    )[0]
    assert "const chronological=[...byId.keys()].filter(id=>!collapsedAlternativeIds.has(id)).sort((a,b)=>earlier(a,b));" in source
    assert "const chronologicalIndex=new Map(chronological.map((id,index)=>[id,index]));" in source
    assert "const chronologicalX=id=>(chronologicalIndex.get(id)||0)*(NODE_W+X_GAP);" in source
    assert "backbone.forEach(id=>positions.set(id,{x:chronologicalX(id),y:0}));" in source
    assert "positions.set(id,{x:chronologicalX(id),y:" in source


def test_large_relation_graph_has_trackpad_zoom_mouse_pan_and_controls() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html.split("function initFullRelationGraphInteractions", 1)[1].split(
        "function renderOpenWatchOrder", 1
    )[0]
    assert "event.ctrlKey||event.metaKey||event.altKey" in source
    assert "Math.exp(-event.deltaY*.0025)" in source
    assert "pointerdown" in source and "pointermove" in source
    assert "data-relation-zoom=\"out\"" in html
    assert "data-relation-zoom=\"reset\"" in html
    assert "data-relation-zoom=\"in\"" in html


def test_edge_labels_use_larger_svg_text_with_background() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert ".full-relation-edge-label { fill:#d5e4f8; font-size:11px;" in html
    assert ".full-relation-edge-label-bg" in html
    assert '<rect class="full-relation-edge-label-bg"' in html
    assert "shape-rendering:geometricPrecision" in html
    assert "text-rendering:geometricPrecision" in html
