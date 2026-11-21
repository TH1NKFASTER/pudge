"""LOWRAM: the window page in Low RAM mode, and unchanged with the mode off.

On: html.low-ram (off-screen cards skip layout/paint), Light novels drop their DOM a minute
after you leave them and are built again on return, Planning goes after a minute instead of
five, AniList covers load at list size and a stored animated original yields to its static
list copy, the foil covers never start their motion. The page tells Python why it must stay
(an episode followed in mpv). Settings carries the checkbox.
"""

from __future__ import annotations

import pytest

BOOK = {
    "id": 4,
    "title": "Novel",
    "volume": 1,
    "anilist_id": None,
    "cover_url": "covers/ln-abc.jpg.list.jpg",
    "remote_cover_url": "",
    "character_count": 1000,
}
SHOW = {
    "media_id": 7,
    "title": "Show",
    "episode": 3,
    "video_path": "/library/Show - 03.mkv",
    "cover": "https://img.example/7.jpg",
}
ANILIST = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx7-abc.jpg"

PAGE = r"""
const cards = () => document.querySelectorAll('#lightNovelsContent [data-ln-book]').length;
const shines = () => Number(document.querySelector('#currentContent .cover-shell.polychrome')?.dataset.polychromeToken || 0);
await __tick(3000);
const out = {lowRam: document.documentElement.classList.contains('low-ram')};
out.sources = pudgeCoverSources(ANILIST);
out.gif = pudgeCoverSources('http://127.0.0.1/k/x/covers/ln-abc.gif');
await __click('.nav button[data-page="planned"]'); await __tick(100);
await __click('.nav button[data-page="current"]'); await __tick(100);   // a return to Watching starts a shine
out.shineOnReturn = shines();
await __tick(3000);
await __fire('#currentContent .cover-shell.polychrome', 'pointerover', {}, 'PointerEvent'); await __tick(100);
out.shineOnHover = shines();
setPage('lightnovels'); await __tick(100);
out.shown = cards();
setPage('current'); await __tick(61 * 1000);
out.afterMinute = cards();
setPage('lightnovels'); await __tick(100);
out.back = cards();
out.veto = window.PudgeLowRam.unloadVeto();
ui.playMonitors.set('/library/Show - 03.mkv', Promise.resolve());
out.vetoPlaying = window.PudgeLowRam.unloadVeto();
ui.playMonitors.clear();
return out;
""".replace("ANILIST", repr(ANILIST))


def _run(page, on: bool):
    return page.run(
        PAGE,
        state={"settings": {"low_ram_mode": on}, "home": {"continue_watching": [SHOW]}},
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [BOOK]}},
    ).value


def test_low_ram_mode_renders_lighter(page) -> None:
    out = _run(page, True)
    assert out["lowRam"] is True
    assert out["sources"] == [ANILIST.replace("/cover/large/", "/cover/medium/"), ANILIST]
    assert out["gif"] == [
        "http://127.0.0.1/k/x/covers/ln-abc.gif.list.jpg",
        "http://127.0.0.1/k/x/covers/ln-abc.gif",
    ]
    assert out["shineOnReturn"] == 0 and out["shineOnHover"] == 0  # covers stay still
    assert out["shown"] == 1 and out["afterMinute"] == 0 and out["back"] == 1
    assert out["veto"] == "" and out["vetoPlaying"] == "play_monitor"


def test_mode_off_leaves_the_page_as_it_was(page) -> None:
    out = _run(page, False)
    assert out["lowRam"] is False
    assert out["sources"] == [ANILIST]
    assert out["gif"] == ["http://127.0.0.1/k/x/covers/ln-abc.gif"]
    assert out["shineOnReturn"] >= 1 and out["shineOnHover"] == out["shineOnReturn"] + 1
    assert out["shown"] == 1 and out["afterMinute"] == 1 and out["back"] == 1


@pytest.mark.parametrize("on", [True, False])
def test_planning_goes_after_a_minute_only_in_low_ram_mode(page, on) -> None:
    from test_v0552_planning_relations import planning_relations_state
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        state = planning_relations_state(Path(folder))
    state.setdefault("settings", {})["low_ram_mode"] = on
    result = page.run(
        r"""
        const cards = () => document.querySelectorAll('#plannedContent > [data-media-id]').length;
        setPage('planned'); await __tick(20);
        const shown = cards();
        setPage('current'); await __tick(61 * 1000);
        return {shown, later: cards()};
        """,
        state=state,
    )
    assert result.value["shown"] == 2
    assert result.value["later"] == (0 if on else 2)


def test_settings_checkbox_round_trips(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(200);
        const box = document.getElementById('s_low_ram_mode');
        const filled = !!box?.checked;
        box.checked = false;
        return {filled, saved: collectSettings().low_ram_mode, label: box.parentElement.innerText};
        """,
        lang="ru",
        state={"settings": {"low_ram_mode": True}},
    )
    assert result.value["filled"] is True and result.value["saved"] is False
    assert "экономии памяти" in result.value["label"]


def test_statistics_drops_its_page_after_a_minute_and_loads_again(page) -> None:
    result = page.run(
        r"""
        const built = () => !!document.querySelector('#statisticsContent .stats-shell');
        setPage('statistics'); await __tick(500);
        const shown = built();
        setPage('current'); await __tick(61 * 1000);
        const later = built(), payload = window.PudgeStatistics.state.payload;
        setPage('statistics'); await __tick(500);
        return {shown, later, payload, back: built(), queries: __apiCalls.filter(c => c.name === 'consumption_statistics_query').length};
        """,
        state={"settings": {"low_ram_mode": True}},
    )
    out = result.value
    assert out["shown"] is True and out["later"] is False and out["payload"] is None
    assert out["back"] is True and out["queries"] == 2
