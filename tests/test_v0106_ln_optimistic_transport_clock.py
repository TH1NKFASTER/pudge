"""Listen together: the reader's optimistic transport clock (behaviour; audit 71Б rewrite).

The clock functions run in the real window (no source extraction); the transport loop is
driven through the reader's Listen/Pause button against a backend whose position moves."""

from __future__ import annotations

import json

from t71g18_support import NO_LN_GATE, READER_VIEW, paired_ln

# Pause/resume presses of a real session (ms after the start, wanted playing state).
PRESSES = [
    [0, False],
    [352, True],
    [692, False],
    [1181, True],
    [1521, False],
    [1984, True],
    [2353, False],
    [2739, True],
    [3106, False],
    [3466, True],
    [3874, False],
    [4239, True],
    [4699, False],
    [5084, True],
    [5559, False],
    [5977, True],
    [6512, False],
    [6937, True],
    [7461, False],
    [7917, True],
    [8378, False],
    [8991, True],
]


def test_visual_clock_counts_only_the_time_the_narration_was_wanted(page) -> None:
    result = page.run(
        f"""
        const state = {{position: 336.663454, speed: 1, playing: true}};
        ui.lnPairedState = {{...state}};
        lnPairedTransportClockReset(state);
        let at = 0;
        for (const [time, desired] of {json.dumps(PRESSES)}) {{
          await __tick(time - at); at = time;
          lnPairedTransportClockSetDesired(desired, state); ui.lnPairedTransportDesired = desired;
        }}
        await __tick(9612 - at);
        const local = lnPairedTransportClockNow(state);
        const reconciled = lnPairedTransportClockReconcile({{...state, position: 341.886417, playing: true}}, {{settled: true}});
        await __tick(150);
        return {{local, reconciled, later: lnPairedTransportClockNow(state)}};
        """
    )
    out = result.value
    # 4.9 s of wanted playback in 9.6 s of presses.
    assert abs(out["local"] - 341.563454) < 0.01
    # The backend is 0.32 s ahead: no jump, the clock eases towards it.
    assert out["reconciled"]["snap"] is False and 0.30 < out["reconciled"]["drift"] < 0.35
    assert out["reconciled"]["position"] == out["local"]
    assert out["later"] > out["local"] + 0.15


def test_a_stale_sample_while_play_is_on_its_way_does_not_move_the_clock_back(page) -> None:
    # mpv answers with a sample from before the press (100.25 s) while the reader's clock,
    # running since the press, is at 101.2 s: the words must not jump back.
    result = page.run(
        """
        const state = {position: 100, speed: 1, playing: true};
        ui.lnPairedState = {...state};
        lnPairedTransportClockReset(state);
        lnPairedTransportClockSetDesired(true, state); ui.lnPairedTransportDesired = true;
        await __tick(1200);
        const before = lnPairedTransportClockNow(state);
        const result = lnPairedTransportClockReconcile({...state, position: 100.25}, {settled: false});
        return {before, result};
        """
    )
    out = result.value
    assert out["before"] > 101.1
    assert out["result"]["position"] >= out["before"] and out["result"]["snap"] is False


# The backend's position moves while its player plays; window.__jump moves it (a seek elsewhere).
MOVING = (
    paired_ln(True)
    + r"""
window.__since = Date.now(); window.__jump = 0;
const advance = () => { if (window.__state.playing) window.__state.position += (Date.now() - window.__since) / 1000; window.__since = Date.now(); };
__api.light_novel_paired_state = () => { advance(); return Promise.resolve({...window.__state, position: window.__state.position + window.__jump}); };
const heldMoving = playing => () => new Promise(resolve => { window.__release = () => {
  advance(); window.__state = {...window.__state, playing, paused: !playing, player_running: true}; resolve({...window.__state}); }; });
__api.audiobook_set_paused = (_id, paused) => heldMoving(!paused)();
"""
)


def test_transport_keeps_polling_while_a_press_is_on_its_way_and_snaps_only_on_a_big_jump(page) -> None:
    result = page.run(
        READER_VIEW
        + r"""
        const samples = () => __apiCalls.filter(c => c.name === 'light_novel_paired_state').length;
        await __settle(openLightNovel(6)); await __tick(500);
        await __click('#lnPairedAudio');                     // Pause: mpv has not answered yet
        let n = samples(); await __tick(1500);
        const out = {pausePolls: samples() - n};
        window.__release(); await __tick(500);
        out.paused = {label: transport().label, expected: PudgeI18n.t('ui.resume')};
        await __click('#lnPairedAudio');                     // Resume, again not answered yet
        n = samples(); await __tick(1500);
        out.resumePolls = samples() - n;
        window.__release(); await __tick(1000);
        const ticks = [];
        for (let step = 0; step < 4; step++) { ticks.push(clock()); await __tick(250); }
        out.ticks = ticks;
        // A small lead of the backend (0.3 s) is eased in, a seek elsewhere (5 s) is followed at once.
        const beforeLead = clock(); window.__jump = 0.3; await __tick(300);
        out.lead = clock() - beforeLead;
        const beforeJump = clock(); window.__jump = 5.3; await __tick(300);
        out.jump = {moved: clock() - beforeJump, word: spoken(), expected: [...'一二三四五六七八九十百千'][Math.floor(clock() - 100)]};
        return out;
        """,
        before=MOVING,
        state=NO_LN_GATE,
    )
    out = result.value
    assert out["pausePolls"] >= 3 and out["resumePolls"] >= 3
    assert out["paused"]["label"] == out["paused"]["expected"]
    assert [round(later - earlier, 2) for earlier, later in zip(out["ticks"], out["ticks"][1:])] == [
        0.25,
        0.25,
        0.25,
    ]
    assert 0.3 <= out["lead"] < 0.45  # 0.3 s of time plus part of the lead, no jump
    assert out["jump"]["moved"] > 4.5 and out["jump"]["word"] == out["jump"]["expected"]
