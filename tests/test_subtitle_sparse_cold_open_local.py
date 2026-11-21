from pathlib import Path

from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _repair_sparse_cold_open,
    repair_with_embedded_reference_piecewise,
)


def _write(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    write_srt(cues, path, preserve_order=True)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    candidate = [
        (0.1, 4.285, "（足音）"),
        (4.385, 9.123, "本日の当番"),
        (9.223, 12.126, "それから"),
        (14.228, 20.468, "鍵を渡そう"),
        (20.568, 25.139, "♬～"),
        (25.239, 62.276, "♬～"),
        (62.376, 66.947, "♬～"),
        (67.047, 91.806, "♬～"),
        (91.906, 95.609, "♬～"),
        (119.199, 122.603, "朝の会に出席"),
        (122.703, 125.439, "早かったな"),
        (125.539, 128.442, "待っている"),
        (128.542, 130.778, "どこまでも"),
        (130.878, 133.280, "好奇心"),
        (133.380, 135.950, "実は"),
        (136.050, 137.952, "近道"),
        (138.052, 142.623, "相棒"),
        (142.723, 146.627, "迎えに行こう"),
        (200.000, 202.000, "その次"),
    ]
    reference = [
        (14.710, 19.060, "On duty today"),
        (19.060, 22.130, "And then"),
        (24.080, 30.590, "Here is the key"),
        (111.090, 117.010, "III"),
        (119.330, 124.060, "That was quick"),
        (124.060, 128.690, "Still waiting"),
        (128.690, 133.440, "All the way"),
        (133.830, 138.300, "Actually"),
        (138.300, 142.690, "A short cut"),
        (143.170, 145.790, "Let's go meet them"),
        (147.120, 153.170, "Next stop"),
        (153.930, 156.060, "It rained"),
        (200.100, 202.100, "Later"),
    ]
    return (
        _write(tmp_path / "candidate.srt", candidate),
        _write(tmp_path / "reference.srt", reference),
    )


def test_sparse_cold_open_shifts_only_block_before_main_dialogue(tmp_path: Path) -> None:
    candidate, reference = _fixture(tmp_path)
    candidate_cues = parse_srt(candidate)

    output, result = _repair_sparse_cold_open(
        candidate,
        reference,
        candidate_cues,
        parse_srt(reference),
        tmp_path / "cache",
        force=True,
    )

    assert result["applied"] is True
    assert result["reason"] == "sparse_cold_open_fingerprint_improved"
    assert 9.8 <= float(result["correction_seconds"]) <= 10.2
    repaired = parse_srt(output)
    correction = float(result["correction_seconds"])
    assert abs(repaired[1][0] - (candidate_cues[1][0] + correction)) < 0.002
    assert abs(repaired[8][1] - (candidate_cues[8][1] + correction)) < 0.002
    assert repaired[9] == candidate_cues[9]


def test_sparse_cold_open_requires_stable_post_opening_dialogue(tmp_path: Path) -> None:
    candidate, reference = _fixture(tmp_path)
    broken_reference = _write(
        tmp_path / "broken-reference.srt",
        [
            (start + 5.0, end + 5.0, text) if start >= 100.0 else (start, end, text)
            for start, end, text in parse_srt(reference)
        ],
    )

    output, result = _repair_sparse_cold_open(
        candidate,
        broken_reference,
        parse_srt(candidate),
        parse_srt(broken_reference),
        tmp_path / "cache-broken",
        force=True,
    )

    assert output == candidate
    assert result["applied"] is False
    assert result["reason"] == "sparse_cold_open_post_dialogue_not_stable"


def test_piecewise_pipeline_prefers_sparse_cold_open_repair(tmp_path: Path) -> None:
    candidate, reference = _fixture(tmp_path)

    output, result = repair_with_embedded_reference_piecewise(
        candidate,
        reference,
        tmp_path / "cache-pipeline",
        SyncConfig(),
        force=True,
    )

    assert output != candidate
    assert result["applied"] is True
    assert result["reason"] == "sparse_cold_open_fingerprint_improved"
