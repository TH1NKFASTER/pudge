"""DEV: the library shows what is downloaded on a paired iPhone/iPad (behaviour, page harness).

Anime cards get a small icon in the cover's top-left corner (the card menu is top-right);
light-novel, manga and audiobook cards get it in their title line, beside the light novel's
audiobook badge; rows inside a title (a series' volumes, Planning's episode buttons) too. The
usual tooltip names the devices from pairing ("On iPad · iPhone", no counts); a report older than a
week says "as of <date>". New reports update the icons in place (UiPush topic "devices").
"""

from __future__ import annotations

import json

import pytest

NOW = 1790856000000  # 2026-10-01 12:00 UTC, the harness clock
PAD, PHONE = "pad-1", "phone-1"
DEVICES = [
    {"id": PAD, "name": "iPad", "idiom": "pad", "reportedAt": NOW, "stale": False},
    {"id": PHONE, "name": "iPhone", "idiom": "phone", "reportedAt": NOW, "stale": False},
]
COPIES = {
    "devices": DEVICES,
    "anime": {"101": {PAD: [1, 2, 3], PHONE: [1]}, "103": {PHONE: [2]}},
    "ln": {"5": [PAD], "6": [PAD, PHONE]},
    "manga": {"8": [PHONE]},
    "audiobooks": {"9": [PAD]},
}


def _anime(media_id, title):
    return {
        "media_id": media_id,
        "title": title,
        "site_url": f"https://anilist.co/anime/{media_id}",
        "cover": "",
    }


HOME = {"home": {"caught_up": [_anime(101, "Frieren"), _anime(102, "Nothing here")]}}

LOOK = r"""
window.__look = sel => {
  const n = document.querySelector(sel);
  return n ? {hidden: n.hidden, tip: n.dataset.tooltip ?? null, glyphs: n.querySelectorAll('svg').length,
              stale: n.classList.contains('stale')} : null;
};
"""


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("en", "On iPad · iPhone"), ("ru", "На iPad · iPhone")],
)
def test_anime_card_has_the_icon_in_its_top_left_corner_with_devices_on_hover(page, lang, expected) -> None:
    result = page.run(
        LOOK
        + r"""
        await __tick(100);
        const card = '#currentContent article[data-media-id="101"]';
        const out = {a: __look(card + ' > .device-copies-corner'), b: __look('#currentContent article[data-media-id="102"] > .device-copies-corner'),
                     menu: Boolean(document.querySelector(card + ' > .card-menu-button'))};
        await __fire(card + ' > .device-copies-corner', 'mouseover', {}, 'MouseEvent');
        const tip = document.getElementById('titleTooltip');
        out.tooltip = tip.classList.contains('show') ? tip.innerText : null;
        return out;
        """,
        lang=lang,
        state=HOME,
        api={"iphone_device_copies": COPIES},
    )
    out = result.value
    # An iPad and an iPhone: both glyphs.
    assert out["a"] == {"hidden": False, "tip": expected, "glyphs": 2, "stale": False}
    assert out["tooltip"] == expected
    assert out["menu"] is True
    # Nothing of it on a device: an empty, hidden slot.
    assert out["b"] == {"hidden": True, "tip": None, "glyphs": 0, "stale": False}
    assert result.calls_of("iphone_device_copies")


def test_an_old_report_says_as_of_and_new_reports_update_the_cards_in_place(page) -> None:
    stale = {
        **COPIES,
        "devices": [{**DEVICES[0], "stale": True, "reportedAt": NOW - 9 * 86400000}, DEVICES[1]],
    }
    result = page.run(
        LOOK
        + r"""
        await __tick(100);
        const sel = '#currentContent article[data-media-id="101"] > .device-copies-corner';
        const card = document.querySelector('#currentContent article[data-media-id="101"]');
        const date = new Date(window.STALE.devices[0].reportedAt).toLocaleDateString('en-US', {day: 'numeric', month: 'short'});
        const out = {first: __look(sel), date};
        // The iPhone deleted its episode, the iPad reported long ago.
        window.ANSWER = {...window.STALE, anime: {'101': {'pad-1': [1, 2, 3]}}};
        PudgePush('devices', {at: 1}); await __tick(50);
        out.pushed = __look(sel);
        out.sameCard = card === document.querySelector('#currentContent article[data-media-id="101"]');
        // And then nothing is on a device any more.
        window.ANSWER = {...window.STALE, anime: {}};
        PudgePush('devices', {at: 2}); await __tick(50);
        out.gone = __look(sel);
        // A redraw of Home with the latest answer agrees.
        renderCurrent();
        out.redrawn = __look(sel);
        return out;
        """,
        state=HOME,
        before="window.STALE = " + json.dumps(stale) + "; window.ANSWER = window.STALE;"
        "__api.iphone_device_copies = async () => JSON.parse(JSON.stringify(window.ANSWER));",
    )
    out = result.value
    assert out["first"]["tip"] == f"On iPad (as of {out['date']}) · iPhone"
    assert out["first"]["stale"] is False  # one device is current
    assert out["pushed"] == {
        "hidden": False,
        "tip": f"On iPad (as of {out['date']})",
        "glyphs": 1,
        "stale": True,
    }
    assert out["sameCard"] is True
    assert out["gone"] == {"hidden": True, "tip": None, "glyphs": 0, "stale": False}
    assert out["redrawn"] == out["gone"]


