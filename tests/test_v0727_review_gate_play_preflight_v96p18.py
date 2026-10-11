"""v96p18: before an episode starts, the window asks whether a review is due and opens the
review gate first; the episode starts only afterwards, with the launch options it was asked
for (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json


PATH = "/library/Show - 03.mkv"
# "Continue watching" on Home: a press on the card resumes the episode.
SHOW = {
    "media_id": 7,
    "title": "Show",
    "episode": 3,
    "video_path": PATH,
    "resume_start": 300,
    "progress_percent": 40,
}
OTHER = {**SHOW, "media_id": 8, "title": "Other", "video_path": "/library/Other - 01.mkv"}
DUE = {
    "supported": True,
    "blocking": True,
    "granted": False,
    "required": 1,
    "completed": 0,
    "remaining": 1,
    "reason": "episode_due_scan_required",
}
PAGE = (
    f"const PATH = {json.dumps(PATH)};\n"
    'const CARD = `#currentContent [data-continue-card][data-play-path="${PATH}"]`;\n'
    "const playing = () => ({gate: Boolean(PudgeReviewGate.isOpen()), state: ui.playStates.get(PATH) ?? null,\n"
    "                        starting: document.querySelector(CARD).classList.contains('play-starting')});\n"
    "const press = () => __fire(CARD, 'pointerup', {button: 0}, 'PointerEvent');\n"
)


def test_review_gate_opens_before_the_episode_starts(page) -> None:
    result = page.run(
        PAGE
        + r"""
        __api.review_gate_begin = () => new Promise(() => {});      // the gate is still loading cards
        await press();
        await __tick(100);
        const gated = playing();
        await press();                                         // pressing again changes nothing
        await __fire('#currentContent [data-continue-card][data-media-id="8"]', 'pointerup', {button: 0}, 'PointerEvent');
        await __tick(100);
        return {gated, again: playing()};
        """,
        state={"home": {"continue_watching": [SHOW, OTHER]}},
        api={"review_gate_status": DUE},
    )
    out = result.value
    assert out["gated"] == {"gate": True, "state": None, "starting": False}
    assert out["again"] == out["gated"]
    # Neither this episode nor another one starts behind the open gate.
    assert result.calls_of("review_gate_status") == [[PATH]]
    assert result.calls_of("review_gate_begin") == [[PATH]]
    assert not result.called("play")


def test_a_second_watch_click_during_the_review_check_starts_nothing_twice(page) -> None:
    # The check is not visible (no spinner yet): a double click must not ask twice or start twice.
    result = page.run(
        PAGE
        + r"""
        let answer;
        __api.review_gate_status = () => new Promise(resolve => { answer = resolve; });
        await press();
        await press();
        await __flush();
        const asked = __apiCalls.filter(call => call.name === 'review_gate_status').length;
        answer({supported: true, blocking: false, granted: true});
        await __tick(100);
        return {asked, after: playing()};
        """,
        state={"home": {"continue_watching": [SHOW]}},
        api={"play": {"ok": True, "status": "starting"}, "play_status": {"status": "starting"}},
    )
    out = result.value
    assert out["asked"] == 1
    assert result.calls_of("play") == [[PATH, True, False, False]]
    assert out["after"] == {"gate": False, "state": "starting", "starting": True}


def test_backend_gate_reopens_and_the_episode_starts_with_its_launch_options(page) -> None:
    # The preflight said nothing is due, but play() itself requires a review (fallback). After
    # the review, the episode starts with the same options and its playlist position: when it
    # has been watched, the playlist advances.
    result = page.run(
        PAGE
        + r"""
        let plays = 0;
        __api.play = async () => (++plays === 1
          ? {ok: false, review_gate_required: true, review_gate: {blocking: true, required: 1, completed: 0, remaining: 1}}
          : {ok: true, status: 'starting'});
        __api.play_status = async () => ({status: 'exited', watched: true, episode: 3});
        __api.advance_playlist = async () => ({state: await __api.get_state(), next: null});
        await __settle(startPlay(PATH, true, false, {playlistId: 4, itemId: 9}, true));
        await __tick(2000);
        return {gate: PudgeReviewGate.isOpen(), toasts: __toasts(), finished: t('toast.queueFinished')};
        """,
        state={"home": {"continue_watching": [SHOW]}},
        api={
            "review_gate_status": {"supported": True, "blocking": False, "granted": True},
            "review_gate_begin": {"supported": True, "blocking": False, "granted": True},
        },
    )
    out = result.value
    assert result.calls_of("review_gate_begin") == [[PATH]]
    assert result.calls_of("play") == [[PATH, True, False, True], [PATH, True, False, True]]
    assert out["gate"] is False
    assert result.calls_of("advance_playlist") == [[4, 9]]
    assert out["finished"] in out["toasts"]


def test_episode_starts_when_the_gate_grants_at_once(page) -> None:
    result = page.run(
        PAGE
        + r"""
        let granted = false;          // the backend grants the episode once the gate has begun
        __api.review_gate_status = async () => (granted ? {supported: true, blocking: false, granted: true} : DUE);
        __api.review_gate_begin = async () => { granted = true; return {supported: true, blocking: false, granted: true}; };
        await __settle(startPlay(PATH, true, false, null, true));
        await __tick(100);
        return playing();
        """.replace("DUE", json.dumps(DUE)),
        state={"home": {"continue_watching": [SHOW]}},
        api={"play": {"ok": True, "status": "starting"}, "play_status": {"status": "starting"}},
    )
    assert result.calls_of("review_gate_begin") == [[PATH]]
    assert result.value["gate"] is False
    assert result.calls_of("play") == [[PATH, True, False, True]]
