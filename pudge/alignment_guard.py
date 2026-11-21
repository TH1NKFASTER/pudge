"""Mandatory structural check of an aligned subtitle against its source.

Every alignment path (ALASS, timeline, piecewise/group, STT refinement,
consensus shortcuts, cached results) produces a *final* file from a *raw*
one. Whatever the confidence signals say (timing-activity overlap, exact
Jimaku identity, mutual agreement of already-aligned files), a final file
whose time map is structurally destroyed must never be shown.

Measured on the cue mapping itself (not on ALASS stdout):

* new clamped starts — cues that started later in the raw file and now start
  at ~0 s (ALASS pushing a block before the start of the video);
* new overlaps — neighbouring cues that did not overlap in the raw file;
* order inversions and many different lines collapsed onto one start;
* the shift map: spread, number of jumps and the share of cues in short,
  scattered shift runs. A large *piecewise* map (cold open, ad cut, recap
  removed) is legitimate; a large *scattered* one is not.

Music-only cues (``♪〜``) are kept out of the statistics: they legitimately
move to the nearest music and are no speech-clock evidence.

Thresholds are calibrated on a real catastrophic case (Jujutsu Kaisen E11,
43 lines clamped to 0 s, 42 new overlaps, 220 s spread) and on good ones
(constant offsets, cold-open maps, ASS files with parallel lines).
"""

from __future__ import annotations

import hashlib
import re
import statistics
from pathlib import Path
from typing import Any

from .subtitle_formats import parse_srt, write_exact_srt

GUARD_VERSION = "alignment-guard-2"
_MUSIC_ONLY_RE = re.compile(r"^[\s♪♫♬♩〜～~・…\-‐―─()（）]*$")
# Pudge's own writer never starts a line before 0.1 s (write_srt), so a block pushed
# before the start of the video piles up there, not at 0.0 s: with 0.05 s neither the
# clamped-start count nor the standalone check ever saw a file Pudge wrote itself.
_CLAMP_EPSILON = 0.105
_OVERLAP_EPSILON = 0.05
# Subtitle writers lengthen very short lines to about this (write_srt: 0.3 s).
_MIN_DISPLAY_SECONDS = 0.35
# A final file this many lines shorter than its source pairs lines in order (_match).
_ORDERED_MATCH_DROPPED_CUES = 20
_ORDERED_MATCH_DROPPED_FRACTION = 0.1


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def music_only(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value) and bool(_MUSIC_ONLY_RE.match(value))


def _load(path: Path, cache_dir: Path | None) -> list[tuple[float, float, str]]:
    path = Path(path)
    if path.suffix.casefold() == ".srt":
        return parse_srt(path)
    if path.suffix.casefold() in {".ass", ".ssa", ".vtt"} and cache_dir is not None:
        from .subtitle_formats import convert_to_plain_srt

        converted, _result = convert_to_plain_srt(path, Path(cache_dir), force=False, verbose=False)
        if Path(converted).suffix.casefold() == ".srt" and Path(converted).is_file():
            return parse_srt(Path(converted))
    raise ValueError(f"unsupported subtitle for the alignment guard: {path.suffix}")


def _match(
    raw: list[tuple[float, float, str]], final: list[tuple[float, float, str]]
) -> list[tuple[int, int]]:
    """Index pairs raw→final: by position when texts agree, else a text walk."""
    if len(raw) == len(final) and all(_norm(a[2]) == _norm(b[2]) for a, b in zip(raw, final)):
        return [(i, i) for i in range(len(raw))]
    if len(raw) - len(final) >= max(_ORDERED_MATCH_DROPPED_CUES, _ORDERED_MATCH_DROPPED_FRACTION * len(raw)):
        # Whole scenes dropped (a cut broadcast): the nearest-index walk pairs a repeated
        # short line with a copy hundreds of lines away; the order-keeping match does not.
        import difflib

        matcher = difflib.SequenceMatcher(
            None, [_norm(cue[2]) for cue in raw], [_norm(cue[2]) for cue in final], autojunk=False
        )
        return [
            (block.a + offset, block.b + offset)
            for block in matcher.get_matching_blocks()
            for offset in range(block.size)
        ]
    pairs: list[tuple[int, int]] = []
    by_text: dict[str, list[int]] = {}
    for index, cue in enumerate(final):
        by_text.setdefault(_norm(cue[2]), []).append(index)
    used: set[int] = set()
    for index, cue in enumerate(raw):
        options = [j for j in by_text.get(_norm(cue[2]), []) if j not in used]
        if not options:
            continue
        # Nearest remaining occurrence in list order keeps repeated lines apart.
        target = min(options, key=lambda j: abs(j - index))
        used.add(target)
        pairs.append((index, target))
    return pairs


