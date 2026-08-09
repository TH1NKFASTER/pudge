from pathlib import Path

from anime_mpv.database import Database


def test_relation_graph_cache_returns_one_shared_component(tmp_path: Path) -> None:
    db = Database(tmp_path / "anime.sqlite3")
    graph = {
        "nodes": [
            {"media_id": 101, "title": "First"},
            {"media_id": 102, "title": "Second"},
        ],
        "edges": [{"source": 101, "target": 102, "relation_type": "SEQUEL"}],
    }
    graph_id = db.store_relation_graph(
        graph,
        refreshed_at=100.0,
        next_refresh_at=200.0,
    )

    cached = db.relation_graph_cache([101, 102])

    assert len(cached) == 1
    assert cached[0]["graph_id"] == graph_id
    assert cached[0]["graph"] == graph
    assert cached[0]["members"] == [101, 102]


def test_watch_order_renders_cached_graph_before_showing_modal() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "get_relation_graph_cache" in html
    assert "relationGraphCache:{graphs:{},mediaToGraph:{}}" in html
    assert "const cached=cachedRelationGraph(a.media_id);" in html

    cached_branch = html.split("if(cached){", 1)[1].split("context.loadingTimer", 1)[0]
    assert "await prepareCachedWatchOrderFirstFrame(context,startedAt);" in cached_branch
    assert "pywebview.api.get_relation_graph" not in cached_branch
    prepare = html.split("async function prepareCachedWatchOrderFirstFrame", 1)[1].split(
        "function showWatchOrderModal", 1
    )[0]
    assert prepare.index("backdrop.hidden=true") < prepare.index("renderOpenWatchOrder();")
    assert prepare.index("renderOpenWatchOrder();") < prepare.index("backdrop.hidden=false")
    assert prepare.index("backdrop.hidden=false") < prepare.index("backdrop.classList.add('open')")


def test_uncached_watch_order_does_not_show_empty_modal_before_delay() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    function = html.split("async function openWatchOrder(mediaId){", 1)[1].split(
        "async function openRelease", 1
    )[0]

    before_timer = function.split("context.loadingTimer=setTimeout", 1)[0]
    assert "showWatchOrderModal();" not in before_timer
    assert "classList.add('open','watch-order-open')" not in before_timer
    assert "context.showLoading=true;" in function
