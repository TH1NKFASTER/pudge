"""Torrent Off is confirmed within seconds, not minutes (behaviour).

Right after Pudge starts (or Off is clicked) aria2 can still be exiting, so the first check
says Off is not confirmed. The switch then showed "Shutdown not confirmed" until the next check,
which the unreachable-client backoff and the push interval put a minute or more later."""

from __future__ import annotations


def test_unconfirmed_off_is_checked_again_within_seconds(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            window.__answers = 0;
            window.fetch = async () => {
              window.__answers += 1;
              const confirmed = window.__answers > 2;  // aria2 gone after the second check
              return new Response(JSON.stringify({ok: true, enabled: false, desired_enabled: false,
                transition: confirmed ? 'off_confirmed' : 'unconfirmed', stale: !confirmed,
                updated_at: confirmed ? Date.now() / 1000 : null}));
            };
            ui.pushReady = true;  // the push channel is up, as in the app
            scheduleTorrentTrafficPoll(0); await __tick(50);
            const first = __text('#torrentToggleButton');
            await __tick(8000);
            return {first, after: __text('#torrentToggleButton'), polls: window.__answers,
                    unconfirmed: t('ui.shutdownNotConfirmed')};
            """,
            lang=lang,
            state={"settings": {"torrents_enabled": False}},
        )
        out = result.value
        assert out["unconfirmed"] in out["first"]
        assert out["unconfirmed"] not in out["after"], out
        assert out["polls"] <= 5  # a few checks, not a busy loop