def _overlaps(cues: list[tuple[float, float, str]]) -> set[tuple[str, str]]:
    ordered = sorted(cues, key=lambda cue: (cue[0], cue[1]))
    result: set[tuple[str, str]] = set()
    for left, right in zip(ordered, ordered[1:]):
        if right[0] < left[1] - _OVERLAP_EPSILON:
            result.add((_norm(left[2]), _norm(right[2])))
    return result


def _new_overlaps(
    raw: list[tuple[float, float, str]],
    final: list[tuple[float, float, str]],
    pairs: list[tuple[int, int]],
) -> int:
    """Neighbouring lines that overlap in the final file but did not in the raw one.

    Lines are told apart by their place in the raw file, not by their text: with sets of
    text pairs a repeated "はい" made overlaps appear and vanish. A line the writer only
    lengthened to the minimum display time keeps its raw length here: Seirei Gensouki E11
    (Netflix [cc], 0.08 s lines) was rejected for 20 "new overlaps" on a constant -1.0 s
    map, and the raw file was shown 1 s off. Lines lengthened further still count.
    """
    placed = []
    for i, j in pairs:
        raw_length = max(0.0, raw[i][1] - raw[i][0])
        final_length = max(0.0, final[j][1] - final[j][0])
        if final_length <= max(raw_length + _OVERLAP_EPSILON, _MIN_DISPLAY_SECONDS):
            final_length = min(final_length, raw_length)
        placed.append((final[j][0], final[j][0] + final_length, i))
    placed.sort()
    count = 0
    for (_start, end, left), (next_start, _next_end, right) in zip(placed, placed[1:]):
        if next_start >= end - _OVERLAP_EPSILON:
            continue
        a, b = raw[left], raw[right]
        if min(a[1], b[1]) - max(a[0], b[0]) > _OVERLAP_EPSILON:
            continue  # they overlapped in the raw file already (parallel ASS lines)
        count += 1
    return count


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[position]


