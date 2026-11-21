"""Continue watching: the card of an episode that is already open ("Already open") brings
its mpv to the front. It did nothing: that card's own click handler ignored running cards."""

from __future__ import annotations

import json

PATH = "/lib/Odd Taxi - 02.mkv"
CARD = {
    "media_id": 7,
    "title": "Odd Taxi",
    "episode": 2,
    "video_path": PATH,
    "resume_start": 600,
    "progress_percent": 40,
}


def _click_running_card(page, focused: bool):
    return page.run(
        f"""
        ui.playStates.set({json.dumps(PATH)}, 'running');
        renderCurrent(); setPathPlayState({json.dumps(PATH)}, 'running');
        const running = document.querySelector('[data-continue-card="1"]').classList.contains('play-running');
        await __fire('[data-continue-card="1"] .airing-meta', 'pointerup', {{button: 0, bubbles: true}}, 'PointerEvent');
        await __tick(20);
        return {{running, toasts: __toasts()}};
        """,
        state={"home": {"continue_watching": [CARD]}},
        api={"focus_playback": {"ok": True, "status": "running", "focused": focused}},
    )


def test_clicking_an_open_continue_card_brings_its_mpv_to_the_front(page) -> None:
    result = _click_running_card(page, True)
    assert result.value == {"running": True, "toasts": []}
    assert result.calls_of("focus_playback") == [[PATH]]
    assert result.calls_of("play") == []


def test_when_mpv_cannot_come_to_the_front_the_card_says_it_is_open(page) -> None:
    result = _click_running_card(page, False)
    assert result.value["toasts"] == ["Video is already open"]
    assert result.calls_of("play") == []
