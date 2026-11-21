"""MEM: the window page lets go of what a hidden page does not need.

Planning kept a card (cover, relation covers, Jiten chips) for every planned title for the
whole run, also while another page was open; its cards now go after five minutes away and come
back when Planning is opened again. Cover images load lazily and decode off the main thread.
"""

from __future__ import annotations

from pathlib import Path

from test_v0552_planning_relations import planning_relations_state

CARDS = "() => [...document.querySelectorAll('#plannedContent > [data-media-id]')].map(n => Number(n.dataset.mediaId))"


def test_hidden_planning_releases_its_cards_and_rebuilds_them(tmp_path: Path, page) -> None:
    state = planning_relations_state(tmp_path)
    result = page.run(
        f"""
        const cards = {CARDS};
        setPage('planned');
        await __tick(20);
        const out = {{shown: cards()}};
        setPage('current');
        await __tick(4 * 60 * 1000);
        out.after4min = cards();
        setPage('planned');                    // back before the five minutes: kept, timer cancelled
        await __tick(20);
        setPage('current');
        await __tick(4 * 60 * 1000);
        out.restarted = cards();
        await __tick(61 * 1000);
        out.released = cards();
        out.nodes = ui.plannedNodes.size;
        setPage('planned');
        await __tick(20);
        out.rebuilt = cards();
        out.relation = !!document.querySelector('#plannedContent .relation-node[data-media-id="90"]');
        await __tick(10 * 60 * 1000);          // on Planning: never released
        out.stays = cards();
        return out;
        """,
        lang="en",
        state=state,
    )
    out = result.value
    assert sorted(out["shown"]) == [100, 200]
    assert sorted(out["after4min"]) == [100, 200]
    assert sorted(out["restarted"]) == [100, 200]
    assert out["released"] == [] and out["nodes"] == 0
    assert sorted(out["rebuilt"]) == [100, 200] and out["relation"] is True
    assert sorted(out["stays"]) == [100, 200]


def test_cover_images_load_lazily_and_decode_async(tmp_path: Path, page) -> None:
    state = planning_relations_state(tmp_path)
    result = page.run(
        r"""
        setPage('planned');
        await __tick(20);
        const relation = document.querySelector('#plannedContent img.relation-node-cover');
        const home = document.createElement('div');
        home.innerHTML = cover({cover: 'http://127.0.0.1/covers/1.jpg.list.jpg'}, 'airing-cover');
        const img = home.querySelector('img');
        return {relation: [relation?.getAttribute('loading'), relation?.getAttribute('decoding')],
                airing: [img?.getAttribute('loading'), img?.getAttribute('decoding')]};
        """,
        lang="en",
        state=state,
    )
    assert result.value == {"relation": ["lazy", "async"], "airing": ["lazy", "async"]}
