"""Alignment guard: "new overlaps" from the time map only (sync benchmark, 2026-10).

Seirei Gensouki E11 (Netflix [cc]): the timeline gave a constant -1.0 s clock, the
writer lengthened twenty 0.08 s lines to its 0.3 s minimum, and the guard counted the
text pairs that now overlapped (repeated lines made pairs appear and vanish). It
rejected the clock as "new_overlaps" and the raw file was shown 1 s off.
"""

from __future__ import annotations

from pathlib import Path

from pudge.alignment_guard import validate_alignment_output
from pudge.subtitle_formats import write_exact_srt, write_srt


def _raw() -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    time = 60.0
    for index in range(200):
        text = "はい" if index % 7 == 0 else f"台詞{index}番"
        if index % 10 == 5:
            # a 0.08 s line handing over to the next one at once (a sound-effect caption)
            cues.append((time, time + 0.08, f"（物音{index}）"))
            time += 0.08
        cues.append((time, time + 2.4, text))
        time += 4.0
    return cues


def test_constant_clock_with_minimum_display_lengths_is_accepted(tmp_path: Path) -> None:
    raw = _raw()
    raw_path = tmp_path / "raw.srt"
    write_exact_srt(raw, raw_path)
    final_path = write_srt(
        [(a - 1.0, b - 1.0, t) for a, b, t in raw], tmp_path / "final.srt", preserve_order=True
    )
    verdict = validate_alignment_output(raw_path, final_path)
    assert verdict["status"] == "accepted", verdict
    assert verdict["diagnostics"]["new_overlaps"] == 0


def test_lines_lengthened_beyond_the_minimum_still_count(tmp_path: Path) -> None:
    raw = _raw()
    raw_path = tmp_path / "raw.srt"
    write_exact_srt(raw, raw_path)
    final_path = tmp_path / "final.srt"
    write_exact_srt([(a - 1.0, b + 2.0, t) for a, b, t in raw], final_path)
    verdict = validate_alignment_output(raw_path, final_path)
    assert verdict["status"] == "rejected" and "new_overlaps" in verdict["reason"], verdict


def test_blocks_moved_onto_each_other_are_still_new_overlaps(tmp_path: Path) -> None:
    raw = [(60.0 + i * 4.0, 62.4 + i * 4.0, f"台詞{i}番") for i in range(200)]
    raw_path = tmp_path / "raw.srt"
    write_exact_srt(raw, raw_path)
    # the second half moved 2 s back: every one of its lines runs into a line of the first
    final = [(a, b, t) if i < 100 else (a - 402.0, b - 402.0, t) for i, (a, b, t) in enumerate(raw)]
    final_path = tmp_path / "final.srt"
    write_exact_srt(final, final_path)
    verdict = validate_alignment_output(raw_path, final_path)
    assert verdict["status"] == "rejected" and "new_overlaps" in verdict["reason"], verdict
