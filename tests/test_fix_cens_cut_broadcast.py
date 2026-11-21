"""Broadcast versions with scenes cut out (censored or shortened TV airings): the speech
text clock read as the kept regions, each on its own clock, with the lines of a cut
scene dropped. Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.alignment_guard import _match, validate_alignment_output
from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.cut_clocks import fit_text_clock_cuts

_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"

# (lines, video shift or None for a cut scene)
_PLAN = [(30, 60.0), (12, None), (30, 0.0), (12, None), (30, -60.0), (10, None), (30, -110.0)]


def _lines(count: int, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_KANA) for _ in range(rng.randint(9, 16))) for _ in range(count)]


def _episode(plan=_PLAN):
    """Source cues (5 s apart), their region shift (None = cut) and the speech."""
    total = sum(count for count, _shift in plan)
    texts = _lines(total)
    cues: list[tuple[float, float, str]] = []
    shifts: list[float | None] = []
    start = 8.0
    for count, shift in plan:
        for _ in range(count):
            cues.append((start, start + 3.0, texts[len(cues)]))
            shifts.append(shift)
            start += 5.0
    speech = [(a + s, b + s, t) for (a, b, t), s in zip(cues, shifts) if s is not None]
    return cues, shifts, speech


def _text_map_shifts(cues, shifts):
    """What the text clock reads: kept lines on their clock (small noise), the lines of a
    cut ramp across the cut (the text alignment interpolates through it)."""
    values: list[float | None] = []
    rng = random.Random(3)
    index = 0
    while index < len(cues):
        if shifts[index] is not None:
            values.append(float(shifts[index]) + rng.uniform(-0.3, 0.3))
            index += 1
            continue
        low = index
        while index < len(cues) and shifts[index] is None:
            index += 1
        left = float(shifts[low - 1])
        right = float(shifts[index])
        span = index - low + 1
        for k in range(low, index):
            values.append(left + (right - left) * (k - low + 1) / span)
    return values


def test_fit_keeps_the_regions_and_drops_the_cut_scenes():
    cues, shifts, _speech = _episode()
    fit = fit_text_clock_cuts([c[0] for c in cues], [c[1] for c in cues], _text_map_shifts(cues, shifts))
    assert fit["accepted"] is True, fit
    assert [round(c["shift_seconds"]) for c in fit["clocks"]] == [60, 0, -60, -110]
    assert [j["kind"] for j in fit["jumps"]] == ["missing_source_content"] * 3
    for index, (value, truth) in enumerate(zip(fit["shifts"], shifts)):
        if truth is None:
            assert value is None, index
        else:
            assert value is not None and abs(value - truth) < 0.35, index
    assert sorted(fit["dropped"]) == [k for k, s in enumerate(shifts) if s is None]


def test_fit_stays_out_of_a_normal_episode_and_a_single_cut():
    starts = [8.0 + 5.0 * i for i in range(150)]
    ends = [s + 3.0 for s in starts]
    one_clock = fit_text_clock_cuts(starts, ends, [12.0 + (0.2 if i % 3 else -0.2) for i in range(150)])
    assert one_clock["accepted"] is False
    cues, shifts, _speech = _episode([(70, 30.0), (12, None), (70, -30.0)])
    single_cut = fit_text_clock_cuts(
        [c[0] for c in cues], [c[1] for c in cues], _text_map_shifts(cues, shifts)
    )
    assert single_cut["accepted"] is False
    assert single_cut["reason"] == "too_few_strong_clocks"


def test_fit_refuses_a_drifting_text_map_and_regions_out_of_order():
    starts = [8.0 + 5.0 * i for i in range(150)]
    ends = [s + 3.0 for s in starts]
    drifting = fit_text_clock_cuts(starts, ends, [130.0 - 1.5 * i for i in range(150)])
    assert drifting["accepted"] is False
    # The third region would play before the end of the second one: not a cut.
    cues, shifts, _speech = _episode([(30, 60.0), (12, None), (30, 0.0), (12, None), (30, -300.0)])
    values = _text_map_shifts(cues, shifts)
    overlap = fit_text_clock_cuts([c[0] for c in cues], [c[1] for c in cues], values)
    assert overlap["accepted"] is False
    assert overlap["reason"] == "clocks_overlap_in_video"


def test_fit_needs_most_lines_kept():
    cues, shifts, _speech = _episode([(14, 60.0), (40, None), (14, 0.0), (40, None), (14, -60.0)])
    fit = fit_text_clock_cuts([c[0] for c in cues], [c[1] for c in cues], _text_map_shifts(cues, shifts))
    assert fit["accepted"] is False
    assert fit["reason"] == "too_many_dropped_cues"


def test_cut_output_checks_every_region_against_the_speech(tmp_path: Path):
    cues, shifts, speech = _episode()
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    values = _text_map_shifts(cues, shifts)
    mapped = [c[0] + v for c, v in zip(cues, values)]
    result = syncing._text_clock_cut_output(cues, mapped, reference, tmp_path / "clock.srt")
    assert result["accepted"] is True, result
    out = parse_srt(Path(result["output"]))
    kept = [(c, s) for c, s in zip(cues, shifts) if s is not None]
    assert len(out) == len(kept)
    assert all(abs(o[0] - c[0] - s) < 0.35 and o[2] == c[2] for o, (c, s) in zip(out, kept))
    # The speech of the third region belongs to other lines: its clock has no text.
    wrong = [(a, b, t) for a, b, t in speech]
    third = [k for k, s in enumerate([s for s in shifts if s is not None]) if 60 <= k < 90]
    rng = random.Random(5)
    for k in third:
        a, b, _t = wrong[k]
        wrong[k] = (a, b, "".join(rng.choice(_KANA) for _ in range(12)))
    write_srt(wrong, reference)
    refused = syncing._text_clock_cut_output(cues, mapped, reference, tmp_path / "clock2.srt")
    assert refused["accepted"] is False


# Five cuts: more clock changes than the plateau fit takes.
_FIVE_CUTS = [
    (26, 140.0),
    (10, None),
    (26, 90.0),
    (8, None),
    (26, 50.0),
    (10, None),
    (26, 0.0),
    (6, None),
    (26, -30.0),
    (8, None),
    (26, -70.0),
]


def test_speech_fallback_reads_a_cut_broadcast(tmp_path: Path, monkeypatch):
    cues, shifts, speech = _episode(_FIVE_CUTS)
    source = tmp_path / "source.ja.srt"
    write_srt(cues, source)
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    video = tmp_path / "broadcast.ts"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        syncing, "prepare_japanese_stt_reference", lambda *a, **k: (reference, {"model": "t"})
    )
    monkeypatch.setattr(
        syncing,
        "synchronize_with_alass",
        lambda _r, subtitle, *_a, **_k: (subtitle, {"sync_was_successful": False, "reason": "alass_failed"}),
    )
    out, result = syncing._try_japanese_stt_fallback(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert out is not None, result.get("stt_text_alignment")
    assert result["engine"] == "japanese-stt+text-clock-cuts"
    final = parse_srt(out)
    kept = [(c, s) for c, s in zip(cues, shifts) if s is not None]
    assert len(final) == len(kept)
    within = sum(1 for f, (c, s) in zip(final, kept) if abs(f[0] - c[0] - s) <= 0.3)
    assert within >= 0.95 * len(kept)
    syncing._guard_alignment_result(source, out, result, tmp_path / "cache")
    assert syncing.subtitle_quality_accepted(result)[0] is True


def test_guard_pairs_lines_in_order_when_whole_scenes_are_dropped(tmp_path: Path):
    repeated = "はい"
    raw = []
    for k in range(120):
        text = repeated if k % 10 == 0 else f"せりふ{k}です"
        raw.append((5.0 * k, 5.0 * k + 2.0, text))
    final = [(a - 40.0 if k >= 60 else a, b - 40.0 if k >= 60 else b, t) for k, (a, b, t) in enumerate(raw)]
    final = final[:40] + final[60:]  # 20 lines of a cut scene dropped
    pairs = _match(raw, final)
    assert all(b0 < b1 for (_a0, b0), (_a1, b1) in zip(pairs, pairs[1:]))
    assert len(pairs) == 100 and all(raw[a][2] == final[b][2] for a, b in pairs)
    source, output = tmp_path / "raw.srt", tmp_path / "final.srt"
    write_srt(raw, source)
    write_srt(final, output, preserve_order=True)
    verdict = validate_alignment_output(source, output)
    assert verdict["status"] != "rejected", verdict
    assert verdict["diagnostics"]["order_inversions"] == 0
