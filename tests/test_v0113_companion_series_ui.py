from pathlib import Path
import re

def test_companion_series_ui_contracts():
    root = Path(__file__).parents[1] / "pudge" / "web" / "companion"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    for x in ("PUDGE_COMPANION_LIBRARY_UI_V8","groupLightNovels","lnSeriesKey","openLnSeries","seriesCard","attachCover","/cover"):
        assert x in app
    assert 'id="seriesView"' in html
    assert 'id="volumeGrid"' in html
    js = re.search(r"app\.js\?v=(\d+)", html)
    css_version = re.search(r"styles\.css\?v=(\d+)", html)
    assert js is not None
    assert css_version is not None
    assert js.group(1) == css_version.group(1)
    assert int(js.group(1)) >= 8
    assert ".series-view" in css and ".volume-grid" in css
