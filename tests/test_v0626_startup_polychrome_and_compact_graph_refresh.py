from pathlib import Path


HTML = Path('pudge/web/index.html').read_text(encoding='utf-8')


def test_startup_poll_does_not_rebuild_watching_when_home_is_unchanged() -> None:
    assert 'currentRenderSignature:null' in HTML
    assert 'function currentRenderSignature(state=ui.state)' in HTML
    assert "const volatileKeys=new Set(['remaining_seconds','delete_remaining_seconds']);" in HTML
    assert "if(ui.currentRenderSignature!==nextCurrentSignature)" in HTML
    assert "if(renderSafely('current',renderCurrent))ui.currentRenderSignature=nextCurrentSignature;" in HTML


def test_full_graph_refresh_reloads_backend_compact_relations() -> None:
    marker = "if(action==='refresh-watch-order')"
    start = HTML.index(marker)
    end = HTML.index("if(action==='release')", start)
    block = HTML[start:end]
    assert 'await pywebview.api.refresh_relation_graph' in block
    assert 'ui.state=await pywebview.api.get_state_fast();' in block
    assert 'renderDataPages();' in block
    assert 'hydrateAllCompactRelationsFromGraph(ui.relationContext.graph);' in block
    assert block.index('ui.state=await pywebview.api.get_state_fast();') < block.index('renderDataPages();')
