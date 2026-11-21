"""A TV caption file that opens on a sound caption keeps its one-line cold open on its own clock.

Black Clover S2 E07 (TV captions from Jimaku on the Crunchyroll WEB video): the broadcast has a
sponsor card after the opening, so everything after it is 10 s later than in the video while
the cold open before it is not. The file's first cue is a cry (``（泣き声）``) the English track
never shows, so the first-edge hint (+7.9 s) paired the cry with the English first line, was
"not local" against the -10 s clock, and the one spoken line of the cold open was shown ten
seconds early. The hint is now taken from the first spoken line when the file opens on a sound
caption, and that line's end must fit the reference too. When the reference opens on the same
block of lines before a long gap, the cold open needs no speech check (it lost the line to the
-10 s clock again, or rejected both text files without speech recognition). All data here is
invented.
"""

from __future__ import annotations

import random
from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.timeline_alignment import (
    _cold_start_refinement,
    _early_edit_audio_verification_risk,
    _sound_caption_only,
    _WindowMatch,
    align_subtitle_timelines,
)

MAIN = -10.0


def _segments() -> list[dict[str, object]]:
    return [
        {
            "first_center": 115.0,
            "last_center": 1200.0,
            "offset_seconds": MAIN,
            "support": 20,
            "mean_score": 3.3,
            "mean_coverage": 0.94,
            "windows": [],
        }
    ]


def _cues(first_text: str = "（雨の音）") -> list[tuple[float, float, str]]:
    return [
        (7.1, 14.6, first_text),
        (14.6, 18.2, "⸨まって！⸩"),
        (125.0, 127.0, "えっ…。"),
        (127.1, 131.0, "ここから先は通さない。"),
        (131.1, 135.0, "それでも行くの？"),
    ]


def _run(cues, reference_starts, reference_ends):
    onsets = sorted(start for start, _end, _text in cues)
    hints = (reference_starts[0] - onsets[0], reference_starts[-1] - onsets[-1])
    reference = [(start, end, "line") for start, end in zip(reference_starts, reference_ends)]
    return _cold_start_refinement(
        cues,
        onsets,
        sorted(reference_starts),
        _segments(),
        [],
        [],
        hints,
        reference_cues=reference,
    )


def _early_window(center: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=MAIN,
        score=2.8,
        matched=5,
        source_count=7,
        reference_count=8,
        onset_coverage=0.71,
        onset_f1=0.67,
        activity_f1=0.85,
        mean_error=0.42,
        gap_fingerprint=0.9,
        edge_hint_distance=None,
    )


def _risk(cold_start: dict[str, object]) -> dict[str, object]:
    return _early_edit_audio_verification_risk(
        [_early_window(115.0), _early_window(151.0), _early_window(223.0)], cold_start, []
    )


REFERENCE_STARTS = [14.95, 115.0, 117.2, 121.1]
REFERENCE_ENDS = [18.3, 117.0, 120.9, 125.0]


def test_sound_caption_only() -> None:
    assert _sound_caption_only("（雨の音）")
    assert _sound_caption_only("(door opens)")
    assert _sound_caption_only("（足音）（ドアが開く）")
    assert _sound_caption_only("［拍手］")
    assert not _sound_caption_only("（ミナ）えっ…。")
    assert not _sound_caption_only("((あっ！))")
    assert not _sound_caption_only("⸨まって！⸩")
    assert not _sound_caption_only("♪～")
    assert not _sound_caption_only("それでも行くの？")
    assert not _sound_caption_only("")


def test_cold_open_after_a_sound_caption_takes_the_first_spoken_line_clock() -> None:
    segments, boundaries, payload, diagnostics = _run(_cues(), REFERENCE_STARTS, REFERENCE_ENDS)

    assert diagnostics["applied"] is True
    assert diagnostics["hint_from"] == "first_spoken_line"
    assert diagnostics["first_onset_hint"]["reason"] == "edge_hint_not_local"
    assert abs(float(diagnostics["hint_offset_seconds"]) - 0.35) < 1e-6
    assert diagnostics["cue_count"] == 1
    assert segments[0]["kind"] == "cold_start"
    assert abs(float(segments[0]["offset_seconds"]) - 0.35) < 1e-6
    assert segments[1]["offset_seconds"] == MAIN
    assert 18.2 < boundaries[0] < 125.0
    assert payload[0]["kind"] == "cold_start"
    # The reference opens on that one line, then nothing for 96 s: both tracks agree.
    assert diagnostics["reference_block_lines"] == 1
    assert diagnostics["reference_block_verified"] is True
    risk = _risk(diagnostics)
    assert risk["required"] is False
    assert risk["cold_start_verified_by"] == "reference_line_block"


