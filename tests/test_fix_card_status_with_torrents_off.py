"""A title's card with Torrents off says its download is paused, not "Shutdown not confirmed".

User (Rakugo card, "Shutdown not confirmed …"): "a strange status for an anime - that is the
torrent client's status, not the anime's?" The page counted an observed Off only while the
reading was under 15 s old, but with Torrents off the status comes every 30-120 s: between two
checks every card said "Shutdown not confirmed". An observed stop stays true until the next On;
and whether the stop is confirmed is shown on the Torrents button, not on titles.
"""

from __future__ import annotations

SCRIPT = r"""
const download = {state: 'downloading', progress: 0.85, torrent_hash: 'abc', hash: 'abc'};
ui.state.settings = {...(ui.state.settings || {}), torrents_enabled: false};
ui.torrentToggleDesired = false;
const at = seconds => Date.now() / 1000 - seconds;
const card = live => { ui.torrentTraffic = live; return compactDownloadStatus(download); };
const row = live => { ui.torrentTraffic = live; return torrentRowKind(download); };
const confirmed = age => ({enabled: false, transition: 'off_confirmed', stale: false, updated_at: at(age)});
const unconfirmed = {enabled: false, transition: 'unconfirmed', stale: true, updated_at: null};
return {
  cardFresh: card(confirmed(2)), cardOld: card(confirmed(90)), cardUnconfirmed: card(unconfirmed),
  rowOld: row(confirmed(90)), rowUnconfirmed: row(unconfirmed),
  paused: torrentUiLabel('paused', ui.lang), notConfirmed: t('ui.shutdownNotConfirmed'),
};
"""


def test_cards_say_paused_while_torrents_are_off(page) -> None:
    for lang in ("en", "ru"):
        out = page.run(SCRIPT, lang=lang).value
        expected = f"{out['paused']} · 85%"
        assert out["cardFresh"] == out["cardOld"] == out["cardUnconfirmed"] == expected, out
        assert out["notConfirmed"] not in out["cardUnconfirmed"]
        # The downloads window: a stop seen 90 s ago is still a stop; an unconfirmed one stays shown there.
        assert out["rowOld"] == "paused" and out["rowUnconfirmed"] == "unconfirmed"
