"""The large watch-order graph (behaviour, page harness + stylesheet; audit 71Б rewrite):
entries stand left to right in release order across all branches; the graph zooms with a
trackpad pinch (Ctrl/Cmd/Alt + wheel) and the −/100%/+ buttons, pans by dragging; relation
labels are readable text on a background."""

from __future__ import annotations

from css_support import css_rules

# Season 1 → Season 2 is the main line; a spin-off of Season 1 and a special of Season 2
# came out between them.
GRAPH = {
    "root_id": 1,
    "nodes": [
        {"media_id": 1, "title": "Season 1", "format": "TV", "season_year": 2019},
        {"media_id": 3, "title": "Season 2", "format": "TV", "season_year": 2022},
        {"media_id": 4, "title": "Spin-off", "format": "TV", "season_year": 2020},
        {"media_id": 5, "title": "Special", "format": "SPECIAL", "season_year": 2021},
    ],
    "edges": [
        {"source": 1, "target": 3, "relation_type": "SEQUEL"},
        {"source": 1, "target": 4, "relation_type": "SPIN_OFF"},
        {"source": 3, "target": 5, "relation_type": "SIDE_STORY"},
    ],
}
OPEN = {
    "state": {"planned": [{"media_id": 1, "title": "Season 1", "format": "TV", "season_year": 2019}]},
    "api": {"get_relation_graph": GRAPH},
}


def test_large_relation_graph_places_every_entry_on_one_release_timeline(page) -> None:
    result = page.run(
        r"""
        await openWatchOrder(1); await __tick(300);
        const nodes = [...document.querySelectorAll('#modalBody .full-relation-node')].map(node =>
          ({title: node.querySelector('[data-title]').dataset.title, x: parseInt(node.style.left), y: parseInt(node.style.top),
            main: node.classList.contains('backbone')}));
        const labels = [...document.querySelectorAll('#modalBody .full-relation-edge-label-group')].map(group =>
          ({background: Boolean(group.querySelector('rect.full-relation-edge-label-bg')), text: group.querySelector('text.full-relation-edge-label').textContent}));
        return {nodes, labels, expected: [relationTypeLabel('SPIN_OFF'), relationTypeLabel('SIDE_STORY')]};
        """,
        **OPEN,
    )
    nodes = sorted(result.value["nodes"], key=lambda node: node["x"])
    # Release order from left to right, one column per entry, branches included (a branch
    # used to sit next to the entry it hangs from, out of order).
    assert [node["title"] for node in nodes] == ["Season 1", "Spin-off", "Special", "Season 2"]
    steps = {later["x"] - earlier["x"] for earlier, later in zip(nodes, nodes[1:])}
    assert len(steps) == 1 and steps.pop() > 172  # wider than a node: no overlap
    main = [node for node in nodes if node["main"]]
    assert [node["title"] for node in main] == ["Season 1", "Season 2"] and len(
        {node["y"] for node in main}
    ) == 1
    assert all(node["y"] != main[0]["y"] for node in nodes if not node["main"])
    # The relation names of the side lines stand on a background box.
    assert result.value["labels"] == [{"background": True, "text": text} for text in result.value["expected"]]


def test_large_relation_graph_zooms_with_pinch_and_buttons_and_pans_by_dragging(page) -> None:
    result = page.run(
        r"""
        await openWatchOrder(1); await __tick(300);
        const viewport = document.querySelector('#modalBody .full-relation-viewport');
        const canvas = viewport.querySelector('.full-relation-canvas');
        const zoom = () => [__text('#modalBody [data-relation-zoom-value]'), canvas.style.transform];
        const button = async which => { await __click(`#modalBody [data-relation-zoom="${which}"]`); return zoom(); };
        const wheel = init => { const event = new WheelEvent('wheel', {deltaY: -100, clientX: 0, clientY: 0, cancelable: true, bubbles: true, ...init});
                                viewport.dispatchEvent(event); return {zoom: zoom(), prevented: event.defaultPrevented}; };
        const out = {start: zoom(), in: await button('in'), out: (await button('out'), await button('out')), reset: await button('reset')};
        out.scroll = wheel({});                          // plain scrolling is left alone
        out.pinch = wheel({ctrlKey: true});              // trackpad pinch
        out.option = (await button('reset'), wheel({altKey: true, deltaY: 100}));
        for (let i = 0; i < 12; i++) await button('in');
        out.max = zoom();
        for (let i = 0; i < 12; i++) await button('out');
        out.min = zoom();
        await button('reset');
        viewport.scrollLeft = 300; viewport.scrollTop = 100;
        await __fire('#modalBody .full-relation-viewport', 'pointerdown', {button: 0, clientX: 500, clientY: 400, pointerId: 1, bubbles: true}, 'PointerEvent');
        await __fire('#modalBody .full-relation-viewport', 'pointermove', {clientX: 380, clientY: 350, pointerId: 1, bubbles: true}, 'PointerEvent');
        await __fire('#modalBody .full-relation-viewport', 'pointerup', {clientX: 380, clientY: 350, pointerId: 1, bubbles: true}, 'PointerEvent');
        out.dragged = [viewport.scrollLeft, viewport.scrollTop];
        await __fire('#modalBody .full-relation-viewport', 'pointermove', {clientX: 0, clientY: 0, pointerId: 1, bubbles: true}, 'PointerEvent');
        out.released = [viewport.scrollLeft, viewport.scrollTop];
        out.labels = [...document.querySelectorAll('#modalBody [data-relation-zoom]')].map(b => b.getAttribute('aria-label'));
        out.expectedLabels = [t('uiC.watchOrder.zoomOut'), null, t('uiC.watchOrder.zoomIn')];
        return out;
        """,
        **OPEN,
    )
    out = result.value
    assert out["start"] == ["100%", "scale(1)"] and out["reset"] == out["start"]
    assert out["in"] == ["120%", "scale(1.2)"]
    assert out["out"][0] == "83%"
    assert out["scroll"] == {"zoom": out["start"], "prevented": False}
    assert out["pinch"]["zoom"][0] == "128%" and out["pinch"]["prevented"] is True  # exp(0.25)
    assert out["option"]["zoom"][0] == "78%" and out["option"]["prevented"] is True
    assert out["max"] == ["220%", "scale(2.2)"] and out["min"] == ["45%", "scale(0.45)"]
    assert out["dragged"] == [420, 150] and out["released"] == out["dragged"]
    assert out["labels"] == out["expectedLabels"]


def test_relation_labels_are_crisp_readable_text() -> None:
    rules = css_rules("index.html")
    label = rules.declarations(".full-relation-edge-label")
    assert (label["font-size"], label["fill"]) == ("11px", "#d5e4f8")
    assert rules.get(".full-relation-edge-label-bg", "fill") == "#0b1522"
    svg = rules.declarations(".full-relation-svg")
    assert svg["shape-rendering"] == svg["text-rendering"] == "geometricPrecision"
