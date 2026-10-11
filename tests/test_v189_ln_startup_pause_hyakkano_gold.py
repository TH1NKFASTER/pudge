"""Listen together: the highlight and a starting audiobook player; punctuation pauses
across a long silence (behaviour; audit 71Б rewrite).

The cold-open subtitle clock of the same release (inferred from signals, no per-episode
offset): test_v193_cold_open_download_ln_card_unification.py."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import _inject_punctuation_pause_anchors
from t71g21_support import READER, paired


# The audiobook's player is started by Listen and reports "playing" at once, but its
# media clock stands still for the first 0.7 s (a freshly spawned mpv). before= code on top
# of tests/t71g21_support.py PAIRED.
STALLED_START = r"""
window.STALL_UNTIL = 0;
window.audioPosition = () => AUDIO.playing
  ? AUDIO.position + Math.max(0, performance.now() - Math.max(AUDIO.at, STALL_UNTIL)) / 1000 * AUDIO.speed : AUDIO.position;
__api.light_novel_play_paired = async (_book, _chapter, progress) => {
  setAudio({position: 100 + 10 * progress, playing: true, running: true}); STALL_UNTIL = performance.now() + 700; return pairedState(); };
"""


def test_highlight_does_not_run_ahead_of_a_player_that_is_still_starting(page) -> None:
    # Old bug: the reader's clock ran from the press while mpv stood still, and the
    # highlight stayed ~0.7 s ahead of the voice for the rest of the chapter.
    result = page.run(
        READER
        + r"""
        await openReader();
        await __click('#lnPairedAudio');
        const started = performance.now(), samples = [];
        for (let k = 0; k < 40; k++) { await __tick(50); samples.push({at: performance.now() - started, offset: shown(), voice: audioPosition() - 100}); }
        return {samples, stall: STALL_UNTIL - started};
        """,
        **paired(playing=False, extra=STALLED_START),
    )
    samples, stall = result.value["samples"], result.value["stall"]
    shown = [sample["offset"] for sample in samples if sample["offset"] is not None]
    assert len(shown) == len(samples) and shown == sorted(shown)  # lit at once, never back
    leads = [sample["offset"] - sample["voice"] for sample in samples]
    assert max(leads) < 0.35  # at most until the first poll
    voice_running = [lead for sample, lead in zip(samples, leads) if sample["at"] > stall + 300]
    assert len(voice_running) > 15 and all(-0.06 <= lead <= 0.02 for lead in voice_running)


def test_clock_waits_for_a_new_player_but_not_for_a_running_one(page) -> None:
    # Poll samples of real traces. A new mpv reports the start position twice, then moves:
    # the reader's clock waits for it and then follows without a lead. A running mpv that
    # was asked to seek moves at once: the clock follows it from the first sample.
    result = page.run(
        r"""
        const run = async (start, polls) => {
          const state = {position: start, speed: 1, playing: true};
          lnPairedTransportClockReset(state);
          let at = 0; const out = [];
          for (const [time, position] of polls) {
            await __tick(time - at); at = time;
            const r = lnPairedTransportClockReconcile({...state, position}, {settled: true});
            out.push({lead: r.lead, waiting: r.startupWaiting});
          }
          return out;
        };
        return {fresh: await run(33.646, [[148, 33.646], [527, 33.646], [782, 33.71679], [1040, 33.973208]]),
                running: await run(14054.186847, [[122, 14054.309]])};
        """
    )
    fresh, running = result.value["fresh"], result.value["running"]
    assert [row["waiting"] for row in fresh] == [True, True, False, False]
    assert all(abs(row["lead"]) < 0.01 for row in fresh[:3]) and abs(fresh[3]["lead"]) < 0.05
    assert running == [{"lead": pytest.approx(0, abs=0.03), "waiting": False}]


def test_long_vad_gap_moves_impossible_next_word_onset_to_resume() -> None:
    anchors = [
        {"offset": 131, "time": 14085.635},
        {"offset": 133, "time": 14085.935},
        {"offset": 135, "time": 14087.395},
    ]
    boundaries = [{"offset": 133, "strength": 3}]
    speech_regions = [
        {"start": 14085.455, "end": 14085.960},
        {"start": 14086.700, "end": 14087.140},
        {"start": 14087.580, "end": 14088.075},
    ]
    refined, count = _inject_punctuation_pause_anchors(anchors, boundaries, speech_regions)
    assert count == 1
    assert not any(
        abs(float(row["offset"]) - 133.0) < 1e-6 and float(row["time"]) < 14086.700 - 1e-6 for row in refined
    )
    assert {"offset": 132.999, "time": 14085.96} in refined
    assert {"offset": 132.999, "time": 14086.699} in refined
    assert {"offset": 133, "time": 14086.7} in refined
