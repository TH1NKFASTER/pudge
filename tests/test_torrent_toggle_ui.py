"""The sidebar torrent switch (behaviour, page harness)."""

from __future__ import annotations

# The local HTTP endpoints of the switch: POST /api/torrents/enabled, GET /api/torrents/status.
FETCH = """
let torrentsOn = false;
window.fetch = async (url, init) => {
  __fetches.push({url: String(url), method: init?.method || 'GET', body: init?.body ?? null});
  if (String(url).endsWith('/api/torrents/enabled')) {
    torrentsOn = JSON.parse(init.body).enabled;
    return new Response(JSON.stringify({ok: true, enabled: torrentsOn, transition: torrentsOn ? 'on' : 'off_requested'}), {status: 200});
  }
  return new Response(JSON.stringify({ok: true, enabled: torrentsOn, desired_enabled: torrentsOn}), {status: 200});
};
"""


def test_turning_torrents_on_and_off_shows_no_interface_error(page) -> None:
    # The Downloads page is gone; the switch used to re-render it and failed with
    # "null is not an object (evaluating $('downloadsContent').innerHTML ...)".
    for lang in ("en", "ru"):
        result = page.run(
            """
            await __click('#torrentToggleButton');
            await __tick(10);
            const on = {toast: __text('#toast'), enabled: ui.state.settings.torrents_enabled};
            await __click('#torrentToggleButton');
            await __tick(10);
            return {on, off: {toast: __text('#toast'), enabled: ui.state.settings.torrents_enabled},
                    posts: __fetches.filter(f => f.method === 'POST')};
            """,
            lang=lang,
            before=FETCH,
        )
        on, off = result.value["on"], result.value["off"]
        assert on["enabled"] is True and off["enabled"] is False
        assert on["toast"] == page.run("return t('ui.torrentsEnabled')", lang=lang).value
        assert off["toast"].endswith(
            page.run("return t('ui.torrentShutdownRequested')", lang=lang).value
        )  # toasts queue up
        assert [post["body"] for post in result.value["posts"]] == ['{"enabled":true}', '{"enabled":false}']
        assert all(post["url"] == "/api/torrents/enabled" for post in result.value["posts"])
