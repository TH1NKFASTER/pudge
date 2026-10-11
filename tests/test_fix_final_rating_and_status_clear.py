"""Final-episode score prompt and the sidebar status line (behaviour, page harness)."""

from __future__ import annotations

import json

VIDEO = "/media/Akiba Maid Sensou/[SubsPlease] Akiba Maid Sensou - 12v2 (1080p) [780697D0].mkv"

# One play_status answer per poll; the last one repeats.
MONITOR = r"""
const answers = window.__answers;
__api.play_status = async () => answers.length > 1 ? answers.shift() : answers[0];
__api.final_sequel_option = async () => ({sequel: null});
monitorPlay(VIDEO);
await __tick(20000);
return {open: !$('modalBackdrop').hidden && $('modalTitle').textContent === t('modal.rateFinal', {title: 'Akiba Maid Sensou'}),
        title: $('modalTitle').textContent,
        expected: t('modal.rateFinal', {title: 'Akiba Maid Sensou'}),
        rating: ui.ratingContext ? {media: ui.ratingContext.mediaId, episode: ui.ratingContext.episode,
                                    final: ui.ratingContext.finalPrompt} : null};
"""


def _status(**extra):
    base = {
        "ok": True,
        "status": "running",
        "watched": True,
        "episode": 12,
        "media_id": 151379,
        "title": "Akiba Maid Sensou",
        "user_score": None,
        "final_episode": False,
        "rating_episode": None,
        "rating_prompted": False,
    }
    base.update(extra)
    return base


def _run_monitor(page, answers):
    before = "window.__answers = " + json.dumps(answers) + ";"
    return page.run(MONITOR.replace("VIDEO", json.dumps(VIDEO)), before=before).value


def test_score_prompt_after_the_last_episode_counted_after_the_early_watched_report(page) -> None:
    # Akiba Maid Sensou 12/12: the personal schedule reported the episode watched at ~60 %
    # while AniList progress was still 11 (not final yet); the tracker counted 12 later in the
    # same playback. The rating prompt must still open when mpv closes.
    answers = [
        _status(),
        _status(),
        _status(final_episode=True, rating_episode=12),
        _status(status="idle", watched=True, final_episode=True, rating_episode=12),
    ]
    out = _run_monitor(page, answers)
    assert out["open"] is True
    assert out["title"] == out["expected"]
    assert out["rating"] == {"media": 151379, "episode": 12, "final": True}


def test_no_score_prompt_when_already_rated_or_not_final(page) -> None:
    prompted = [
        _status(final_episode=True, rating_episode=12),
        _status(final_episode=True, rating_episode=12, rating_prompted=True),
        _status(status="idle", final_episode=True, rating_episode=12, rating_prompted=True),
    ]
    out = _run_monitor(page, prompted)
    assert out["open"] is False and out["rating"] is None
    not_final = [_status(), _status(status="idle")]
    out = _run_monitor(page, not_final)
    assert out["open"] is False and out["rating"] is None


def test_sidebar_status_clears_itself_30_seconds_after_the_last_event(page) -> None:
    result = page.run(
        r"""
        const shown = () => ({visible: __visible('#status'), text: $('status').textContent});
        setStatus(t('status.episodeCounted', {episode: 1}));
        const first = shown();
        await __tick(20000);
        const at20 = shown();
        setStatus(t('status.episodeCounted', {episode: 2}));
        await __tick(20000);
        const restarted = shown();
        await __tick(10500);
        const cleared = shown();
        return {first, at20, restarted, cleared, hiddenAttr: $('status').hidden,
                expected1: t('status.episodeCounted', {episode: 1}),
                expected2: t('status.episodeCounted', {episode: 2})};
        """,
        settle=30000,  # start-up finished: its own "Ready" does not interfere
    ).value
    assert result["first"] == {"visible": True, "text": result["expected1"]}
    assert result["at20"] == {"visible": True, "text": result["expected1"]}
    # A newer event restarts the 30 s: 40 s after the first, 20 s after the second.
    assert result["restarted"] == {"visible": True, "text": result["expected2"]}
    # Hidden, not an empty line: the sidebar plate keeps no gap.
    assert result["cleared"] == {"visible": False, "text": ""}
    assert result["hiddenAttr"] is True
