"""The watch-order window opens without flashing an empty or half-drawn modal (behaviour,
page harness; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi


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


def _node(media_id: int, title: str, year: int, status: str) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "format": "TV",
        "episodes": 12,
        "season_year": year,
        "list_status": status,
        "progress": 0,
        "watched": False,
    }


def watch_order_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Real backend answers for the window: Season 1 (watching) → Season 2 (planned); the
    graph AniList returns now and after a refresh (a third season appeared)."""
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    api.manager.db.upsert_anime(LibraryAnime(media_id=10, title="Season 1", status="CURRENT", episodes=12))
    api.manager.db.upsert_anime(LibraryAnime(media_id=20, title="Season 2", status="PLANNING", episodes=12))
    seasons = [_node(10, "Season 1", 2024, "CURRENT"), _node(20, "Season 2", 2025, "PLANNING")]
    edges = [{"source": 10, "target": 20, "relation_type": "SEQUEL"}]

    class AniList:
        def __init__(self, *_args, **_kwargs):
            pass

        def full_relation_graph(self, media_id: int) -> dict:
            return {
                "root_id": media_id,
                "nodes": list(seasons),
                "edges": list(edges),
                "truncated": False,
                "partial": False,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", AniList)

    def as_json(value):
        return json.loads(json.dumps(value, default=str))

    cold = as_json(api.get_relation_graph_cache())
    graph = as_json(api.get_relation_graph(20))
    cache = as_json(api.get_relation_graph_cache())
    seasons.append(_node(30, "Season 3", 2026, ""))
    edges.append({"source": 20, "target": 30, "relation_type": "SEQUEL"})
    refreshed = as_json(api.refresh_relation_graph(20))
    return {
        "state": as_json(api.get_state()),
        "cold": cold,
        "cache": cache,
        "graph": graph,
        "refreshed": refreshed,
    }


# Records what the modal shows after every change of the backdrop or its body (one
# observer, so the changes stay in order), then opens Season 2's watch order from Planning.
OPEN = r"""
const backdrop = $('modalBackdrop'), body = $('modalBody');
const records = [];
const html = nodes => [...nodes].map(n => n.outerHTML ?? n.textContent).join('');
const observer = new MutationObserver(list => { for (const r of list) records.push({
  on: r.target === backdrop ? 'backdrop' : 'body', name: r.attributeName, old: r.oldValue, removed: html(r.removedNodes)}); });
const timeline = () => {
  let cls = backdrop.getAttribute('class') || '', hidden = backdrop.hasAttribute('hidden'), content = body.innerHTML;
  const steps = [];
  for (let i = records.length - 1; i >= 0; i--) {
    const r = records[i];
    steps.unshift({shown: cls.split(/\s+/).includes('open') && !hidden, content});
    if (r.on === 'backdrop' && r.name === 'class') cls = r.old || '';
    else if (r.on === 'backdrop' && r.name === 'hidden') hidden = r.old !== null;
    else if (r.on === 'body') content = r.removed;
  }
  return steps;
};
const shownViews = () => timeline().filter(step => step.shown).map(step =>
  step.content.includes('full-relation-viewport') ? 'graph' : step.content.includes(t('label.loadingRelatedGraph')) ? 'loading' : 'empty');
const isShown = () => backdrop.classList.contains('open') && !backdrop.hidden;
const nodes = () => [...body.querySelectorAll('.relation-node')].map(node => node.dataset.title);
const renders = () => records.filter(r => r.on === 'body').length;
setPage('planned');
await __tick(20);
observer.observe(backdrop, {attributes: true, attributeOldValue: true, attributeFilter: ['class', 'hidden']});
observer.observe(body, {childList: true});
await __click('#plannedContent > [data-media-id="20"] .anime-title');
"""


def test_watch_order_renders_cached_graph_before_showing_modal(tmp_path: Path, monkeypatch, page) -> None:
    backend = watch_order_backend(tmp_path, monkeypatch)
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + """
            await __tick(100);
            await __flush();
            return {views: shownViews(), shown: isShown(), title: __text('#modalTitle'), renders: renders(),
                    expected: t('modal.watchOrder', {title: 'Season 2'}), nodes: nodes()};
            """,
            lang=lang,
            state=backend["state"],
            api={"get_relation_graph_cache": backend["cache"]},
        )
        out = result.value
        assert out["shown"] is True and out["title"] == out["expected"]
        assert out["views"] and set(out["views"]) == {"graph"}  # never empty, never "Loading…"
        assert out["nodes"] == ["Season 1", "Season 2"] and out["renders"] == 1
        assert not result.called("get_relation_graph")  # the start-up cache is enough


def test_uncached_watch_order_does_not_show_empty_modal_before_delay(
    tmp_path: Path, monkeypatch, page
) -> None:
    # AniList is slow: nothing for 200 ms, then the modal says it is loading, then the graph.
    backend = watch_order_backend(tmp_path, monkeypatch)
    result = page.run(
        OPEN
        + """
        await __tick(150);
        const early = isShown();
        await __tick(100);
        const waiting = {shown: isShown(), text: body.innerText, loading: t('label.loadingRelatedGraph')};
        answer();
        await __tick(20);
        await __flush();
        return {early, waiting, views: shownViews(), shown: isShown(), nodes: nodes()};
        """,
        state=backend["state"],
        api={"get_relation_graph_cache": backend["cold"]},
        before="__api.get_relation_graph = () => new Promise(resolve => { window.answer = () => resolve(GRAPH); });".replace(
            "GRAPH", json.dumps(backend["graph"])
        ),
    )
    out = result.value
    assert out["early"] is False
    assert out["waiting"]["shown"] is True and out["waiting"]["text"] == out["waiting"]["loading"]
    assert out["views"][0] == "loading" and out["views"][-1] == "graph" and "empty" not in out["views"]
    assert out["shown"] is True and out["nodes"] == ["Season 1", "Season 2"]
    assert result.calls_of("get_relation_graph") == [[20]]
