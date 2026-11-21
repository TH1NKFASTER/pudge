"""Subtitle analysis, formats and rebuild rules review (rv6).

Invented data only.

* A small-cut stretch of more than ~1000 lines (a film a second off) no longer raises
  OverflowError (which cost the file every clock-block, stretch and cold-open correction);
  the chance tail of shorter stretches is computed exactly as before.
* An ASS style with "nan"/"inf" numbers converts (it raised out of the conversion).
* An SRT with a line out of time order is timeline-aligned like the sorted file (the map was
  refused as one that reorders lines).
* A damaged PGS display or object of gigantic size is not drawn (gigabytes asked for one
  display).
* The short-clock search counts every clock of a proposal at once, with the same counts as
  the line-by-line test.
"""

from __future__ import annotations

import math
import random
import struct
from pathlib import Path

import pytest

from pudge import ocr
from pudge.subtitle_formats import convert_to_plain_srt, parse_srt, write_exact_srt
from pudge.subtitles import small_cuts, timeline_alignment

# --- 1. small cuts: the chance tail of a long stretch --------------------------------------------


def test_small_cut_binomial_tail_is_exact_where_it_fitted_and_finite_beyond() -> None:
    def reference(size: int, hits: int, rate: float) -> float:
        return sum(math.comb(size, k) * rate**k * (1.0 - rate) ** (size - k) for k in range(hits, size + 1))

    for size, hits, rate in ((12, 9, 0.2), (300, 120, 0.3), (900, 400, 0.4)):
        assert small_cuts._binomial_tail(size, hits, rate) == reference(size, hits, rate)
    with pytest.raises(OverflowError):
        reference(1200, 720, 0.2)  # what the step used to raise
    tail = small_cuts._binomial_tail(1200, 720, 0.2)
    assert 0.0 <= tail < 1e-100
    assert small_cuts._binomial_tail(3000, 10, 0.3) == pytest.approx(1.0)
    # The log-space sum agrees with the exact one where both work.
    log_rate, log_rest, log_size = math.log(0.4), math.log1p(-0.4), math.lgamma(901)
    logged = sum(
        math.exp(log_size - math.lgamma(k + 1) - math.lgamma(901 - k) + k * log_rate + (900 - k) * log_rest)
        for k in range(400, 901)
    )
    assert logged == pytest.approx(reference(900, 400, 0.4), rel=1e-9)


def test_a_long_stretch_on_another_clock_is_checked_without_overflow() -> None:
    # 2400 lines: the last 1200 a second early; the reference and the speech carry 3 of
    # every 5 of them (one stretch of 1200 lines, 720 on the right clock).
    count = 2400
    rng = random.Random(3)
    source = [10.0]
    for _index in range(count - 1):
        source.append(round(source[-1] + rng.uniform(2.5, 4.5), 3))
    clock = [0.0 if index < 1200 else 1.0 for index in range(count)]
    reference, speech = [], []
    for index, (start, shift) in enumerate(zip(source, clock)):
        if index < 1200 or index % 5 < 3:
            reference.append(start + shift + 0.1)
            speech.append(start + shift + 0.15)
    output = [(start, start + 1.5, f"line {index}") for index, start in enumerate(source)]
    pairs = [(index, index, 0.0) for index in range(count)]
    moved, diagnostics = small_cuts.correct_small_cuts(output, pairs, reference, speech)
    assert diagnostics["applied"] is True
    applied = [row for row in diagnostics["stretches"] if row["applied"]]
    assert applied and applied[0]["lines"] >= 1100 and applied[0]["clock_seconds"] == pytest.approx(1.0)
    assert moved[2000][0] == pytest.approx(output[2000][0] + 1.0)
    assert moved[100] == output[100]


# --- 2. ASS: non-finite style numbers --------------------------------------------------------------


