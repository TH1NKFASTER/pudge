"""The torrent switch says only "Torrents: off" once the shutdown is confirmed (behaviour,
page harness; audit 71Б rewrite)."""

from __future__ import annotations

# /api/torrents/status answers with window.__status (the switch polls it over HTTP).
STATUS = """
window.__status = {ok: true, enabled: false, transition: 'unconfirmed'};
window.fetch = async url => { __fetches.push({url: String(url)}); return new Response(JSON.stringify(window.__status), {status: 200}); };
"""
BUTTON = (
    "[...document.querySelectorAll('#torrentToggleButton > *')].map(node => [node.tagName, node.textContent])"
)


def test_confirmed_torrent_off_button_has_no_redundant_second_line(page) -> None:
    for lang in ("en", "ru"):
        before = page.run(
            f"return {{lines: {BUTTON}, off: t('ui.torrentsOff2')}};", lang=lang, ready=False
        ).value
        assert before["lines"] == [["SPAN", before["off"]]]  # what the window shows before it starts
        result = page.run(
            f"""
            const button = () => {BUTTON};
            await __tick(300);
            const unconfirmed = button();
            window.__status = {{ok: true, enabled: false, transition: 'off_confirmed'}};
            await __tick(7000);
            return {{unconfirmed, confirmed: button(), off: t('ui.torrentsOff2'), notConfirmed: t('ui.shutdownNotConfirmed'),
                     polled: __fetches.filter(f => f.url === '/api/torrents/status').length}};
            """,
            lang=lang,
            before=STATUS,
        )
        out = result.value
        assert out["polled"] >= 2
        assert out["unconfirmed"] == [["SPAN", out["off"]], ["SMALL", out["notConfirmed"]]]
        assert out["confirmed"] == [["SPAN", out["off"]]]
