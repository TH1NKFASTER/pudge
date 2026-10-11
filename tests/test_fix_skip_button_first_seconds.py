"""The skip opening/ending button is shown only during the first 5 seconds of the segment."""

from __future__ import annotations

import json
from pathlib import Path

from lua_harness import SKIP_SEGMENTS, needs_lua, run_mpv_scenario


def _session(tmp_path: Path, segments: list[dict]) -> dict[str, str]:
    path = tmp_path / "session.json"
    video = "/media/Show - 05.mkv"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "abc",
                "video": video,
                "video_absolute": video,
                "fingerprint": "f",
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )
    return {"PUDGE_SEGMENTS_FILE": str(path)}


@needs_lua
def test_button_disappears_five_seconds_into_the_opening(tmp_path: Path) -> None:
    env = _session(tmp_path, [{"kind": "intro", "start": 30, "end": 120}])
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 28)
        sim.fire('file-loaded')
        sim.play(3)  -- ~31 s: 1 s into the OP
        assert(sim.overlay():find('Skip opening', 1, true), 'button at the start of the OP')
        sim.play(3)  -- ~34 s
        assert(sim.overlay():find('Skip opening', 1, true), 'still visible within 5 s')
        sim.play(2)  -- ~36 s: 6 s into the OP
        assert(sim.overlay() == '', 'button gone after 5 s')
        assert(sim.press('Tab') == false, 'key released with the button')
        assert(sim.click(1700, 850) == false, 'clicks pass through')
        sim.play(20)
        assert(sim.overlay() == '', 'does not come back while the OP plays on')
        sim.seek(32)  -- back into the first 5 s
        sim.play(0.5)
        assert(sim.overlay():find('Skip opening', 1, true), 'seeking back into the first 5 s shows it again')
        assert(sim.press('Tab') == true, 'key works while the button is visible')
        assert(math.abs(sim.props['time-pos'] - 120) < 0.001, 'skipped to the OP end')
    """,
        scripts=[SKIP_SEGMENTS],
        env=env,
    )


@needs_lua
def test_entering_the_ending_late_shows_no_button(tmp_path: Path) -> None:
    env = _session(tmp_path, [{"kind": "outro", "start": 1300, "end": 1390}])
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.seek(1320)  -- 20 s into the ED
        sim.play(2)
        assert(sim.overlay() == '', 'no button when entering the ED after its first 5 s')
        assert(sim.press('Tab') == false)
        sim.seek(1299)
        sim.play(2)  -- playback runs into the ED
        assert(sim.overlay():find('Skip ending', 1, true), 'button at the start of the ED')
        sim.play(5)
        assert(sim.overlay() == '', 'gone again after 5 s')
    """,
        scripts=[SKIP_SEGMENTS],
        env=env,
    )
