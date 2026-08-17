from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _local_speech_shift_estimate,
    _refine_stt_opening_plateaus,
)


def test_local_speech_shift_estimate_finds_five_second_cold_open_residual() -> None:
    starts = [10.0, 16.0, 23.0, 31.0, 40.0, 48.0]
    reference = [value + 5.2 for value in starts]
    reference += [150.0, 160.0, 170.0, 180.0]
    result = _local_speech_shift_estimate(starts, sorted(reference))
    assert result["accepted"] is True
    assert abs(float(result["shift_seconds"]) - 5.2) <= 0.11


def test_opening_plateau_refinement_can_shift_pre_and_post_independently(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.srt"

    pre = [10.0, 16.0, 23.0, 31.0, 40.0, 48.0]
    post = [140.0, 147.0, 155.0, 164.0, 174.0, 185.0, 197.0, 210.0]
    source_cues = [(value, value + 1.4, f"s{index}") for index, value in enumerate(pre + post)]
    aligned_cues = list(source_cues)
    reference_cues = [
        (value + 5.2, value + 6.6, f"r{index}")
        for index, value in enumerate(pre)
    ]
    reference_cues += [
        (value + 0.5, value + 1.9, f"p{index}")
        for index, value in enumerate(post)
    ]

    write_srt(source_cues, source)
    write_srt(aligned_cues, aligned)
    write_srt(reference_cues, reference)

    output, result = _refine_stt_opening_plateaus(
        source,
        aligned,
        reference,
        tmp_path / "cache",
    )
    assert result["applied"] is True
    assert abs(float(result["pre_shift_seconds"]) - 5.2) <= 0.11
    assert abs(float(result["post_shift_seconds"]) - 0.5) <= 0.11

    cues = parse_srt(output)
    assert abs(cues[0][0] - 15.2) <= 0.11
    assert abs(cues[len(pre)][0] - 140.5) <= 0.11
