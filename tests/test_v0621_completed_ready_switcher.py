"""Completed-ready cards (behaviour, page harness; audit 71Б rewrite): no "Why not ready?"
for them, and the watch-order card switches between its entries. The ready state text of
the selected entry is tested in test_v0569_retry_tabs_and_sequence_status.py, the hover
list and the switch buttons on the cover in test_v0566_hover_jimaku_and_release_layout.py,
caught-up cards in test_v0639_legacy_watched_dirs_and_caught_up.py."""

from __future__ import annotations

# An episode whose subtitles are OCR only: "Why not ready?" applies to it elsewhere.
OCR_ONLY = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "progress": 2,
    "site_url": "https://anilist.co/anime/7",
    "local": {"episode": 3, "state": "ready", "subtitle_origin": "ocr", "video_path": "/v/s3.mkv"},
    "ready_episodes": [3],
}
MENUS = r"""
const actions = () => [...document.querySelectorAll('#contextMenu button')].map(b => b.dataset.contextAction);
await __fire('#currentContent [data-media-id="7"]', 'contextmenu', {bubbles: true, cancelable: true, clientX: 10, clientY: 10}, 'MouseEvent');
const rightClick = actions(); hideContextMenu();
await __click('#currentContent [data-media-id="7"] [data-action="card-menu"]');
const button = actions(); hideContextMenu();
return {rightClick, button};
"""


def test_completed_ready_cards_never_offer_readiness_diagnostics(page) -> None:
    completed = page.run(MENUS, state={"home": {"completed_ready": [OCR_ONLY]}, "current": [OCR_ONLY]}).value
    assert "open" in completed["rightClick"]
    assert "diagnose" not in completed["rightClick"] and "diagnose" not in completed["button"]
    fresh = page.run(MENUS, state={"home": {"new_ready": [OCR_ONLY]}, "current": [OCR_ONLY]}).value
    assert "diagnose" in fresh["rightClick"] and "diagnose" in fresh["button"]


SEQUENCE = {
    "kind": "watch_sequence",
    "title": "Series",
    "items": [
        {
            "media_id": 2,
            "title": "Part A",
            "cover": "https://img/a.jpg",
            "local": {"video_path": "/v/a.mkv"},
            "ready_episodes": [1, 2],
        },
        {
            "media_id": 3,
            "title": "Movie B",
            "cover": "https://img/b.jpg",
            "format": "MOVIE",
            "local": {"video_path": "/v/b.mkv"},
            "ready_episodes": [],
        },
    ],
}


def test_watch_order_card_switches_entries_and_plays_the_selected_one(page) -> None:
    result = page.run(
        r"""
        const card = document.querySelector('#currentContent .ready-sequence-card');
        const look = () => ({title: card.querySelector('.ready-sequence-meta strong').textContent,
                             position: card.querySelector('[data-ready-sequence-position]').textContent,
                             cover: card.querySelector('.ready-sequence-cover-media img')?.getAttribute('src') ?? null,
                             active: [...card.querySelectorAll('.ready-sequence-row.active')].map(row => row.innerText.replace('\n', ' '))});
        const out = {start: look()};
        await __click('#currentContent .ready-sequence-card [data-action="ready-sequence-previous"]');   // wraps around
        out.previous = look();
        await __click('#currentContent .ready-sequence-card .ready-sequence-meta strong'); await __tick(50);
        await __click('#currentContent .ready-sequence-card [data-action="ready-sequence-next"]');
        out.next = look();
        return out;
        """,
        state={"home": {"completed_ready": [SEQUENCE]}},
        api={"play": {"ok": True, "status": "starting"}, "play_status": {"status": "exited"}},
    )
    out = result.value
    assert out["start"] == {
        "title": "Part A",
        "position": "1/2",
        "cover": "https://img/a.jpg",
        "active": ["1 Part A"],
    }
    assert out["previous"] == {
        "title": "Movie B",
        "position": "2/2",
        "cover": "https://img/b.jpg",
        "active": ["2 Movie B"],
    }
    assert out["next"] == out["start"]
    # Clicking the card plays the entry it shows.
    assert [call[0] for call in result.calls_of("play")] == ["/v/b.mkv"]
