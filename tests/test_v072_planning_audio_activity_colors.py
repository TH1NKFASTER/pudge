from __future__ import annotations

from pathlib import Path

import pytest

from pudge.audio_activity import activity_regions_from_features, gate_activity_regions
from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)


ROOT = Path(__file__).resolve().parents[1]


def test_fft_features_keep_sentence_pauses_separate() -> None:
    np = pytest.importorskip("numpy")
    energy = np.full(260, -72.0, dtype=np.float32)
    energy[40:100] = -24.0
    energy[135:205] = -22.0
    flux = np.zeros_like(energy)
    flux[40] = 1.0
    flux[135] = 1.0

    regions, diagnostics = activity_regions_from_features(energy, flux)

    assert len(regions) == 2
    assert regions[0]["end"] < regions[1]["start"]
    assert diagnostics["speech_threshold_db"] > diagnostics["noise_floor_db"]


def test_fft_activity_is_gated_by_stt_to_reject_music() -> None:
    regions = [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    hints = [{"start": 2.2, "end": 2.8}]

    assert gate_activity_regions(regions, hints, padding_seconds=0.1) == [
        {"start": 2.1, "end": 2.9}
    ]


def test_anchor_clock_is_linear_even_when_fft_reports_silence() -> None:
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 0,
                "normalized_length": 100,
                "start": 10.0,
                "end": 18.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 100, "time": 18.0},
                ],
                "speech_regions": [
                    {"start": 10.0, "end": 12.0},
                    {"start": 16.0, "end": 18.0},
                ],
            }
        ],
    }

    middle = light_novel_position_for_audio(alignment, 14.0)
    late = light_novel_position_for_audio(alignment, 15.5)
    near_end = light_novel_position_for_audio(alignment, 17.0)

    assert middle is not None
    assert late is not None
    assert near_end is not None
    assert middle["chapter_char_offset_exact"] == 50.0
    assert late["chapter_char_offset_exact"] == 68.75
    assert near_end["chapter_char_offset_exact"] == 87.5
    assert middle["anchor_window"]["activity"] == alignment["chapters"][0][
        "speech_regions"
    ]
    assert audio_position_for_light_novel_offset(alignment, 0, 50) == 14.0

def test_planning_primary_download_and_visible_ln_color_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    alignment = (ROOT / "pudge/reading_audio_alignment.py").read_text(encoding="utf-8")
    audiobooks = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")

    assert 'data-action="planning-episodes-auto-card"' in html
    assert "Автоскачать серии" in html
    assert "startPlanningEpisodeDownload" in html
    assert "planning_episode_download_status" in html
    assert "preparePlanningEpisodeChooser" in html
    assert "syncLnWordColorOutputs" in html
    assert "data-ln-word-color-value" in html
    assert 'input[type="color"]::-webkit-color-swatch' in html
    assert "anchor.activity" in html
    assert '"schema": "reading-audio-v3"' in alignment
    assert "reading-audio-v3-punctuation-clock-v2" in audiobooks
