"""An open but idle window calls into Pudge much less (Low RAM work, MEM7).

Measured with the page harness over one fake hour, push live, nothing playing or downloading
(before → after, bridge calls + HTTP torrent fetches):
focused 547+60 → 379+60, visible but unfocused 366+60 → ~41+12, hidden 120+60 → ~18+12.
"""

from __future__ import annotations

HOUR = r"""
const counts = () => { const m = {}; for (const c of __apiCalls) m[c.name] = (m[c.name]||0)+1; return m; };
const diff = (a, b) => { const d = {}; for (const k of Object.keys(b)) { const v = b[k] - (a[k]||0); if (v) d[k] = v; } return d; };
const hour = async () => { const a = counts(), f = __fetches.length; for (let i = 0; i < 60; i++) await __tick(60000); return {calls: diff(a, counts()), fetches: __fetches.length - f}; };
"""
PUSH = {"ui_push_ready": {"ok": True, "topics": ["versions", "torrents", "audio"]}}


def _total(calls: dict) -> int:
    return sum(calls.values())


def test_an_unfocused_or_hidden_window_polls_rarely(page) -> None:
    result = page.run(
        HOUR
        + r"""
        await __tick(5000);
        const focused = await hour();
        await __setFocused(false); await __tick(1000);
        const unfocused = await hour();
        await __setHidden(true); await __tick(1000);
        const hidden = await hour();
        return {focused, unfocused, hidden};
        """,
        api=PUSH,
    )
    value = result.value
    assert _total(value["focused"]["calls"]) <= 420  # was 547
    unfocused, hidden = value["unfocused"], value["hidden"]
    assert _total(unfocused["calls"]) <= 60, unfocused  # was 366
    assert unfocused["fetches"] <= 13  # torrent fallback: was 60
    for name in (
        "poll_planned_release_discovery",
        "poll_anilist_related_media_notifications",
        "review_gate_prefetch",
        "ui_state_versions",
    ):
        assert name not in unfocused["calls"], name
    assert unfocused["calls"].get("power_status", 0) <= 12  # was 60
    assert unfocused["calls"].get("sidebar_audio_state", 0) <= 12  # was 60
    assert unfocused["calls"].get("sidebar_due_review_cards", 0) <= 13  # was 60
    assert _total(hidden["calls"]) <= 25 and hidden["fetches"] <= 13, hidden  # was 120 + 60
    # AniList additions: Python answers from a 5-minute cache, so the page asks every 5 minutes.
    assert value["focused"]["calls"].get("poll_anilist_related_media_notifications", 0) <= 13


def test_focus_runs_the_waiting_polls_and_rereads_state_only_when_it_changed(page) -> None:
    result = page.run(
        HOUR
        + r"""
        await __tick(5000);
        await __setFocused(false); await __tick(1000);
        for (let i = 0; i < 10; i++) await __tick(60000);
        const count = name => __apiCalls.filter(call => call.name === name).length;
        const before = {fast: count('get_state_fast'), offers: count('poll_planned_release_discovery'),
                        related: count('poll_anilist_related_media_notifications'), audio: count('sidebar_audio_state'),
                        versions: count('ui_state_versions')};
        await __setFocused(true); await __tick(2000);
        const first = {fast: count('get_state_fast') - before.fast, offers: count('poll_planned_release_discovery') - before.offers,
                       related: count('poll_anilist_related_media_notifications') - before.related,
                       audio: count('sidebar_audio_state') - before.audio, versions: count('ui_state_versions') - before.versions};
        // A quick second focus with nothing changed: versions only, no full state.
        await __setFocused(false); await __tick(1000);
        const fast = count('get_state_fast'), versions = count('ui_state_versions');
        await __setFocused(true); await __tick(2000);
        return {first, again: {fast: count('get_state_fast') - fast, versions: count('ui_state_versions') - versions}};
        """,
        api=PUSH,
    )
    first, again = result.value["first"], result.value["again"]
    assert first["offers"] == 1 and first["related"] == 1 and first["audio"] >= 1
    assert first["fast"] == 1  # the first focus in 5 minutes re-reads everything, as before
    assert again == {"fast": 0, "versions": 1}