def test_a_watch_sequence_card_counts_every_entry(page) -> None:
    sequence = {
        "kind": "watch_sequence",
        "title": "Frieren",
        "items": [
            {**_anime(101, "Frieren"), "local": {"video_path": "/v/1.mkv"}, "ready_episodes": [1]},
            {**_anime(103, "Frieren 2"), "local": {"video_path": "/v/2.mkv"}, "ready_episodes": [2]},
        ],
    }
    result = page.run(
        LOOK
        + "await __tick(100); return __look('#currentContent .ready-sequence-card > .device-copies-corner');",
        state={"home": {"new_ready": [sequence]}},
        api={"iphone_device_copies": COPIES},
    )
    assert result.value["tip"] == "On iPad · iPhone"


def test_light_novel_cards_and_volume_rows_show_it_beside_the_audiobook_badge(page) -> None:
    books = [
        {"id": 5, "title": "Alone", "volume": 1, "series_key": "alone", "paired_audio": {"book": {"id": 9}}},
        {"id": 6, "title": "Tower 1", "volume": 1, "series_key": "tower", "series_title": "Tower"},
        {"id": 7, "title": "Tower 2", "volume": 2, "series_key": "tower", "series_title": "Tower"},
    ]
    result = page.run(
        LOOK
        + r"""
        setPage('lightnovels', true); await __tick(100);
        const root = '#lightNovelsContent ';
        const badge = document.querySelector(root + '[data-ln-book="5"] h3 .ln-paired-audio-badge');
        return {
          single: __look(root + '[data-ln-book="5"] h3 > .device-copies'),
          afterBadge: badge?.nextElementSibling?.classList.contains('device-copies') ?? false,
          series: __look(root + '.ln-series-head .device-copies'),
          row6: __look(root + '.ln-series-books [data-ln-book="6"] h3 > .device-copies-row'),
          row7: __look(root + '.ln-series-books [data-ln-book="7"] h3 > .device-copies-row'),
          onCover: document.querySelectorAll(root + '.ln-card-cover .device-copies').length,
        };
        """,
        api={
            "light_novel_state": {"version": 1, "settings": {}, "books": books},
            "iphone_device_copies": COPIES,
        },
    )
    out = result.value
    assert out["single"]["tip"] == "On iPad" and out["single"]["glyphs"] == 1
    assert out["afterBadge"] is True
    assert out["series"]["tip"] == "On iPad · iPhone"
    assert out["row6"]["tip"] == "On iPad · iPhone" and out["row6"]["glyphs"] == 2
    assert out["row7"]["hidden"] is True
    assert out["onCover"] == 0


def test_manga_and_audiobook_cards_show_it_in_their_title_line(page) -> None:
    manga = {
        "books": [{"id": 8, "title": "Yotsuba", "page_count": 20, "read_pages": 3}],
        "ocr_available": False,
    }
    book = {"id": 9, "title": "Book", "duration": 600, "position": 0, "chapters": []}
    result = page.run(
        LOOK
        + r"""
        setPage('manga', true); await __tick(300);
        const out = {manga: __look('#mangaContent [data-manga-book="8"] h3 > .device-copies')};
        setPage('audiobooks', true); await __tick(500);
        out.audio = __look('.audiobook-card[data-audiobook-id="9"] .audiobook-title-row > .device-copies');
        return out;
        """,
        lang="ru",
        api={"manga_state": manga, "audiobook_state": {"books": [book]}, "iphone_device_copies": COPIES},
    )
    out = result.value
    assert out["manga"] == {"hidden": False, "tip": "На iPhone", "glyphs": 1, "stale": False}
    assert out["audio"] == {"hidden": False, "tip": "На iPad", "glyphs": 1, "stale": False}


