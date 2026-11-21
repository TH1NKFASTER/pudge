"""Cold-open cues that ALASS clamped to 0 s keep their spacing when the opening plateau moves.

TV-timed subtitles of a web release: the cold open is on the video's clock (+0.3 s), the
episode after the OP is 9.7 s earlier (the TV OP is longer). ALASS put the whole file on
the episode's clock, which moves the first lines before 0 s: ALASS writes them all at
0 s. The opening-plateau refinement then moved the cold open +9.8 s as a block, so two
lines 5 s apart showed on the same instant, 6 s and 1 s late.
"""

from __future__ import annotations

from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _refine_stt_opening_plateaus

PRE = [3.6, 8.7, 10.9, 14.0, 20.7, 24.4, 29.5, 32.7, 35.2, 38.9, 41.4, 44.7, 47.6, 49.7, 54.6]
POST = [172.9, 176.5, 180.3, 183.7, 187.5, 191.2, 194.8, 196.8, 200.8, 202.8, 206.5, 210.2, 213.9, 217.7]
COLD_OPEN = 0.3
EPISODE = -9.7


def _write(path: Path, starts: list[float], prefix: str) -> list[tuple[float, float, str]]:
    cues = [(start, start + 1.2, f"{prefix}{index}") for index, start in enumerate(starts)]
    write_srt(cues, path)
    return cues


def test_unclamp_restores_the_block_shift_of_cues_at_the_file_start() -> None:
    from pudge.syncing import _unclamp_zero_start_cues

    source = [(3.6, 4.8, "a"), (8.7, 9.9, "b"), (10.9, 12.1, "c"), (14.0, 15.2, "d")]
    aligned = [(0.0, 0.0, "a"), (0.1, 0.4, "b"), (1.2, 2.4, "c"), (4.3, 5.5, "d")]
    restored = _unclamp_zero_start_cues(source, aligned)
    assert [round(cue[0], 3) for cue in restored] == [-6.1, -1.0, 1.2, 4.3]
    # A cue ALASS put at 0 s with a non-negative shift is not touched.
    assert _unclamp_zero_start_cues(
        [(0.0, 1.0, "x"), (5.0, 6.0, "y")], [(0.0, 1.0, "x"), (5.0, 6.0, "y")]
    ) == [
        (0.0, 1.0, "x"),
        (5.0, 6.0, "y"),
    ]


def test_opening_plateau_keeps_spacing_of_cold_open_lines_alass_clamped(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.ja.srt"
    source_cues = _write(source, PRE + POST, "line")
    # ALASS: one block on the episode clock; negative times are written as 0 s.
    write_srt(
        [(max(0.0, start + EPISODE), max(0.0, end + EPISODE), text) for start, end, text in source_cues],
        aligned,
        preserve_order=True,
    )
    # Japanese speech: the cold open on its own clock, the episode on the other.
    write_srt(
        [(start + COLD_OPEN, end + COLD_OPEN, text) for start, end, text in source_cues[: len(PRE)]]
        + [(start + EPISODE, end + EPISODE, text) for start, end, text in source_cues[len(PRE) :]],
        reference,
    )

    output, result = _refine_stt_opening_plateaus(source, aligned, reference, tmp_path / "cache")

    assert result["applied"] is True, result.get("reason")
    cues = parse_srt(output)
    for index, start in enumerate(PRE):
        assert abs(cues[index][0] - (start + COLD_OPEN)) <= 0.15, (index, cues[index][0])
    for index, start in enumerate(POST, start=len(PRE)):
        assert abs(cues[index][0] - (start + EPISODE)) <= 0.15, (index, cues[index][0])