def stabilize_music_marker_pairs(
    raw: Path,
    final: Path,
    *,
    cache_dir: Path | None = None,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Keep the two boundaries of one music block on one supported clock.

    Some subtitle sources encode an OP/ED as two adjacent marker cues such as
    ``♪〜`` and ``〜♪`` separated by ~90 seconds. Piecewise subtitle alignment can
    accidentally put the two markers on different clock segments even though no
    spoken cue supports that split, shortening/lengthening the song by many seconds.
    When that happens, use the nearby *dialogue* clock for both markers. A music
    block whose two boundaries already share the same shift is left untouched.

    The repaired track is written to ``destination`` (default: ``final`` itself).
    """
    try:
        raw_cues = _load(Path(raw), cache_dir)
        final_cues = _load(Path(final), cache_dir)
    except (OSError, ValueError) as exc:
        return {"applied": False, "reason": "unreadable", "error": str(exc)[:200]}
    if not raw_cues or not final_cues:
        return {"applied": False, "reason": "empty_track"}

    pairs = _match(raw_cues, final_cues)
    by_raw = {i: j for i, j in pairs}
    speech = [(i, j, final_cues[j][0] - raw_cues[i][0]) for i, j in pairs if not music_only(raw_cues[i][2])]
    if not speech:
        return {"applied": False, "reason": "no_dialogue_support"}

    repaired = list(final_cues)
    repairs: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(raw_cues):
        first = raw_cues[index]
        second = raw_cues[index + 1]
        if not (music_only(first[2]) and music_only(second[2])):
            index += 1
            continue
        first_final_index = by_raw.get(index)
        second_final_index = by_raw.get(index + 1)
        if first_final_index is None or second_final_index is None:
            index += 1
            continue
        block_span = float(second[0]) - float(first[0])
        if not 30.0 <= block_span <= 180.0:
            index += 1
            continue
        first_shift = final_cues[first_final_index][0] - first[0]
        second_shift = final_cues[second_final_index][0] - second[0]
        if abs(first_shift - second_shift) < 1.5:
            index += 2
            continue

        # Prefer several nearby dialogue cues over one boundary line. The endpoint
        # with the closest speech support wins (OP -> dialogue after the end marker;
        # ED -> dialogue before the start marker).
        support_options: list[tuple[float, float, int]] = []
        for marker_raw_index in (index, index + 1):
            marker_start = float(raw_cues[marker_raw_index][0])
            nearby = [
                (abs(float(raw_cues[i][0]) - marker_start), shift)
                for i, _j, shift in speech
                if abs(float(raw_cues[i][0]) - marker_start) <= 45.0
            ]
            if nearby:
                shifts = [shift for _distance, shift in nearby]
                support_options.append(
                    (min(distance for distance, _shift in nearby), statistics.median(shifts), len(shifts))
                )
                continue
            nearest = min(
                ((abs(float(raw_cues[i][0]) - marker_start), shift) for i, _j, shift in speech),
                default=None,
            )
            if nearest is not None and nearest[0] <= 120.0:
                support_options.append((nearest[0], nearest[1], 1))
        if not support_options:
            index += 2
            continue
        support_distance, supported_shift, support_count = min(
            support_options, key=lambda row: (-row[2], row[0])
        )
        # Do not rewrite a pair unless the chosen dialogue clock clearly agrees
        # with at least one existing boundary better than with the invented split.
        if min(abs(supported_shift - first_shift), abs(supported_shift - second_shift)) > 1.0:
            index += 2
            continue

        for raw_index, final_index in ((index, first_final_index), (index + 1, second_final_index)):
            raw_start, raw_end, _text = raw_cues[raw_index]
            duration = max(0.05, float(raw_end) - float(raw_start))
            target_start = float(raw_start) + float(supported_shift)
            if target_start < 0.0:
                break
            repaired[final_index] = (target_start, target_start + duration, final_cues[final_index][2])
        else:
            repairs.append(
                {
                    "raw_indexes": [index, index + 1],
                    "raw_span_seconds": round(block_span, 3),
                    "before_shifts": [round(first_shift, 3), round(second_shift, 3)],
                    "supported_shift": round(float(supported_shift), 3),
                    "support_distance_seconds": round(float(support_distance), 3),
                    "support_cues": int(support_count),
                }
            )
        index += 2

    if not repairs:
        return {"applied": False, "reason": "no_inconsistent_music_marker_pair"}
    target = Path(destination) if destination is not None else Path(final)
    write_exact_srt(repaired, target)
    return {
        "applied": True,
        "reason": "music_marker_pair_clock_stabilized",
        "repairs": repairs,
        "output": str(target),
    }


def validate_alignment_output(
    raw: Path,
    final: Path,
    *,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """``{"status": accepted|needs_verification|rejected, "reason", "diagnostics", ...}``."""
    base: dict[str, Any] = {"version": GUARD_VERSION, "raw": str(raw), "final": str(final)}
    try:
        raw_cues = _load(Path(raw), cache_dir)
        final_cues = _load(Path(final), cache_dir)
    except (OSError, ValueError) as exc:
        return {
            **base,
            "status": "needs_verification",
            "reason": "unreadable",
            "diagnostics": {"error": str(exc)[:200]},
        }
    try:
        base["raw_sha256"] = _sha(Path(raw))
        base["final_sha256"] = _sha(Path(final))
    except OSError:
        pass
    if not raw_cues or not final_cues:
        return {
            **base,
            "status": "needs_verification",
            "reason": "empty_track",
            "diagnostics": {"raw_cues": len(raw_cues), "final_cues": len(final_cues)},
        }

    pairs = _match(raw_cues, final_cues)
    speech = [(i, j) for i, j in pairs if not music_only(raw_cues[i][2])]
    n = len(speech)
    diagnostics: dict[str, Any] = {
        "raw_cues": len(raw_cues),
        "final_cues": len(final_cues),
        "matched": len(pairs),
        "speech_matched": n,
    }
    if n < 5:
        return {
            **base,
            "status": "needs_verification",
            "reason": "too_few_matched_cues",
            "diagnostics": diagnostics,
        }

    new_clamped = sum(1 for i, j in speech if raw_cues[i][0] > 1.0 and final_cues[j][0] <= _CLAMP_EPSILON)
    raw_overlaps = _overlaps([raw_cues[i] for i, _ in speech])
    new_overlaps = _new_overlaps(raw_cues, final_cues, speech)
    ordered = sorted(speech, key=lambda pair: pair[0])
    inversions = 0
    for (i0, j0), (i1, j1) in zip(ordered, ordered[1:]):
        if raw_cues[i1][0] >= raw_cues[i0][0] and final_cues[j1][0] < final_cues[j0][0] - 0.5:
            inversions += 1
    starts: dict[int, set[str]] = {}
    raw_starts: dict[int, set[str]] = {}
    for i, j in speech:
        starts.setdefault(int(round(final_cues[j][0] * 20)), set()).add(_norm(final_cues[j][2]))
        raw_starts.setdefault(int(round(raw_cues[i][0] * 20)), set()).add(_norm(raw_cues[i][2]))
    raw_max_same_start = max((len(v) for v in raw_starts.values()), default=0)
    collapsed = max((len(v) for v in starts.values()), default=0)
    shifts = [final_cues[j][0] - raw_cues[i][0] for i, j in ordered]
    median = statistics.median(shifts)
    spread = _percentile(shifts, 0.95) - _percentile(shifts, 0.05)
    jumps = sum(1 for a, b in zip(shifts, shifts[1:]) if abs(b - a) > 2.0)
    # Runs of (nearly) constant shift; cues in runs shorter than 3 are "scattered".
    runs: list[int] = []
    current = 1
    for a, b in zip(shifts, shifts[1:]):
        if abs(b - a) <= 1.0:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    scattered = sum(length for length in runs if length < 3) / n
    # Clock changes between the stable runs (3+ cues on one shift). The lines a piecewise
    # map squeezes between two clocks (content one side has and the other lacks) ramp
    # across in steps of a few seconds; every step counted as a jump made one cut look
    # like a destroyed map (Konosuba E6 on a TV capture with commercials: three clocks,
    # 22 squeezed lines, 12 "jumps"). Short runs stay measured by ``scattered``.
    stable_levels: list[float] = []
    position = 0
    for length in runs:
        if length >= 3:
            stable_levels.append(statistics.median(shifts[position : position + length]))
        position += length
    stable_jumps = sum(1 for a, b in zip(stable_levels, stable_levels[1:]) if abs(b - a) > 2.0)
    far = sum(1 for value in shifts if abs(value - median) > 30.0) / n
    diagnostics.update(
        {
            "new_clamped_starts": new_clamped,
            "raw_overlaps": len(raw_overlaps),
            "new_overlaps": new_overlaps,
            "order_inversions": inversions,
            "max_lines_on_one_start": collapsed,
            "raw_max_lines_on_one_start": raw_max_same_start,
            "median_shift": round(median, 3),
            "shift_span_p05_p95": round(spread, 3),
            "shift_min": round(min(shifts), 3),
            "shift_max": round(max(shifts), 3),
            "shift_jumps": jumps,
            "stable_shift_jumps": stable_jumps,
            "shift_runs": len(runs),
            "scattered_fraction": round(scattered, 4),
            "far_from_median_fraction": round(far, 4),
        }
    )

    reasons: list[str] = []
    if new_clamped >= max(5, int(0.02 * n)):
        reasons.append("clamped_starts")
    if new_overlaps >= max(8, int(0.04 * n)):
        reasons.append("new_overlaps")
    if inversions >= max(5, int(0.02 * n)):
        reasons.append("order_inversions")
    if collapsed >= 5 and collapsed > raw_max_same_start + 2:
        reasons.append("collapsed_lines")
    if spread > 30.0 and (stable_jumps > max(6, int(0.03 * n)) or scattered > 0.10):
        reasons.append("scattered_time_map")
    if reasons:
        return {**base, "status": "rejected", "reason": ",".join(reasons), "diagnostics": diagnostics}
    if len(pairs) < 0.6 * min(len(raw_cues), len(final_cues)):
        return {
            **base,
            "status": "needs_verification",
            "reason": "cue_mapping_incomplete",
            "diagnostics": diagnostics,
        }
    note = "large_piecewise_map" if spread > 30.0 else "ok"
    return {**base, "status": "accepted", "reason": note, "diagnostics": diagnostics}


def validate_final_standalone(final: Path) -> dict[str, Any]:
    """Check a prepared file without its source (legacy caches, library rows).

    Only the unmistakable signature of a destroyed map is rejected: many
    different dialogue lines piled up at the very start of the video. Good
    subtitles practically never have five dialogue cues starting at 0 s.
    """
    base: dict[str, Any] = {"version": GUARD_VERSION, "final": str(final), "mode": "standalone"}
    try:
        cues = _load(Path(final), None)
    except (OSError, ValueError) as exc:
        return {
            **base,
            "status": "needs_verification",
            "reason": "unreadable",
            "diagnostics": {"error": str(exc)[:200]},
        }
    at_zero = {_norm(text) for start, _end, text in cues if start <= _CLAMP_EPSILON and not music_only(text)}
    diagnostics = {"cues": len(cues), "distinct_lines_at_zero": len(at_zero)}
    if len(at_zero) >= 5:
        return {**base, "status": "rejected", "reason": "lines_piled_at_zero", "diagnostics": diagnostics}
    return {**base, "status": "accepted", "reason": "ok", "diagnostics": diagnostics}