def test_planning_episode_buttons_mark_the_episodes_on_a_device(page) -> None:
    result = page.run(
        LOOK
        + r"""
        await __tick(100);
        renderPlanningEpisodeChooser({mediaId: 101, total: 4, title: 'Frieren', states: new Map([[1, 'local']]), running: false, current: 0, added: 0});
        const row = n => __look(`#modalBody [data-episode="${n}"] .device-copies-row`);
        return {one: row(1), three: row(3), four: row(4)};
        """,
        api={"iphone_device_copies": COPIES},
    )
    out = result.value
    assert out["one"]["tip"] == "On iPad · iPhone"
    assert out["three"]["tip"] == "On iPad"
    assert out["four"]["hidden"] is True


def test_without_paired_devices_nothing_is_shown_and_nothing_breaks(page) -> None:
    result = page.run(
        LOOK
        + r"""
        await __tick(100);
        return {slots: document.querySelectorAll('.device-copies').length,
                shown: document.querySelectorAll('.device-copies:not([hidden])').length};
        """,
        state=HOME,
        # An older backend without the call answers null.
    )
    assert result.value == {"slots": 2, "shown": 0}


def test_planning_cards_have_it_in_the_cover_corner(page) -> None:
    result = page.run(
        LOOK
        + r"""
        setPage('planned', true); await __tick(150);
        const card = '#plannedContent article[data-media-id="101"]';
        return {corner: __look(card + ' .cover-shell > .device-copies-corner'),
                count: document.querySelectorAll(card + ' .device-copies').length};
        """,
        state={
            "planned": [{"media_id": 101, "title": "Frieren", "status": "PLANNING", "media_kind": "anime"}]
        },
        api={"iphone_device_copies": COPIES},
    )
    assert result.value["corner"]["tip"] == "On iPad · iPhone"
    assert result.value["count"] == 1


def test_audiobook_series_icon_follows_the_title(page) -> None:
    books = [
        {
            "id": 9,
            "title": "Book 1",
            "volume": 1,
            "series_key": "s",
            "series_title": "Series",
            "duration": 600,
            "position": 0,
            "chapters": [],
        },
        {
            "id": 10,
            "title": "Book 2",
            "volume": 2,
            "series_key": "s",
            "series_title": "Series",
            "duration": 600,
            "position": 0,
            "chapters": [],
        },
    ]
    result = page.run(
        LOOK
        + r"""
        setPage('audiobooks', true); await __tick(500);
        const head = document.querySelector('.audiobook-series-card .audiobook-series-card-head');
        const icon = head.querySelector('.device-copies');
        return {after: icon?.previousElementSibling?.tagName, count: icon?.nextElementSibling?.textContent,
                look: __look('.audiobook-series-card-head .device-copies'), volumes: tn('media.volumeCount', 2)};
        """,
        api={"audiobook_state": {"books": books}, "iphone_device_copies": COPIES},
    )
    out = result.value
    assert out["after"] == "STRONG" and out["count"] == out["volumes"]
    assert out["look"]["tip"] == "On iPad"


def test_clicking_the_corner_icon_of_a_planning_card_does_not_open_anilist(page) -> None:
    result = page.run(
        r"""
        setPage('planned', true); await __tick(150);
        const card = '#plannedContent article[data-media-id="101"]';
        await __click(card + ' .device-copies-corner svg'); await __tick(20);
        const onIcon = __apiCalls.filter(call => call.name === 'open_url').length;
        await __click(card + ' .cover-action'); await __tick(20);
        return {onIcon, onCover: __apiCalls.filter(call => call.name === 'open_url').map(call => call.args[0])};
        """,
        state={
            "planned": [{"media_id": 101, "title": "Frieren", "status": "PLANNING", "media_kind": "anime"}]
        },
        api={"iphone_device_copies": COPIES},
    )
    assert result.value == {"onIcon": 0, "onCover": ["https://anilist.co/anime/101"]}