def test_cold_open_without_the_same_reference_block_still_asks_speech() -> None:
    # The reference has a second line right after the first: the blocks differ, so the
    # cold open is moved but goes to the Japanese speech check as before.
    starts = [14.95, 19.0, 115.0, 117.2, 121.1]
    ends = [18.3, 21.0, 117.0, 120.9, 125.0]
    segments, _boundaries, _payload, diagnostics = _run(_cues(), starts, ends)

    assert diagnostics["applied"] is True
    assert diagnostics["reference_block_verified"] is False
    assert segments[0]["kind"] == "cold_start"
    risk = _risk(diagnostics)
    assert risk["required"] is True
    assert "opening_gap_clock_ambiguity" in risk["reasons"]
    assert "cold_start_verified_by" not in risk


def test_spoken_line_whose_end_does_not_fit_keeps_the_main_clock() -> None:
    # The reference's first line runs 9 s: not the 3.6 s spoken line at any clock.
    segments, boundaries, _payload, diagnostics = _run(_cues(), REFERENCE_STARTS, [24.0, 117.0, 120.9, 125.0])

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_hint_not_local"
    assert diagnostics["first_line_hint"]["reason"] == "edge_hint_does_not_fit_line_ends"
    assert segments == _segments()
    assert boundaries == []


def test_file_opening_on_a_spoken_line_is_judged_as_before() -> None:
    # Same times, but the first cue is spoken: its own first-onset hint (+7.85 s against
    # the -10 s clock) is too far to be local and no other hint is tried.
    segments, boundaries, _payload, diagnostics = _run(
        _cues("（ミナ）ねえ、起きて。"), REFERENCE_STARTS, REFERENCE_ENDS
    )

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_hint_not_local"
    assert "first_line_hint" not in diagnostics
    assert segments == _segments()
    assert boundaries == []


def test_sound_caption_on_the_main_clock_is_left_alone() -> None:
    # The reference's first line sits where the main clock puts the sound caption: the edge
    # agrees with the clock, so no other hint is tried.
    cues = [(20.0, 24.0, "（雨の音）"), (30.0, 33.0, "⸨まって！⸩"), *_cues()[2:]]
    starts = [20.0 + MAIN + 0.2, 115.0, 117.2, 121.1]
    segments, boundaries, _payload, diagnostics = _run(cues, starts, REFERENCE_ENDS)

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_hint_not_local"
    assert "first_line_hint" not in diagnostics
    assert segments == _segments()
    assert boundaries == []


def test_without_reference_lines_nothing_else_is_tried() -> None:
    cues = _cues()
    onsets = sorted(start for start, _end, _text in cues)
    _segments_out, _boundaries, _payload, diagnostics = _cold_start_refinement(
        cues,
        onsets,
        REFERENCE_STARTS,
        _segments(),
        [],
        [],
        (REFERENCE_STARTS[0] - onsets[0], 0.0),
    )
    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_hint_not_local"


def _episode(tmp_path: Path) -> tuple[Path, Path]:
    rng = random.Random(7)
    source: list[tuple[float, float, str]] = [
        (7.107, 14.648, "（雨の音）"),
        (14.648, 18.151, "⸨まって！⸩"),
    ]
    reference: list[tuple[float, float, str]] = [(15.0, 18.25, "Wait!")]
    reference.append((108.5, 113.9, "EPISODE TITLE"))
    time = 125.0
    index = 0
    while time < 1320.0:
        duration = rng.uniform(1.4, 4.2)
        source.append((round(time, 3), round(time + duration, 3), f"せりふ{index}です。"))
        jitter = rng.uniform(-0.12, 0.12)
        start = time + MAIN + 0.25 + jitter
        reference.append((round(start, 3), round(start + duration - 0.3, 3), f"Line {index}."))
        index += 1
        time += duration + rng.choice((0.1, 0.1, 0.4, 0.9, 1.6, 2.5, 4.0, 7.5))
    source_path = tmp_path / "source.srt"
    reference_path = tmp_path / "reference.srt"
    write_srt(source, source_path)
    write_srt(reference, reference_path)
    return source_path, reference_path


def test_episode_cold_open_after_a_sound_caption_is_mapped_onto_its_reference_line(tmp_path: Path) -> None:
    source, reference = _episode(tmp_path)

    output, result = align_subtitle_timelines(source, reference, tmp_path / "cache", force=True)

    assert result["accepted"] is True
    assert result["timeline_cold_start"]["applied"] is True
    assert result["timeline_cold_start"]["hint_from"] == "first_spoken_line"
    assert result["timeline_early_edit_audio_verification"]["required"] is False
    cues = parse_srt(output)
    spoken = next(cue for cue in cues if "まって" in cue[2])
    assert abs(spoken[0] - 15.0) < 0.05
    body = next(cue for cue in cues if cue[2].startswith("せりふ0"))
    assert abs(body[0] - (125.0 + MAIN)) < 0.35
    assert result["timeline_segments"][-1]["kind"] == "stable"