def _ass_with_style_alignment(path: Path, alignment: str) -> Path:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,"
        f"{alignment},10,10,10,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index in range(6):
        lines.append(
            f"Dialogue: 0,0:00:{10 + index * 5:02d}.00,0:00:{12 + index * 5:02d}.00,Default,,0,0,0,,"
            f"{{\\pos(100,{100 + index})}}こんにちは{index}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_an_ass_style_with_a_non_finite_alignment_converts(tmp_path: Path, value: str) -> None:
    source = _ass_with_style_alignment(tmp_path / "sample.ass", value)
    output, result = convert_to_plain_srt(source, tmp_path / "cache", ffmpeg_path="/nonexistent/ffmpeg")
    assert result["converted"] is True and result["method"] == "ass-geometry"
    cues = parse_srt(output)
    assert [text for _start, _end, text in cues] == [f"こんにちは{index}" for index in range(6)]
    # The default alignment (2) was used: the same lines as with an explicit 2.
    expected, _result = convert_to_plain_srt(
        _ass_with_style_alignment(tmp_path / "two.ass", "2"),
        tmp_path / "cache",
        ffmpeg_path="/nonexistent/ffmpeg",
    )
    assert parse_srt(expected) == cues


# --- 3. timeline: a line out of time order ---------------------------------------------------------


def test_an_srt_with_a_line_out_of_order_is_timeline_aligned(tmp_path: Path) -> None:
    rng = random.Random(7)
    cues: list[tuple[float, float, str]] = []
    position = 5.0
    for index in range(260):
        position += rng.uniform(1.0, 6.0)
        length = rng.uniform(1.0, 3.0)
        cues.append((round(position, 3), round(position + length, 3), f"せりふ{index}"))
        position += length
    write_exact_srt(
        [(start + 5.0, end + 5.0, f"line {i}") for i, (start, end, _t) in enumerate(cues)],
        tmp_path / "ref.srt",
    )
    write_exact_srt(cues, tmp_path / "sorted.srt")
    # A line moved to the end of the file (a sign or a song appended by its author).
    write_exact_srt(cues[:40] + cues[41:] + [cues[40]], tmp_path / "unsorted.srt")
    results = {}
    for name in ("sorted.srt", "unsorted.srt"):
        output, result = timeline_alignment.align_subtitle_timelines(
            tmp_path / name, tmp_path / "ref.srt", tmp_path / name.replace(".srt", "-cache")
        )
        results[name] = (output, result)
    sorted_output, sorted_result = results["sorted.srt"]
    output, result = results["unsorted.srt"]
    # Before: "timeline_mapping_reorders_cues", the file refused whatever its clock.
    assert result["reason"] == "applied" and result["sync_was_successful"] is True
    assert result["offset_seconds"] == sorted_result["offset_seconds"] == 5.0
    # The output is in time order, the same lines as from the sorted file.
    assert parse_srt(output) == parse_srt(sorted_output)


# --- 4. OCR: a damaged PGS display ------------------------------------------------------------------


def _segment(kind: int, payload: bytes, pts: int = 90_000) -> bytes:
    return b"PG" + struct.pack(">IIBH", pts, 0, kind, len(payload)) + payload


def _sup(display: tuple[int, int], bitmap: tuple[int, int], pts: int = 90_000) -> bytes:
    width, height = display
    composition = struct.pack(">HHBHBBBB", width, height, 0x10, 1, 0x80, 0, 0, 1)
    composition += struct.pack(">HBBHH", 1, 0, 0, 10, 10)
    palette = bytes([0, 0]) + bytes([1, 235, 128, 128, 255])
    # Run-length rows of colour 1 (a damaged object's data is cut short, as on a broken disc).
    rows = b"".join(bytes([1]) * min(bitmap[0], 16) + b"\x00\x00" for _row in range(min(bitmap[1], 4)))
    obj = struct.pack(">HBB", 1, 0, 0xC0) + (len(rows) + 4).to_bytes(3, "big")
    obj += struct.pack(">HH", bitmap[0], bitmap[1]) + rows
    return (
        _segment(0x16, composition, pts)
        + _segment(0x14, palette, pts)
        + _segment(0x15, obj, pts)
        + _segment(0x80, b"", pts)
    )


def test_a_damaged_pgs_display_is_not_drawn(tmp_path: Path, monkeypatch) -> None:
    sizes: list[tuple[int, int]] = []
    real_new = ocr.Image.new

    def guarded_new(mode, size, *args, **kwargs):
        sizes.append(tuple(size))
        assert max(size) <= 4096, "a gigantic canvas was allocated"
        return real_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(ocr.Image, "new", guarded_new)
    decoded: list[tuple[int, int]] = []
    real_decode = ocr._decode_rle

    def guarded_decode(bitmap, palette):
        decoded.append((bitmap.width, bitmap.height))
        assert max(bitmap.width, bitmap.height) <= 4096, "a gigantic bitmap was decoded"
        return real_decode(bitmap, palette)

    monkeypatch.setattr(ocr, "_decode_rle", guarded_decode)
    sup = tmp_path / "damaged.sup"
    sup.write_bytes(
        _sup((60000, 60000), (8, 2), pts=90_000)  # a damaged display size
        + _sup((1920, 1080), (60000, 60000), pts=180_000)  # a damaged object size
        + _sup((1920, 1080), (8, 2), pts=270_000)  # an ordinary display
    )
    displays = list(ocr.iter_pgs_compositions(sup))
    assert [time for time, _image in displays] == [1.0, 2.0, 3.0]
    assert displays[0][1] is None and displays[1][1] is None
    image = displays[2][1]
    assert image is not None and image.size == (1920, 1080) and image.getbbox() == (10, 10, 18, 12)
    assert decoded == [(8, 2)]


# --- 5. timeline: the short-clock search counts every clock at once --------------------------------


def test_short_clock_hit_counts_match_the_line_by_line_test() -> None:
    rng = random.Random(5)
    for _trial in range(200):
        reference = sorted(
            round(rng.uniform(0, 300), rng.choice([1, 2, 3])) for _ in range(rng.randint(1, 150))
        )
        onsets = [round(rng.uniform(-5, 300), rng.choice([1, 2, 3])) for _ in range(rng.randint(1, 50))]
        proposed = round(rng.uniform(-30, 30), rng.choice([1, 2, 3]))
        clocks = [proposed + step * 0.05 for step in range(-20, 21)]
        expected = [
            sum(timeline_alignment._onset_hits(reference, onset + clock, 0.3) for onset in onsets)
            for clock in clocks
        ]
        assert timeline_alignment._onset_hit_counts(onsets, clocks, reference) == expected
    # Starts at the edge of the tolerance are decided by the same float comparison.
    edge = [timeline_alignment._onset_hits([10.3, 11.0], 10.0 + clock, 0.3) for clock in (0.0, 0.5, 0.7)]
    assert timeline_alignment._onset_hit_counts([10.0], [0.0, 0.5, 0.7], [10.3, 11.0]) == [
        int(v) for v in edge
    ]
