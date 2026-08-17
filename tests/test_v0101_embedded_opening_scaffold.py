from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _restore_embedded_opening_clock_scaffold


def test_bleach_style_global_stt_restores_pre_op_clock(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    pre = [10.0, 18.0, 27.0, 36.0, 45.0]
    post = [160.0, 169.0, 178.0, 187.0, 196.0, 205.0, 214.0]
    write_srt(
        [(v, v + 1.5, f"c{i}") for i, v in enumerate(pre + post)],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": 0.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -10.0, "support": 22, "kind": "stable"},
        ],
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": [
                "early_path_clock_change",
                "early_boundary_delayed_for_monotonicity",
            ],
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -10.12},
        tmp_path / "cache",
    )
    assert result["applied"] is True
    assert float(result["correction_seconds"]) == 10.0
    cues = parse_srt(output)
    assert abs(cues[0][0] - 20.0) < 0.01
    assert abs(cues[len(pre)][0] - 160.0) < 0.01


def test_otome_single_plateau_is_untouched(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [(10.0, 11.0, "a"), (100.0, 101.0, "b"), (200.0, 201.0, "c")],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": -35.0, "support": 25, "kind": "stable"}
        ],
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["opening_gap_clock_ambiguity"],
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -36.0},
        tmp_path / "cache",
    )
    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "not_piecewise"


def test_scaffold_requires_stt_post_clock_agreement(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [
            (10.0, 11.0, "a"),
            (20.0, 21.0, "b"),
            (30.0, 31.0, "c"),
            (40.0, 41.0, "d"),
            (150.0, 151.0, "e"),
            (160.0, 161.0, "f"),
            (170.0, 171.0, "g"),
            (180.0, 181.0, "h"),
        ],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": 0.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -10.0, "support": 20, "kind": "stable"},
        ],
        "timeline_early_edit_audio_verification": {"required": True},
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -2.0},
        tmp_path / "cache",
    )
    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "speech_clock_disagrees_with_post_plateau"
