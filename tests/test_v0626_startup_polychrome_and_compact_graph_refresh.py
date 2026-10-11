"""Watching is redrawn only when Home changes; refreshing the full watch-order graph reloads
the compact Planning relations the backend rebuilt (behaviour, page harness; audit 71Б
rewrite). The foil shine on start-up is tested in test_v0653_energy_idle.py."""

from __future__ import annotations

import json

SHOW = {
    "media_id": 7,
    "title": "Show",
    "next_episode": 3,
    "next_airing_episode": 3,
    "remaining_seconds": 3600,
    "site_url": "https://anilist.co/anime/7",
}
# The backend's Home is window.__home; every bump() is a library change the window picks
# up from the state versions within a second.
HOME_UPDATES = r"""
window.__home = {waiting: [SHOW]};
window.__versions = {ready: '1', ui: '1'};
const base = __api.get_state;
__api.ui_state_versions = async () => window.__versions;
__api.get_state = __api.get_state_fast = async () => ({...(await base()), ui_state_version: window.__versions.ui,
                                                       home: {...(await base()).home, ...window.__home}});
""".replace("SHOW", json.dumps(SHOW))


def test_state_updates_do_not_rebuild_watching_when_home_is_unchanged(page) -> None:
    # Only the countdowns moved (time to the next episode, to the deletion): Watching is not
    # rebuilt (a rebuild restarts the cover effects and costs energy on every poll).
    result = page.run(
        r"""
        await __tick(3000);
        let redraws = 0;
        const draw = renderCurrent;
        window.renderCurrent = (...args) => { redraws++; return draw(...args); };
        const bump = async home => { window.__home = home; window.__versions = {ready: '1', ui: String(Number(window.__versions.ui) + 1)};
                                     await __tick(1000); return redraws; };
        const show = SHOW;
        const settled = await bump({waiting: [show]});
        const countdown = await bump({waiting: [{...show, remaining_seconds: 3400, delete_remaining_seconds: 90}]}) - settled;
        const changed = await bump({waiting: [{...show, title: 'Renamed', remaining_seconds: 3300}]}) - settled;
        return {countdown, changed, text: __text('#currentContent')};
        """.replace("SHOW", json.dumps(SHOW)),
        before=HOME_UPDATES,
    )
    assert result.value["countdown"] == 0
    assert result.value["changed"] == 1 and "Renamed" in result.value["text"]


def _node(media_id: int, title: str, year: int) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "format": "TV",
        "season_year": year,
        "site_url": f"https://anilist.co/anime/{media_id}",
    }


# Planning shows Season 1 with its compact relations (Season 2). AniList now has a Season 2
# Part 2: refreshing the full graph makes the backend rebuild the compact relations of every
# member; window.__stateFails makes reading that state fail.
PLANNED = [
    {
        **_node(1, "Season 1", 2019),
        "list_status": "PLANNING",
        "relations": {
            "current": _node(1, "Season 1", 2019),
            "prequel_levels": [],
            "sequel_levels": [[_node(3, "Season 2", 2021)]],
            "full_prequel_levels": [],
            "full_sequel_levels": [[_node(3, "Season 2", 2021)]],
            "order_mode": "anilist",
        },
    }
]
AFTER_REFRESH = r"""
const node = (id, title, year) => ({media_id: id, title, format: 'TV', season_year: year, site_url: `https://anilist.co/anime/${id}`});
const base = __api.get_state_fast;
__api.get_state_fast = async () => {
  if (!window.__refreshed) return base();
  if (window.__stateFails) throw new Error('busy');
  const state = await base();
  state.planned[0].relations.sequel_levels = [[node(3, 'Season 2', 2021), node(4, 'Season 2 Part 2', 2022)]];
  return state;
};
const graph = refreshed => ({root_id: 1, nodes: [node(1, 'Season 1', 2019), node(3, 'Season 2', 2021), ...(refreshed ? [node(4, 'Season 2 Part 2', 2022)] : [])],
  edges: [{source: 1, target: 3, relation_type: 'SEQUEL'}, ...(refreshed ? [{source: 1, target: 4, relation_type: 'SEQUEL'}] : [])]});
__api.get_relation_graph = async () => graph(false);
__api.refresh_relation_graph = async () => { window.__refreshed = true; return graph(true); };
"""


def test_full_graph_refresh_updates_the_compact_planning_relations(page) -> None:
    for fails in (False, True):
        result = page.run(
            r"""
            setPage('planned'); await __tick(50);
            const related = () => [...document.querySelectorAll('#plannedContent [data-relation-node="1"]')].map(node => node.dataset.title);
            const before = related();
            await openWatchOrder(1); await __tick(300);
            await __click('#modalBody [data-action="refresh-watch-order"]'); await __tick(50);
            return {before, after: related(), toasts: __toasts(), refreshed: t('toast.watchOrderRefreshed')};
            """,
            state={"planned": PLANNED},
            before=f"window.__stateFails = {str(fails).lower()};" + AFTER_REFRESH,
        )
        out = result.value
        assert out["before"] == ["Season 1", "Season 2"]
        # The backend's rebuilt relations (or, when they cannot be read, the refreshed graph).
        assert out["after"] == ["Season 1", "Season 2", "Season 2 Part 2"], fails
        assert out["toasts"] == [out["refreshed"]]
        assert result.calls_of("refresh_relation_graph") == [[1]]
