from __future__ import annotations

from pathlib import Path

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"


def _wall_clock_bridge_alignment() -> dict:
    return {
        "duration": 20_000.0,
        "chapters": [
            {
                "chapter_index": 2,
                "start": 14054.365,
                "end": 14110.0,
                "normalized_length": 200,
                "activity_clock": True,
                "punctuation_pause_count": 1,
                "speech_regions": [
                    {"start": 14089.015, "end": 14090.360},
                    {"start": 14090.900, "end": 14092.460},
                ],
                "anchors": [
                    {"time": 14089.015, "offset": 142},
                    {
                        "time": 14090.900,
                        "offset": 155,
                        "wall_clock_from_previous": True,
                    },
                    {"time": 14094.755, "offset": 162},
                ],
            }
        ],
    }


def test_pause_stretched_bridge_is_not_recompressed_by_backend_activity_clock() -> None:
    alignment = _wall_clock_bridge_alignment()
    at_speech_end = light_novel_position_for_audio(alignment, 14090.360)
    assert at_speech_end is not None
    # Linear wall-time interpolation: 1.345 / 1.885 of the 13-char bridge.
    expected = 142 + 13 * ((14090.360 - 14089.015) / (14090.900 - 14089.015))
    assert at_speech_end["chapter_char_offset_exact"] == pytest.approx(expected, abs=0.01)
    assert at_speech_end["chapter_char_offset_exact"] < 152.0
    assert at_speech_end["anchor_window"]["path"][1]["wall_clock_from_previous"] is True


def test_pause_stretched_bridge_inverse_seek_uses_same_wall_clock() -> None:
    alignment = _wall_clock_bridge_alignment()
    position = audio_position_for_light_novel_offset(alignment, 2, 151)
    assert position is not None
    expected = 14089.015 + ((151 - 142) / 13) * (14090.900 - 14089.015)
    assert position == pytest.approx(expected, abs=0.01)
    # Activity-clock compression would incorrectly land before the speech end.
    assert position > 14090.30


def test_frontend_does_not_apply_vad_ratio_to_marked_wall_clock_bridge() -> None:
    html = INDEX.read_text(encoding="utf-8")
    path_fn = html.split("function lnPairedAnchorPath", 1)[1].split(
        "function lnPairedOffsetAtTime", 1
    )[0]
    offset_fn = html.split("function lnPairedOffsetAtTime", 1)[1].split(
        "function lnPairedResetWordProgress", 1
    )[0]
    assert "wall_clock_from_previous:row?.wall_clock_from_previous===true" in path_fn
    assert "right.wall_clock_from_previous!==true" in offset_fn


def test_torrent_cleanup_keeps_single_capture_http_owner() -> None:
    html = INDEX.read_text(encoding="utf-8")
    button = html.split('id="torrentToggleButton"', 1)[1].split('</button>', 1)[0]
    assert "onclick=" not in button
    assert html.count("async function torrentToggleHttpCapture(){") == 1
    assert html.count("torrentHttpJson('/api/torrents/enabled'") == 1
    assert "torrentToggleGeneration" not in html
