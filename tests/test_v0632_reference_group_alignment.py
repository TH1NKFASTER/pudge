from pathlib import Path

import pytest

from anime_mpv.subtitle_formats import parse_srt, write_srt
from anime_mpv.syncing import refine_with_embedded_reference_groups


def _fixture_cues() -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str]]]:
    candidate: list[tuple[float, float, str]] = []
    reference: list[tuple[float, float, str]] = []
    for index in range(20):
        base = 5.0 + index * 7.0
        candidate.extend(
            [
                (base + 0.25, base + 1.85, f"日本語 {index}a"),
                (base + 1.95, base + 4.25, f"日本語 {index}b"),
            ]
        )
        reference.append((base, base + 4.0, f"English {index}"))
    return candidate, reference


def test_reference_group_refinement_handles_split_and_merged_cues(tmp_path: Path) -> None:
    basis_cues, reference_cues = _fixture_cues()
    # Simulate a later piecewise stage that damaged only the cold open. Group
    # matching must use the pre-piecewise clock but write corrected times into
    # the current aligned file.
    aligned_cues = list(basis_cues)
    aligned_cues[0] = (
        aligned_cues[0][0] - 1.2,
        aligned_cues[0][1] - 1.2,
        aligned_cues[0][2],
    )
    aligned_cues[1] = (
        aligned_cues[1][0] - 1.2,
        aligned_cues[1][1] - 1.2,
        aligned_cues[1][2],
    )

    aligned = tmp_path / "aligned.srt"
    basis = tmp_path / "basis.srt"
    reference = tmp_path / "reference.srt"
    write_srt(aligned_cues, aligned)
    write_srt(basis_cues, basis)
    write_srt(reference_cues, reference)

    output, result = refine_with_embedded_reference_groups(
        aligned,
        reference,
        tmp_path / "cache",
        matching_basis=basis,
        force=True,
    )

    assert result["applied"] is True
    assert result["coverage"] > 0.85
    assert result["split_merge_groups"] >= 15
    refined = parse_srt(output)
    assert refined[0][0] == pytest.approx(reference_cues[0][0], abs=0.03)
    assert refined[1][1] == pytest.approx(reference_cues[0][1], abs=0.03)
    # Split/merge matching may move the whole Japanese group, but must never
    # stretch its internal cue durations or pause to imitate one long English cue.
    assert refined[2][0] == pytest.approx(reference_cues[1][0], abs=0.03)
    assert refined[3][1] == pytest.approx(reference_cues[1][1], abs=0.03)
    for original, updated in zip(basis_cues[2:4], refined[2:4]):
        assert updated[1] - updated[0] == pytest.approx(original[1] - original[0], abs=0.002)
    assert refined[3][0] - refined[2][1] == pytest.approx(
        basis_cues[3][0] - basis_cues[2][1], abs=0.002
    )


def test_reference_group_refinement_rejects_basis_with_changed_cue_count(tmp_path: Path) -> None:
    candidate, reference_cues = _fixture_cues()
    aligned = tmp_path / "aligned.srt"
    basis = tmp_path / "basis.srt"
    reference = tmp_path / "reference.srt"
    write_srt(candidate, aligned)
    write_srt(candidate[:-1], basis)
    write_srt(reference_cues, reference)

    output, result = refine_with_embedded_reference_groups(
        aligned,
        reference,
        tmp_path / "cache",
        matching_basis=basis,
        force=True,
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason"] == "reference_groups_basis_mismatch"
