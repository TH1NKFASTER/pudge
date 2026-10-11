"""A watch order that arrives quickly never flashes the loading text (behaviour, page
harness; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

from test_v0614_preloaded_watch_order_render import OPEN, watch_order_backend


def test_cached_watch_order_does_not_flash_loading_copy_immediately(
    tmp_path: Path, monkeypatch, page
) -> None:
    # Not in the start-up cache, but AniList answers within 200 ms: the modal opens once,
    # directly with the graph; "Loading…" is kept for slow answers only, and the late
    # loading timer does not redraw the graph.
    backend = watch_order_backend(tmp_path, monkeypatch)
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + """
            await __tick(400);
            await __flush();
            return {views: shownViews(), shown: isShown(), nodes: nodes(), renders: renders()};
            """,
            lang=lang,
            state=backend["state"],
            api={"get_relation_graph_cache": backend["cold"]},
            before="__api.get_relation_graph = () => new Promise(resolve => setTimeout(() => resolve(GRAPH), 120));".replace(
                "GRAPH", json.dumps(backend["graph"])
            ),
        )
        out = result.value
        assert out["shown"] is True and out["nodes"] == ["Season 1", "Season 2"]
        assert out["views"] and set(out["views"]) == {"graph"}
        assert out["renders"] == 1  # drawn once: a late redraw would reset zoom and scroll
        assert result.calls_of("get_relation_graph") == [[20]]
