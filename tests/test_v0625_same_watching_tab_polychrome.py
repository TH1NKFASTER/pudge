from pathlib import Path


def test_clicking_active_watching_tab_does_not_restart_polychrome() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("function setPage(page,force=false)")
    end = html.index("function updateCount()", start)
    body = html[start:end]

    assert "if(!force&&ui.page===page)return false;" in body
    assert "if(!force&&ui.page===page){if(page==='current')queuePolychromeWake();return false;}" not in body
    # Real transitions back to Watching and focus restoration still wake the foil.
    assert "if(page==='current')queuePolychromeWake();" in body
    assert "if(next){queuePolychromeWake();" in html
