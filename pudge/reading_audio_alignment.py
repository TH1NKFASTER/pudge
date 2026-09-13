from __future__ import annotations

import bisect
import re
import statistics
import unicodedata
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein


_IGNORED_CATEGORIES = {"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Zl", "Zp", "Zs"}
_STRONG_PUNCTUATION = frozenset("。！？!?…")
_SOFT_PUNCTUATION = frozenset("、，,；;：:")

_IMAGE_PARAGRAPH_RE = re.compile(r"^\[\[PUDGE_LN_IMAGE_URL:.+?\]\]$")


def chapter_audio_text(value: str) -> str:
    """Return the LN text that participates in reader/audio offsets.

    Inline image placeholders are rendered as zero-width figures in the LN
    reader, so audiobook alignment must exclude them from the character clock
    as well.  Otherwise the first spoken prose can appear hundreds of chars
    into the chapter even though the reader considers it offset zero.
    """

    lines = []
    for line in str(value or "").splitlines():
        stripped = line.strip()
        if _IMAGE_PARAGRAPH_RE.fullmatch(stripped):
            continue
        lines.append(line)
    return "\n".join(lines)

def normalize_reading_text(value: str) -> str:
    """Normalize LN/STT text while preserving every spoken letter and digit."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) not in _IGNORED_CATEGORIES
    )


def _punctuation_boundaries(value: str) -> list[dict[str, int]]:
    """Return spoken-text offsets where punctuation can explain a pause."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    offset = 0
    strengths: dict[int, int] = {}
    for character in normalized:
        if character in "\r\n":
            if offset:
                strengths[offset] = max(3, strengths.get(offset, 0))
            continue
        if character in _STRONG_PUNCTUATION:
            if offset:
                strengths[offset] = max(3, strengths.get(offset, 0))
            continue
        if character in _SOFT_PUNCTUATION:
            if offset:
                strengths[offset] = max(1, strengths.get(offset, 0))
            continue
        if character.isspace() or unicodedata.category(character) in _IGNORED_CATEGORIES:
            continue
        offset += 1
    return [
        {"offset": boundary, "strength": strength}
        for boundary, strength in sorted(strengths.items())
    ]


def _median(values: list[float]) -> float:
    rows = sorted(float(value) for value in values)
    middle = len(rows) // 2
    if len(rows) % 2:
        return rows[middle]
    return (rows[middle - 1] + rows[middle]) / 2


def _dense_anchor_clock(
    matches: Iterable[tuple[int, int]],
    *,
    anchor_size: int,
    transcript_times: list[float],
    chapter_length: int,
) -> list[dict[str, float | int]]:
    """Keep every known character boundary inside each exact n-gram match."""

    candidates: dict[int, list[float]] = {}
    for novel_offset, transcript_offset in matches:
        available = min(
            max(0, int(anchor_size)),
            max(0, int(chapter_length) - int(novel_offset)),
            max(0, len(transcript_times) - 1 - int(transcript_offset)),
        )
        for delta in range(available + 1):
            candidates.setdefault(int(novel_offset) + delta, []).append(
                float(transcript_times[int(transcript_offset) + delta])
            )

    clock: list[dict[str, float | int]] = []
    last_time = float("-inf")
    for offset, values in sorted(candidates.items()):
        time = _median(values)
        if time <= last_time + 0.0005:
            later = [value for value in values if value > last_time + 0.0005]
            if not later:
                continue
            time = _median(later)
        clock.append({"offset": int(offset), "time": round(time, 3)})
        last_time = time
    return clock


def _linear_offset_at_time(
    anchors: list[dict[str, Any]],
    position: float,
) -> float:
    if not anchors:
        return 0.0
    position = float(position)
    for left, right in pairwise(anchors):
        left_time = float(left["time"])
        right_time = float(right["time"])
        if position <= right_time:
            ratio = (position - left_time) / max(0.02, right_time - left_time)
            return float(left["offset"]) + max(0.0, min(1.0, ratio)) * (
                float(right["offset"]) - float(left["offset"])
            )
    return float(anchors[-1]["offset"])


def _inject_punctuation_pause_anchors(
    anchors: list[dict[str, Any]],
    boundaries: Iterable[dict[str, Any]],
    speech_regions: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Hold the text clock at nearby punctuation during acoustic silence.

    This path runs on dense clocks.  Keep all range lookups logarithmic instead
    of rescanning every anchor/boundary for every speech gap.
    """

    if len(anchors) < 2:
        return anchors, 0
    regions = _clip_activity_regions(
        speech_regions,
        float(anchors[0]["time"]),
        float(anchors[-1]["time"]),
    )
    if len(regions) < 2:
        return anchors, 0

    boundary_rows = [
        {
            "offset": int(row.get("offset") or 0),
            "strength": int(row.get("strength") or 0),
            "order": order,
        }
        for order, row in enumerate(boundaries)
        if isinstance(row, dict) and int(row.get("offset") or 0) > 0
    ]
    if not boundary_rows:
        return anchors, 0

    anchor_times = [float(row["time"]) for row in anchors]
    anchor_offsets = [float(row["offset"]) for row in anchors]
    sorted_boundaries = sorted(boundary_rows, key=lambda row: (int(row["offset"]), int(row["order"])))
    boundary_offsets = [int(row["offset"]) for row in sorted_boundaries]

    def offset_at(position: float) -> float:
        right_index = bisect.bisect_left(anchor_times, float(position))
        if right_index <= 0:
            left_index, right_index = 0, 1
        elif right_index >= len(anchors):
            return anchor_offsets[-1]
        else:
            left_index = right_index - 1
        left_time, right_time = anchor_times[left_index], anchor_times[right_index]
        ratio = (float(position) - left_time) / max(0.02, right_time - left_time)
        ratio = max(0.0, min(1.0, ratio))
        return anchor_offsets[left_index] + ratio * (anchor_offsets[right_index] - anchor_offsets[left_index])

    additions: list[dict[str, float | int]] = []
    pause_windows: list[tuple[float, float, int]] = []
    used: set[int] = set()
    for previous, following in pairwise(regions):
        gap_start = float(previous["end"])
        gap_end = float(following["start"])
        gap = gap_end - gap_start
        if gap < 0.12:
            continue

        before_index = bisect.bisect_right(anchor_times, gap_start + 1e-6) - 1
        after_index = bisect.bisect_left(anchor_times, gap_end - 1e-6)
        if before_index < 0 or after_index >= len(anchors):
            continue
        minimum_offset = int(anchors[before_index]["offset"])
        maximum_offset = int(anchors[after_index]["offset"])
        if maximum_offset < minimum_offset:
            continue

        expected = offset_at((gap_start + gap_end) / 2)
        lo = bisect.bisect_left(boundary_offsets, minimum_offset)
        hi = bisect.bisect_right(boundary_offsets, maximum_offset)
        choices: list[tuple[float, int, dict[str, int]]] = []
        span = max(1.0, float(maximum_offset - minimum_offset))
        for row in sorted_boundaries[lo:hi]:
            offset = int(row["offset"])
            strength = int(row["strength"])
            if offset in used:
                continue
            if strength < 3 and gap < 0.18:
                continue
            allowance = max(
                2.0 if strength >= 3 else 1.25,
                span * (0.45 if strength >= 3 else 0.30),
            )
            distance = abs(float(offset) - expected)
            if distance > allowance:
                continue
            choices.append((distance - strength * 0.12, int(row["order"]), row))
        if not choices:
            continue

        chosen = min(choices, key=lambda item: (item[0], item[1]))[2]
        pause_offset = int(chosen["offset"])
        hold_offset = max(0.0, float(pause_offset) - 0.001)
        used.add(pause_offset)
        pause_windows.append((gap_start, gap_end, pause_offset))
        hold_end = max(gap_start, gap_end - 0.001)
        additions.extend(
            [
                {"offset": hold_offset, "time": round(gap_start, 3)},
                {"offset": hold_offset, "time": round(hold_end, 3)},
                {"offset": pause_offset, "time": round(gap_end, 3)},
            ]
        )

    if not additions:
        return anchors, 0

    window_starts = [row[0] for row in pause_windows]
    retained: list[dict[str, Any]] = []
    for row in anchors:
        row_time = float(row["time"])
        row_offset = float(row.get("offset") or 0.0)
        next_window_index = bisect.bisect_left(window_starts, row_time)
        if next_window_index < len(pause_windows):
            next_gap_start, next_gap_end, next_pause_offset = pause_windows[next_window_index]
            if (
                next_gap_end - next_gap_start >= 0.25
                and next_gap_start - 0.08 <= row_time < next_gap_start
                and abs(row_offset - float(next_pause_offset)) <= 1e-6
            ):
                # A word onset only a few milliseconds before a long silence is
                # acoustically impossible. Prefer the VAD/punctuation boundary
                # and start that word when speech resumes.
                continue
        window_index = bisect.bisect_right(window_starts, row_time) - 1
        if window_index >= 0:
            gap_start, gap_end, pause_offset = pause_windows[window_index]
            if gap_start + 1e-6 < row_time < gap_end - 1e-6:
                continue
            # A precision word clock may place the previous word's exact end
            # (e.g. offset 11) right at the acoustic gap start.  That would make
            # the reader switch to the next word before the silence.  Let the
            # synthetic 10.999 hold own that boundary instead.
            if (
                row_offset >= float(pause_offset) - 1e-6
                and row_offset <= float(pause_offset) + 1e-6
                and (
                    abs(row_time - gap_start) <= 1e-6
                    or (
                        gap_end - gap_start >= 0.25
                        and gap_start - 0.08 <= row_time < gap_start
                    )
                )
            ):
                # Precision STT occasionally stamps the *next* word a few
                # milliseconds before a long VAD silence (real S&W v2 ch3:
                # 「まず」 at 14085.935, silence starts 14085.960). Treating that
                # tiny pre-gap tail as speech makes the next word light up early
                # and forces the following phrase to race/catch up. Let the
                # punctuation hold own the whole gap and start the word when
                # acoustic activity resumes.
                continue
        retained.append(dict(row))

    merged = retained + additions
    merged.sort(key=lambda row: (float(row["time"]), float(row["offset"])))
    output: list[dict[str, Any]] = []
    for row in merged:
        time_value = round(float(row["time"]), 3)
        raw_offset = float(row["offset"])
        rounded_offset = round(raw_offset)
        offset: float | int = (
            int(rounded_offset)
            if abs(raw_offset - rounded_offset) < 1e-6
            else round(raw_offset, 4)
        )
        if (
            output
            and time_value == float(output[-1]["time"])
            and abs(float(offset) - float(output[-1]["offset"])) < 1e-6
        ):
            continue
        if output and float(offset) < float(output[-1]["offset"]) - 1e-6:
            continue
        output.append({"offset": offset, "time": time_value})
    return output, len(pause_windows)



def _segment_text(segment: dict[str, Any]) -> str:
    text = str(segment.get("text") or "")
    if text.strip():
        return text
    words = segment.get("words")
    if isinstance(words, list):
        return "".join(
            str(row.get("word") or row.get("text") or "")
            for row in words
            if isinstance(row, dict)
        )
    return ""


def _japanese_chapter_number(value: int) -> str:
    value = max(1, int(value))
    digits = "一二三四五六七八九"
    if value <= 9:
        return digits[value - 1]
    if value == 10:
        return "十"
    if value < 20:
        return "十" + digits[value - 11]
    tens, ones = divmod(value, 10)
    if tens <= 9:
        return digits[tens - 1] + "十" + (digits[ones - 1] if ones else "")
    return str(value)


def _chapter_marker_aliases(title: str, chapter_index: int | None) -> set[str]:
    aliases: set[str] = set()
    normalized_title = normalize_reading_text(title)
    if normalized_title:
        aliases.add(normalized_title)
    if (
        chapter_index is not None
        and chapter_index >= 0
        and normalized_title.startswith("第")
        and normalized_title.endswith("幕")
    ):
        number = int(chapter_index) + 1
        aliases.add(normalize_reading_text(f"第{number}幕"))
        aliases.add(normalize_reading_text(f"第{_japanese_chapter_number(number)}幕"))
    return {row for row in aliases if len(row) >= 2}


def _find_spoken_chapter_marker(
    segments: Iterable[dict[str, Any]],
    *,
    title: str,
    chapter_index: int | None,
    search_start: float,
    search_end: float,
    reference_time: float,
) -> dict[str, Any] | None:
    """Find a spoken chapter label such as `第二幕` near a rough boundary.

    The marker is only a locator.  It must never advance the LN text clock.
    """

    aliases = _chapter_marker_aliases(title, chapter_index)
    if not aliases:
        return None
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start") or 0.0)
            end = max(start + 0.02, float(segment.get("end") or start + 0.02))
        except (TypeError, ValueError):
            continue
        if end < search_start - 0.1:
            continue
        if start > search_end + 0.1:
            break
        surface = normalize_reading_text(_segment_text(segment))
        if len(surface) < 2 or len(surface) > 24:
            continue
        best = 0.0
        for alias in aliases:
            if alias == surface:
                score = 100.0
            elif alias in surface:
                score = 96.0
            elif surface in alias and len(surface) >= max(2, len(alias) - 1):
                score = 92.0
            else:
                score = max(float(fuzz.ratio(alias, surface)), float(fuzz.partial_ratio(alias, surface)))
            best = max(best, score)
        if best < 86.0:
            continue
        distance = abs(start - float(reference_time))
        # Prefer a marker slightly before the first dense prose anchor.
        after_penalty = 4.0 if start > float(reference_time) + 3.0 else 0.0
        candidates.append((distance + after_penalty, -best, {"start": start, "end": end, "score": best, "text": surface}))
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row[0], row[1]))[2]


def _marker_hold_fallback(
    clock: list[dict[str, float | int]],
    segments: Iterable[dict[str, Any]],
    marker: dict[str, Any],
    *,
    search_end: float,
    max_rate: float = 10.0,
) -> tuple[list[dict[str, float | int]], float | None]:
    """Hold offset zero through a spoken chapter marker when prefix text is weak."""

    marker_end = float(marker["end"])
    story_floor = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or start)
        except (TypeError, ValueError):
            continue
        if end <= marker_end + 0.08:
            continue
        if start > search_end:
            break
        surface = normalize_reading_text(_segment_text(segment))
        if len(surface) < 4:
            continue
        story_floor = max(marker_end, start)
        break
    if story_floor is None:
        return clock, None

    repaired: list[dict[str, float | int]] = [
        {"offset": 0, "time": round(float(marker["start"]), 3)},
        {"offset": 0, "time": round(float(story_floor), 3)},
    ]
    joined = False
    for index, row in enumerate(clock):
        offset = float(row.get("offset") or 0.0)
        time = float(row.get("time") or 0.0)
        if time <= story_floor + 0.02 or offset <= 0.0:
            continue
        rate = offset / max(0.02, time - story_floor)
        if rate > max_rate:
            continue
        for tail in clock[index:]:
            if float(tail.get("time") or 0.0) <= float(repaired[-1]["time"]) + 0.0005:
                continue
            if float(tail.get("offset") or 0.0) < float(repaired[-1]["offset"]) - 1e-6:
                continue
            repaired.append(dict(tail))
        joined = True
        break
    return (repaired if joined else clock), story_floor


def _clock_offset_at(
    clock: list[dict[str, float | int]],
    position: float,
) -> float | None:
    rows = [row for row in clock if isinstance(row, dict)]
    if not rows:
        return None
    times = [float(row.get("time") or 0.0) for row in rows]
    right = bisect.bisect_left(times, float(position))
    if right <= 0:
        return float(rows[0].get("offset") or 0.0)
    if right >= len(rows):
        return float(rows[-1].get("offset") or 0.0)
    left = right - 1
    left_time = times[left]
    right_time = times[right]
    left_offset = float(rows[left].get("offset") or 0.0)
    right_offset = float(rows[right].get("offset") or left_offset)
    if right_time <= left_time + 1e-6:
        return max(left_offset, right_offset)
    ratio = (float(position) - left_time) / (right_time - left_time)
    return left_offset + (right_offset - left_offset) * max(0.0, min(1.0, ratio))



def _infer_acoustic_chapter_marker(
    chapter_text: str,
    segments: Iterable[dict[str, Any]],
    clock: list[dict[str, float | int]],
    *,
    chapter_title: str,
    search_start: float,
    search_end: float,
) -> dict[str, Any] | None:
    """Infer a spoken `第X幕` even when Whisper mistranscribes the label.

    A marker-like first speech burst alone is not enough: ordinary prose can
    also be followed by a pause.  We only accept the acoustic marker when the
    first one-to-three STT phrases *after* that pause independently match the
    beginning of the LN while the existing clock is already several
    characters ahead.  That positive, repeated offset bias is the evidence
    that the title burst was falsely consumed as readable prose.
    """

    normalized_title = normalize_reading_text(chapter_title)
    if not (normalized_title.startswith("第") and normalized_title.endswith("幕")):
        return None
    normalized = normalize_reading_text(chapter_text)
    if len(normalized) < 40 or len(clock) < 3:
        return None

    shape: dict[str, float] | None = None
    rows = [row for row in clock[:10] if isinstance(row, dict)]
    for index in range(len(rows) - 2):
        left = rows[index]
        right = rows[index + 1]
        left_time = float(left.get("time") or 0.0)
        right_time = float(right.get("time") or 0.0)
        left_offset = float(left.get("offset") or 0.0)
        right_offset = float(right.get("offset") or left_offset)
        advance = right_offset - left_offset
        duration = right_time - left_time
        if left_time < search_start - 0.5 or left_time > search_end:
            continue
        if left_offset > 3.0 or not (5.0 <= advance <= 96.0):
            continue
        if not (0.18 <= duration <= 6.0):
            continue
        for hold in rows[index + 2 : min(len(rows), index + 5)]:
            hold_time = float(hold.get("time") or 0.0)
            hold_offset = float(hold.get("offset") or 0.0)
            silence = hold_time - right_time
            if abs(hold_offset - right_offset) > 1.5:
                continue
            if not (0.45 <= silence <= 8.0):
                continue
            shape = {
                "start": left_time,
                "end": right_time,
                "story_floor": hold_time,
                "false_advance": advance,
            }
            break
        if shape is not None:
            break
    if shape is None:
        return None

    prefix = normalized[: min(len(normalized), 360)]
    evidence: list[dict[str, float | int]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start") or 0.0)
            end = max(start + 0.02, float(segment.get("end") or start + 0.02))
        except (TypeError, ValueError):
            continue
        if end < float(shape["story_floor"]) - 0.15:
            continue
        if start > min(search_end, float(shape["story_floor"]) + 18.0):
            break
        surface = normalize_reading_text(_segment_text(segment))
        if len(surface) < 6:
            continue
        exact_start = prefix.find(surface)
        if exact_start >= 0:
            score = 100.0
            dest_start = int(exact_start)
            dest_end = int(exact_start + len(surface))
        else:
            match = fuzz.partial_ratio_alignment(surface, prefix)
            if match is None:
                continue
            score = float(match.score)
            dest_start = int(match.dest_start)
            dest_end = int(match.dest_end)
        length = dest_end - dest_start
        threshold = 91.0 if len(surface) < 10 else (83.0 if len(surface) < 18 else 75.0)
        if score < threshold or length < 5 or dest_start > 72:
            continue
        existing = _clock_offset_at(clock, start)
        if existing is None:
            continue
        bias = float(existing) - float(dest_start)
        if bias < 4.0 or bias > float(shape["false_advance"]) + 28.0:
            continue
        evidence.append(
            {
                "start": start,
                "end": end,
                "offset_start": dest_start,
                "offset_end": dest_end,
                "score": score,
                "length": length,
                "bias": bias,
            }
        )
        if len(evidence) >= 4:
            break
    if not evidence:
        return None

    median_bias = float(statistics.median(float(row["bias"]) for row in evidence))
    inliers = [row for row in evidence if abs(float(row["bias"]) - median_bias) <= 6.0]
    strong_single = (
        len(inliers) == 1
        and int(inliers[0]["length"]) >= 12
        and float(inliers[0]["score"]) >= 93.0
    )
    if len(inliers) < 2 and not strong_single:
        return None
    first = min(inliers, key=lambda row: float(row["start"]))
    return {
        "start": float(shape["start"]),
        "end": float(shape["end"]),
        # The plateau end is the acoustic onset of prose.  Semantic evidence
        # can arrive several seconds later, so do not move the story floor to
        # the first matched phrase or the opening sentence(s) will be skipped.
        "story_floor": float(shape["story_floor"]),
        "evidence_start": float(first["start"]),
        "score": min(99.0, max(float(row["score"]) for row in inliers)),
        "text": "<acoustic-chapter-marker>",
        "source": "acoustic+prefix-rebase",
        "clock_rebase_chars": round(median_bias, 3),
        "evidence_count": len(inliers),
        "false_advance_chars": round(float(shape["false_advance"]), 3),
    }



def _chapter_title_plateau_story_start(
    clock: list[dict[str, float | int]],
    *,
    chapter_title: str,
    first_verified_offset: int,
    first_verified_time: float,
) -> tuple[float | None, dict[str, Any] | None]:
    """Recognize a short spoken 第X幕 burst that borrowed the first prose chars."""

    normalized_title = normalize_reading_text(chapter_title)
    if not (normalized_title.startswith("第") and normalized_title.endswith("幕")):
        return None, None
    rows = [
        {"offset": float(row.get("offset") or 0.0), "time": float(row.get("time") or 0.0)}
        for row in clock[:48]
        if isinstance(row, dict)
    ]
    if len(rows) < 4 or abs(rows[0]["offset"]) > 0.5:
        return None, None
    # Character-level STT clocks can split the spoken title across 10-15 tiny
    # anchors.  Looking at only the first seven anchors misses exactly the
    # 第二幕 shape seen in the real trace, so inspect the whole short opening
    # burst instead of assuming one coarse jump.
    for index in range(1, min(32, len(rows) - 2)):
        jump = rows[index]
        borrowed = jump["offset"] - rows[0]["offset"]
        jump_seconds = jump["time"] - rows[0]["time"]
        if not (5.0 <= borrowed <= 20.0 and 0.12 <= jump_seconds <= 1.65):
            continue
        plateau_end_index = index
        while (
            plateau_end_index + 1 < len(rows)
            and abs(rows[plateau_end_index + 1]["offset"] - jump["offset"]) <= 0.05
        ):
            plateau_end_index += 1
        plateau_end = rows[plateau_end_index]
        plateau_seconds = plateau_end["time"] - jump["time"]
        if plateau_seconds < 0.55:
            continue
        next_index = plateau_end_index + 1
        if next_index >= len(rows):
            continue
        next_row = rows[next_index]
        if next_row["offset"] <= jump["offset"] + 0.05:
            continue
        # The flat tail is the pause after the spoken chapter label.  The next
        # semantic anchor can be several seconds into prose because Whisper
        # missed the first sentence, so waiting for that anchor would skip real
        # text.  Prose starts acoustically at the end of the plateau.
        story_start = plateau_end["time"]
        if first_verified_offset < borrowed + 6:
            continue
        if first_verified_time < story_start + 1.0:
            continue
        return story_start, {
            "inferred_marker_plateau": True,
            "marker_false_advance_chars": round(borrowed, 3),
            "marker_plateau_start": round(jump["time"], 3),
            "marker_plateau_end": round(plateau_end["time"], 3),
            "marker_story_start": round(story_start, 3),
        }
    return None, None


def _recover_leading_prefix_clock(
    chapter_text: str,
    segments: Iterable[dict[str, Any]],
    clock: list[dict[str, float | int]],
    *,
    chapter_title: str = "",
    chapter_index: int | None = None,
    force_verify: bool = False,
    search_start: float | None = None,
    search_end: float | None = None,
    reading_hints: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, float | int]], float | None, dict[str, Any]]:
    """Verify the opening prose of an LN chapter against nearby STT text.

    Spoken labels such as `第二幕` are treated only as local boundary locators.
    Text progression starts only after the first one-to-three prose phrases are
    semantically confirmed near LN offset zero.
    """

    normalized = normalize_reading_text(chapter_text)
    if not clock or len(normalized) < 40:
        return clock, None, {"attempted": False, "reason": "too_short"}
    segment_rows = [row for row in segments if isinstance(row, dict)]
    first_offset = int(float(clock[0].get("offset") or 0))
    first_time = float(clock[0].get("time") or 0.0)
    unsafe_early_jump: dict[str, float] | None = None
    for left, right in pairwise(clock[:32]):
        left_offset = float(left.get("offset") or 0.0)
        right_offset = float(right.get("offset") or left_offset)
        seconds = float(right.get("time") or 0.0) - float(left.get("time") or 0.0)
        characters = right_offset - left_offset
        if characters >= 96.0 and characters / max(0.02, seconds) > 24.0:
            unsafe_early_jump = {
                "left_offset": left_offset,
                "right_offset": right_offset,
                "left_time": float(left.get("time") or 0.0),
                "right_time": float(right.get("time") or 0.0),
            }
            break
    window_start = max(0.0, float(search_start) if search_start is not None else first_time - 25.0)
    window_end = max(window_start + 5.0, float(search_end) if search_end is not None else first_time + 90.0)
    marker = _find_spoken_chapter_marker(
        segment_rows,
        title=chapter_title,
        chapter_index=chapter_index,
        search_start=window_start,
        search_end=window_end,
        reference_time=first_time,
    )
    normalized_title = normalize_reading_text(chapter_title)
    title_requires_prefix_verification = (
        normalized_title.startswith("第") and normalized_title.endswith("幕")
    )
    acoustic_marker = None
    if marker is None and title_requires_prefix_verification:
        acoustic_marker = _infer_acoustic_chapter_marker(
            normalized,
            segment_rows,
            clock,
            chapter_title=chapter_title,
            search_start=window_start,
            search_end=window_end,
        )
        if acoustic_marker is not None:
            marker = acoustic_marker
    if (
        first_offset < 64
        and unsafe_early_jump is None
        and not (force_verify and (marker is not None or title_requires_prefix_verification))
    ):
        return clock, None, {"attempted": False, "reason": "prefix_already_anchored"}
    prose_floor = (
        max(
            float(marker["end"]) + 0.04,
            float(marker.get("story_floor") or 0.0),
        )
        if marker is not None
        else window_start
    )

    trigger_offset = max(
        first_offset,
        int(float((unsafe_early_jump or {}).get("right_offset") or 0.0)),
    )
    prefix = normalized[: min(len(normalized), max(360, min(1200, trigger_offset + 320)))]
    candidates: list[dict[str, float | int]] = []
    for segment in segment_rows:
        try:
            start = max(0.0, float(segment.get("start") or 0.0))
            end = max(start + 0.02, float(segment.get("end") or start + 0.02))
        except (TypeError, ValueError):
            continue
        if end < window_start:
            continue
        if start > window_end:
            break
        if marker is not None and start < prose_floor - 0.02:
            continue
        surface = normalize_reading_text(_segment_text(segment))
        if len(surface) < 6:
            continue
        exact_start = prefix.find(surface)
        if exact_start >= 0:
            score = 100.0
            dest_start = int(exact_start)
            dest_end = int(exact_start + len(surface))
        else:
            match = fuzz.partial_ratio_alignment(surface, prefix)
            if match is None:
                continue
            score = float(match.score)
            dest_start = int(match.dest_start)
            dest_end = int(match.dest_end)
        length = dest_end - dest_start
        threshold = 90.0 if len(surface) < 10 else (82.0 if len(surface) < 18 else 74.0)
        if score < threshold or length < 5:
            continue
        candidates.append(
            {
                "start": start,
                "end": end,
                "offset_start": dest_start,
                "offset_end": dest_end,
                "score": score,
                "length": length,
            }
        )

    seed_limit = 48 if marker is not None else 64
    starts = [row for row in candidates if int(row["offset_start"]) <= seed_limit]
    best_chain: list[dict[str, float | int]] = []
    for seed in starts:
        chain = [seed]
        for row in candidates:
            if float(row["start"]) < float(chain[-1]["start"]):
                continue
            if row is seed:
                continue
            previous = chain[-1]
            offset_start = int(row["offset_start"])
            previous_start = int(previous["offset_start"])
            previous_end = int(previous["offset_end"])
            if offset_start < previous_start - 6:
                continue
            if offset_start > previous_end + 120:
                continue
            if float(row["start"]) > float(previous["end"]) + 18.0:
                continue
            seed_start = float(seed["start"])
            seed_offset = int(seed["offset_start"])
            envelope_seconds = max(0.05, float(row["end"]) - seed_start)
            envelope_characters = max(0, int(row["offset_end"]) - seed_offset)
            if envelope_characters >= 24 and envelope_characters / envelope_seconds > 16.0:
                continue
            local_seconds = max(0.05, float(row["end"]) - float(previous["start"]))
            local_characters = max(0, int(row["offset_end"]) - previous_start)
            if local_characters >= 24 and local_characters / local_seconds > 20.0:
                continue
            chain.append(row)
        if not best_chain or (
            int(chain[-1]["offset_end"]) - int(chain[0]["offset_start"]),
            len(chain),
            sum(float(row["score"]) for row in chain),
        ) > (
            int(best_chain[-1]["offset_end"]) - int(best_chain[0]["offset_start"]),
            len(best_chain),
            sum(float(row["score"]) for row in best_chain),
        ):
            best_chain = chain

    marker_debug = (
        {
            "chapter_marker": True,
            "marker_start": round(float(marker["start"]), 3),
            "marker_end": round(float(marker["end"]), 3),
            "marker_score": round(float(marker["score"]), 2),
            "marker_source": str(marker.get("source") or "semantic"),
            **(
                {
                    "clock_rebase_chars": float(marker["clock_rebase_chars"]),
                    "marker_evidence_count": int(marker.get("evidence_count") or 0),
                    "marker_false_advance_chars": float(marker.get("false_advance_chars") or 0.0),
                }
                if marker.get("clock_rebase_chars") is not None
                else {}
            ),
        }
        if marker is not None
        else {"chapter_marker": False}
    )

    chain_span = (
        int(best_chain[-1]["offset_end"]) - int(best_chain[0]["offset_start"])
        if best_chain
        else 0
    )
    chain_duration = (
        float(best_chain[-1]["end"]) - float(best_chain[0]["start"])
        if best_chain
        else 0.0
    )
    chain_strong_single = bool(
        best_chain
        and len(best_chain) == 1
        and chain_span >= 16
        and float(best_chain[0].get("score") or 0.0) >= 92.0
        and chain_duration >= 0.08
    )
    needs_local_prefix = (
        not best_chain
        or (
            not chain_strong_single
            and (len(best_chain) < 2 or chain_span < 24 or chain_duration < 1.0)
        )
    )
    if needs_local_prefix:
        local_clock, local_start, local_debug = _local_fuzzy_prefix_clock(
            normalized,
            segment_rows,
            search_start=prose_floor if marker is not None else window_start,
            search_end=window_end,
            reference_time=first_time,
        )
        if local_clock and local_start is not None and local_debug is not None:
            acoustic_story_floor = (
                float(marker.get("story_floor") or prose_floor)
                if marker is not None and str(marker.get("source") or "") == "acoustic+prefix-rebase"
                else None
            )
            if (
                acoustic_story_floor is not None
                and local_clock
                and float(local_clock[0].get("offset") or 0.0) <= 0.01
                and acoustic_story_floor < float(local_clock[0].get("time") or local_start) - 0.05
            ):
                local_clock[0] = {"offset": 0, "time": round(acoustic_story_floor, 3)}
                local_start = acoustic_story_floor
                local_debug = {**local_debug, "story_start_source": "acoustic_marker_plateau"}
            bridge_source, structural_debug = _structural_leading_bias_rebase(clock)
            last = local_clock[-1]
            bridge_index = None
            for index, row in enumerate(bridge_source):
                offset = float(row.get("offset") or 0.0)
                time = float(row.get("time") or 0.0)
                if offset <= float(last["offset"]) + 1e-6:
                    continue
                if time <= float(last["time"]) + 0.02:
                    continue
                rate = (offset - float(last["offset"])) / max(
                    0.02, time - float(last["time"])
                )
                if rate <= 10.0:
                    bridge_index = index
                    break
            recovered = list(local_clock)
            if bridge_index is not None:
                for row in bridge_source[bridge_index:]:
                    if float(row.get("time") or 0.0) <= float(recovered[-1]["time"]) + 0.0005:
                        continue
                    if float(row.get("offset") or 0.0) < float(recovered[-1]["offset"]) - 1e-6:
                        continue
                    recovered.append(dict(row))
            return recovered, local_start, {
                "attempted": True,
                "recovered": True,
                "degraded": False,
                "reason": "local_fuzzy_prefix",
                "candidate_count": len(candidates),
                "chain_count": len(best_chain),
                "story_start": round(float(local_start), 3),
                "bridge_found": bridge_index is not None,
                **local_debug,
                **({"structural_bridge": structural_debug} if structural_debug else {}),
                **marker_debug,
            }

        reading_clock, reading_start, reading_debug = _local_reading_hint_clock(
            reading_hints or [],
            segment_rows,
            search_start=prose_floor if marker is not None else window_start,
            search_end=window_end,
            reference_time=first_time,
        )
        if reading_clock and reading_start is not None and reading_debug is not None:
            acoustic_story_floor = (
                float(marker.get("story_floor") or prose_floor)
                if marker is not None and str(marker.get("source") or "") == "acoustic+prefix-rebase"
                else None
            )
            if (
                acoustic_story_floor is not None
                and reading_clock
                and float(reading_clock[0].get("offset") or 0.0) <= 0.01
                and acoustic_story_floor < float(reading_clock[0].get("time") or reading_start) - 0.05
            ):
                reading_clock[0] = {"offset": 0, "time": round(acoustic_story_floor, 3)}
                reading_start = acoustic_story_floor
                reading_debug = {**reading_debug, "story_start_source": "acoustic_marker_plateau"}
            bridge_source, structural_debug = _structural_leading_bias_rebase(clock)
            last = reading_clock[-1]
            bridge_index = None
            for index, row in enumerate(bridge_source):
                offset = float(row.get("offset") or 0.0)
                time = float(row.get("time") or 0.0)
                if offset <= float(last["offset"]) + 1e-6:
                    continue
                if time <= float(last["time"]) + 0.02:
                    continue
                rate = (offset - float(last["offset"])) / max(0.02, time - float(last["time"]))
                if rate <= 10.0:
                    bridge_index = index
                    break
            recovered = list(reading_clock)
            if bridge_index is not None:
                for row in bridge_source[bridge_index:]:
                    if float(row.get("time") or 0.0) <= float(recovered[-1]["time"]) + 0.0005:
                        continue
                    if float(row.get("offset") or 0.0) < float(recovered[-1]["offset"]) - 1e-6:
                        continue
                    recovered.append(dict(row))
            return recovered, reading_start, {
                "attempted": True,
                "recovered": True,
                "degraded": False,
                "reason": "phonetic_reading_prefix",
                "candidate_count": len(candidates),
                "chain_count": len(best_chain),
                "story_start": round(float(reading_start), 3),
                "bridge_found": bridge_index is not None,
                **reading_debug,
                **({"structural_bridge": structural_debug} if structural_debug else {}),
                **marker_debug,
            }

        structural_clock, structural_debug = _structural_leading_bias_rebase(clock)
        if structural_debug is not None:
            structural_start = float(
                (structural_debug.get("hazard") or {}).get("left_time") or first_time
            )
            return structural_clock, structural_start, {
                "attempted": True,
                "recovered": True,
                "degraded": True,
                "reason": "structural_bias_rebase",
                "candidate_count": len(candidates),
                "chain_count": len(best_chain),
                "story_start": round(structural_start, 3),
                "bridge_found": True,
                "fallback": structural_debug,
                "clock_rebase_chars": float(structural_debug["clock_rebase_chars"]),
                **marker_debug,
            }

    if not best_chain:
        marker_clock, marker_story_floor = (
            _marker_hold_fallback(clock, segment_rows, marker, search_end=window_end)
            if marker is not None
            else (clock, None)
        )
        return marker_clock, marker_story_floor, {
            "attempted": True,
            "recovered": marker_story_floor is not None and marker_clock is not clock,
            "degraded": marker_story_floor is not None and marker_clock is not clock,
            "reason": "marker_hold_fallback" if marker_story_floor is not None and marker_clock is not clock else "no_prefix_seed",
            "candidate_count": len(candidates),
            "first_offset": first_offset,
            **marker_debug,
        }

    span = int(best_chain[-1]["offset_end"]) - int(best_chain[0]["offset_start"])
    duration = float(best_chain[-1]["end"]) - float(best_chain[0]["start"])
    strong_single = (
        len(best_chain) == 1
        and span >= 16
        and float(best_chain[0].get("score") or 0.0) >= 92.0
        and duration >= 0.08
    )
    if not strong_single and (len(best_chain) < 2 or span < 24 or duration < 1.0):
        marker_clock, marker_story_floor = (
            _marker_hold_fallback(clock, segment_rows, marker, search_end=window_end)
            if marker is not None
            else (clock, None)
        )
        return marker_clock, marker_story_floor, {
            "attempted": True,
            "recovered": marker_story_floor is not None and marker_clock is not clock,
            "degraded": marker_story_floor is not None and marker_clock is not clock,
            "reason": "marker_hold_fallback" if marker_story_floor is not None and marker_clock is not clock else "prefix_evidence_too_sparse",
            "candidate_count": len(candidates),
            "chain_count": len(best_chain),
            "span": span,
            **marker_debug,
        }

    first = best_chain[0]
    first_verified_offset = int(first["offset_start"])
    strong_boundaries = [
        int(row["offset"])
        for row in _punctuation_boundaries(chapter_text)
        if int(row.get("strength") or 0) >= 3
    ]
    prose_prefix_boundary = any(
        4 <= boundary <= first_verified_offset + 1 for boundary in strong_boundaries
    )
    plateau_story_start, plateau_debug = _chapter_title_plateau_story_start(
        clock,
        chapter_title=chapter_title,
        first_verified_offset=first_verified_offset,
        first_verified_time=float(first["start"]),
    )

    # A single long semantic segment can still prove that an EPUB-only preface
    # was genuinely not narrated (the v163 regression).  Keep that narrow
    # escape hatch, but never use it for the multi-segment real chapter-start
    # chains that produced the v175 0->10/11 runaway.  Those must reconstruct
    # their missing prose prefix or use the spoken-title plateau below.
    if (
        marker is None
        and plateau_story_start is None
        and strong_single
        and 18 <= first_verified_offset <= 64
    ):
        verified_start = float(first["start"])
        return clock, verified_start, {
            "attempted": True,
            "recovered": True,
            "candidate_count": len(candidates),
            "chain_count": len(best_chain),
            "span": span,
            "story_start": round(verified_start, 3),
            "verified_through_offset": int(first["offset_end"]),
            "bridge_found": True,
            "original_first_offset": first_offset,
            "original_first_time": round(first_time, 3),
            "skipped_ln_prefix": True,
            "skipped_prefix_reason": "single_strong_nonspoken_prefix",
            **marker_debug,
        }

    # Since image placeholders are zero-width in the audiobook coordinate
    # system, a non-zero unmatched prefix from a multi-segment opening chain is
    # prose. Rebuild its timing instead of preserving the old clock.
    verified_rate = span / max(0.25, duration)
    local_rate = max(2.0, min(10.0, verified_rate))
    recovered_start = max(
        prose_floor if marker is not None else 0.0,
        float(first["start"]) - int(first["offset_start"]) / local_rate,
    )
    if marker is not None and str(marker.get("source") or "") == "acoustic+prefix-rebase":
        recovered_start = float(marker.get("story_floor") or prose_floor)
    elif plateau_story_start is not None:
        recovered_start = float(plateau_story_start)

    recovered: list[dict[str, float | int]] = [
        {"offset": 0, "time": round(recovered_start, 3)}
    ]
    for row in best_chain:
        recovered.extend(
            [
                {"offset": int(row["offset_start"]), "time": round(float(row["start"]), 3)},
                {"offset": int(row["offset_end"]), "time": round(float(row["end"]), 3)},
            ]
        )

    cleaned: list[dict[str, float | int]] = []
    for row in sorted(recovered, key=lambda item: (float(item["time"]), float(item["offset"]))):
        if cleaned and float(row["time"]) <= float(cleaned[-1]["time"]) + 0.0005:
            if float(row["offset"]) <= float(cleaned[-1]["offset"]) + 1e-6:
                continue
            continue
        if cleaned and float(row["offset"]) < float(cleaned[-1]["offset"]) - 1e-6:
            continue
        cleaned.append(row)
    recovered = cleaned
    last = recovered[-1]

    bridge_clock = clock
    rebase_chars = float(marker.get("clock_rebase_chars") or 0.0) if marker is not None else 0.0
    if rebase_chars > 0.0:
        bridge_clock = []
        for row in clock:
            time = float(row.get("time") or 0.0)
            offset = float(row.get("offset") or 0.0)
            if time >= prose_floor - 0.02:
                offset = max(0.0, offset - rebase_chars)
            bridge_clock.append({"offset": offset, "time": time})

    bridge_index = None
    max_bridge_rate = max(6.5, min(10.0, verified_rate * 1.8))
    for index, row in enumerate(bridge_clock):
        offset = float(row.get("offset") or 0.0)
        time = float(row.get("time") or 0.0)
        if offset <= float(last["offset"]) + 1e-6 or time <= float(last["time"]) + 0.02:
            continue
        rate = (offset - float(last["offset"])) / max(0.02, time - float(last["time"]))
        if rate <= max_bridge_rate:
            bridge_index = index
            break
    if bridge_index is not None:
        for row in bridge_clock[bridge_index:]:
            if float(row.get("time") or 0.0) <= float(recovered[-1]["time"]) + 0.0005:
                continue
            if float(row.get("offset") or 0.0) < float(recovered[-1]["offset"]) - 1e-6:
                continue
            recovered.append(dict(row))

    return recovered, recovered_start, {
        "attempted": True,
        "recovered": True,
        "candidate_count": len(candidates),
        "chain_count": len(best_chain),
        "span": span,
        "story_start": round(recovered_start, 3),
        "verified_through_offset": int(last["offset"]),
        "bridge_found": bridge_index is not None,
        "original_first_offset": first_offset,
        "original_first_time": round(first_time, 3),
        "unsafe_early_jump": unsafe_early_jump,
        "verified_rate": round(verified_rate, 3),
        "max_bridge_rate": round(max_bridge_rate, 3),
        "reconstructed_prose_prefix": bool(
            marker is None and first_verified_offset > 0
        ),
        "prose_prefix_has_strong_boundary": bool(prose_prefix_boundary),
        **(plateau_debug or {}),
        **marker_debug,
    }

def _safe_degraded_leading_bridge(
    anchors: list[dict[str, Any]],
    *,
    chapter_length: int,
    chapter_start: float,
    chapter_end: float,
    max_rate: float = 10.0,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Salvage an unsafe first-chapter clock without retrying forever.

    When prefix verification fails, a deterministic retry used to hit the same
    hard error forever.  Keep offset zero at the story onset, then rejoin the
    dense clock only at the first anchor reachable at a generous reading rate.
    If no such anchor exists, fall back to one coarse linear chapter bridge.
    """

    rows: list[dict[str, Any]] = []
    for row in anchors:
        if isinstance(row, dict):
            rows.append(dict(row))
    if len(rows) < 2:
        return rows, None
    hazard = _unsafe_leading_anchor_jump(rows)
    if hazard is None:
        return rows, None

    start_time = max(0.0, float(chapter_start))
    start_offset = max(0.0, float(hazard.get("left_offset") or 0.0))
    if start_offset > 64.0:
        return rows, None
    start_anchor = {"offset": 0, "time": round(start_time, 3)}
    limit = max(6.0, min(12.0, float(max_rate)))

    bridge_index = None
    for index, row in enumerate(rows):
        offset = float(row.get("offset") or 0.0)
        time_value = float(row.get("time") or 0.0)
        if offset < 96.0 or time_value <= start_time + 0.25:
            continue
        rate = offset / max(0.02, time_value - start_time)
        if rate <= limit:
            bridge_index = index
            break

    if bridge_index is not None:
        salvaged = [start_anchor]
        for row in rows[bridge_index:]:
            if float(row.get("time") or 0.0) <= start_time + 0.0005:
                continue
            if float(row.get("offset") or 0.0) <= 0.0:
                continue
            salvaged.append(dict(row))
        return salvaged, {
            "mode": "safe_rejoin",
            "hazard": hazard,
            "rejoin_offset": float(salvaged[1]["offset"]),
            "rejoin_time": float(salvaged[1]["time"]),
            "rejoin_rate": round(
                float(salvaged[1]["offset"])
                / max(0.02, float(salvaged[1]["time"]) - start_time),
                3,
            ),
            "max_rate": round(limit, 3),
        }

    end_time = max(start_time + 1.0, float(chapter_end))
    length = max(1, int(chapter_length))
    return [
        start_anchor,
        {"offset": length, "time": round(end_time, 3)},
    ], {
        "mode": "linear_chapter",
        "hazard": hazard,
        "rejoin_offset": float(length),
        "rejoin_time": round(end_time, 3),
        "rejoin_rate": round(length / max(0.02, end_time - start_time), 3),
        "max_rate": round(limit, 3),
    }


def _prune_unreachable_leading_anchors(
    anchors: list[dict[str, Any]],
    *,
    max_rate: float = 10.0,
    verified_through_offset: float = 0.0,
) -> list[dict[str, Any]]:
    """Drop an impossible leading bridge instead of interpolating through it.

    This is a structural and runtime safety net.  It only governs the leading
    corridor; after the first reachable substantial anchor the dense clock is
    preserved unchanged.
    """

    rows: list[dict[str, Any]] = []
    for row in anchors:
        if isinstance(row, dict):
            rows.append(dict(row))
    if len(rows) < 2:
        return rows
    output = [rows[0]]
    corridor_end = max(96.0, float(verified_through_offset) + 64.0)
    bridged = float(output[0].get("offset") or 0.0) >= corridor_end
    for row in rows[1:]:
        previous = output[-1]
        previous_offset = float(previous.get("offset") or 0.0)
        offset = float(row.get("offset") or previous_offset)
        previous_time = float(previous.get("time") or 0.0)
        time_value = float(row.get("time") or previous_time)
        characters = offset - previous_offset
        seconds = time_value - previous_time
        if characters < -1e-6 or seconds < -1e-6:
            continue
        if not bridged and characters >= 24.0:
            rate = characters / max(0.02, seconds)
            if rate > max(1.0, float(max_rate)):
                continue
        output.append(row)
        if offset >= corridor_end:
            bridged = True
    return output


def _prune_rejoining_local_rate_outliers(
    anchors: list[dict[str, Any]],
    *,
    max_rate: float,
    preferred_rate: float | None = None,
    max_offset_exclusive: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop transient dense-clock anchors that exceed a verified local rate.

    The leading-prefix verifier already derives a chapter-specific maximum
    bridge rate from real matched speech.  Dense paired clocks can still carry
    an intermediate edge that races above that envelope and then rejoins it a
    fraction of a second later.  Only prune such *rejoining* outliers: if no
    later anchor is reachable from the last accepted point, preserve the tail
    unchanged rather than inventing a new clock.
    """

    rows: list[dict[str, Any]] = []
    for row in anchors:
        if isinstance(row, dict):
            rows.append(dict(row))
    if len(rows) < 3:
        return rows, {"dropped": 0, "max_rate": round(float(max_rate), 3)}

    # This guard exists for the verified leading-prefix clock only. Applying
    # the same bootstrap envelope to the whole chapter can delete legitimate
    # later dense-word anchors.
    tail: list[dict[str, Any]] = []
    if max_offset_exclusive is not None:
        cutoff = max(0.0, float(max_offset_exclusive))
        split_index = next(
            (
                index
                for index, row in enumerate(rows)
                if float(row.get("offset") or 0.0) >= cutoff - 1e-6
            ),
            len(rows),
        )
        tail = rows[split_index:]
        rows = rows[:split_index]
        if len(rows) < 3:
            return [*rows, *tail], {
                "dropped": 0,
                "max_rate": round(float(max_rate), 3),
                "max_offset_exclusive": round(cutoff, 3),
            }

    limit = max(1.0, float(max_rate))
    preferred_limit = None
    if preferred_rate is not None and float(preferred_rate) > 0.0:
        # max_rate is a hard safety ceiling.  When the chapter verifier gives
        # us a real local speaking rate, prefer a rejoin close to that rate
        # instead of the first merely-legal point under the broad ceiling.
        preferred_limit = min(limit, max(1.0, float(preferred_rate) * 1.20))
    output = [rows[0]]
    dropped: list[dict[str, float]] = []
    index = 1
    while index < len(rows):
        previous = output[-1]
        previous_offset = float(previous.get("offset") or 0.0)
        previous_time = float(previous.get("time") or 0.0)
        row = rows[index]
        offset = float(row.get("offset") or previous_offset)
        time_value = float(row.get("time") or previous_time)
        characters = offset - previous_offset
        seconds = time_value - previous_time

        if characters < -1e-6 or seconds < -1e-6:
            # The helper is only allowed to prune a transient fast-forward
            # spike. Preserve any non-monotonic tail for the existing alignment
            # safeguards instead of silently deleting unrelated anchors.
            output.extend(rows[index:])
            break
        rate = characters / max(0.02, seconds)
        if characters <= 0.0 or rate <= limit + 1e-9:
            output.append(row)
            index += 1
            continue

        # Do not delete an unsupported tail.  A bad intermediate anchor is safe
        # to remove only when the existing clock itself supplies a later point
        # that rejoins the verified envelope from the last accepted anchor.
        rejoin_index = None
        fallback_rejoin_index = None
        for candidate_index in range(index + 1, len(rows)):
            candidate = rows[candidate_index]
            candidate_offset = float(candidate.get("offset") or previous_offset)
            candidate_time = float(candidate.get("time") or previous_time)
            candidate_chars = candidate_offset - previous_offset
            candidate_seconds = candidate_time - previous_time
            if candidate_chars < -1e-6 or candidate_seconds <= 0.0:
                continue
            candidate_rate = candidate_chars / max(0.02, candidate_seconds)
            if candidate_rate <= limit + 1e-9 and fallback_rejoin_index is None:
                fallback_rejoin_index = candidate_index
            if preferred_limit is not None and candidate_rate <= preferred_limit + 1e-9:
                rejoin_index = candidate_index
                break
            if preferred_limit is None and fallback_rejoin_index is not None:
                rejoin_index = fallback_rejoin_index
                break

        if rejoin_index is None:
            rejoin_index = fallback_rejoin_index
        if rejoin_index is None:
            output.extend(rows[index:])
            break

        for skipped in rows[index:rejoin_index]:
            skipped_offset = float(skipped.get("offset") or previous_offset)
            skipped_time = float(skipped.get("time") or previous_time)
            dropped.append({
                "offset": round(skipped_offset, 4),
                "time": round(skipped_time, 3),
                "rate": round(
                    (skipped_offset - previous_offset)
                    / max(0.02, skipped_time - previous_time),
                    3,
                ),
            })
        # The rejoin intentionally borrows wall-clock silence to slow a
        # transient fast prefix edge.  Mark that segment so the runtime
        # activity clock does not immediately compress the borrowed pause away
        # and recreate the exact same visual runaway.
        rows[rejoin_index]["wall_clock_from_previous"] = True
        index = rejoin_index

    debug = {
        "dropped": len(dropped),
        "max_rate": round(limit, 3),
        "anchors": dropped,
    }
    if preferred_limit is not None:
        debug["preferred_rate"] = round(float(preferred_rate), 3)
        debug["preferred_max_rate"] = round(preferred_limit, 3)
    if max_offset_exclusive is not None:
        debug["max_offset_exclusive"] = round(float(max_offset_exclusive), 3)
    return [*output, *tail], debug


def _unsafe_leading_anchor_jump(
    anchors: list[dict[str, Any]],
) -> dict[str, float] | None:
    for left, right in pairwise(anchors):
        left_offset = float(left.get("offset") or 0.0)
        if left_offset > 64.0:
            break
        right_offset = float(right.get("offset") or left_offset)
        characters = right_offset - left_offset
        seconds = float(right.get("time") or 0.0) - float(left.get("time") or 0.0)
        if characters >= 64.0 and characters / max(0.02, seconds) > 24.0:
            return {
                "left_offset": left_offset,
                "right_offset": right_offset,
                "left_time": float(left.get("time") or 0.0),
                "right_time": float(right.get("time") or 0.0),
                "characters_per_second": characters / max(0.02, seconds),
            }
    return None


def _transcript_clock(segments: Iterable[dict[str, Any]]) -> tuple[str, list[float]]:
    text_parts: list[str] = []
    times: list[float] = []
    last_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        words = segment.get("words")
        rows = words if isinstance(words, list) and words else [segment]
        for row in rows:
            if not isinstance(row, dict):
                continue
            surface = str(row.get("word") or row.get("text") or "")
            normalized = normalize_reading_text(surface)
            if not normalized:
                continue
            try:
                start = max(last_end, float(row.get("start") or segment.get("start") or last_end))
                end = max(start + 0.02, float(row.get("end") or segment.get("end") or start + 0.02))
            except (TypeError, ValueError):
                continue
            for index, character in enumerate(normalized):
                text_parts.append(character)
                times.append(start + (end - start) * index / max(1, len(normalized)))
            last_end = end
    times.append(last_end)
    return "".join(text_parts), times


def _structural_leading_bias_rebase(
    anchors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Remove a clearly impossible constant prefix bias from a dense clock.

    The global matcher can lock the first real prose onto a later LN occurrence,
    yielding a shape such as 0@35.84 -> 411@36.24 -> 412@36.52.  Dropping those
    anchors and later interpolating 0->759 merely hides the same error.  When an
    instantaneous leading jump is physically impossible, treat its size as a
    candidate constant bias and preserve the dense timing after subtracting it.
    """

    rows = [dict(row) for row in anchors if isinstance(row, dict)]
    if len(rows) < 3:
        return rows, None
    hazard = _unsafe_leading_anchor_jump(rows)
    if hazard is None:
        return rows, None
    left_offset = float(hazard["left_offset"])
    right_offset = float(hazard["right_offset"])
    bias = right_offset - left_offset
    if left_offset > 16.0 or bias < 96.0:
        return rows, None
    cut_time = float(hazard["right_time"])

    rebased: list[dict[str, Any]] = []
    for row in rows:
        time_value = float(row.get("time") or 0.0)
        offset = float(row.get("offset") or 0.0)
        if time_value >= cut_time - 0.0005:
            offset = max(left_offset, offset - bias)
        if rebased and offset < float(rebased[-1]["offset"]) - 1e-6:
            offset = float(rebased[-1]["offset"])
        item = {"offset": offset, "time": time_value}
        if (
            rebased
            and abs(time_value - float(rebased[-1]["time"])) < 0.0005
            and abs(offset - float(rebased[-1]["offset"])) < 1e-6
        ):
            continue
        rebased.append(item)

    if _unsafe_leading_anchor_jump(rebased) is not None:
        return rows, None
    progressed = [
        row for row in rebased
        if float(row.get("time") or 0.0) > cut_time + 0.1
        and float(row.get("offset") or 0.0) >= left_offset + 24.0
    ]
    if not progressed:
        return rows, None

    rates: list[float] = []
    for left, right in pairwise(rebased):
        chars = float(right.get("offset") or 0.0) - float(left.get("offset") or 0.0)
        secs = float(right.get("time") or 0.0) - float(left.get("time") or 0.0)
        if chars >= 2.0 and secs >= 0.08 and float(left.get("offset") or 0.0) < 900.0:
            rates.append(chars / secs)
    median_rate = float(statistics.median(rates)) if rates else 0.0
    if median_rate > 14.0:
        return rows, None
    return rebased, {
        "mode": "structural_bias_rebase",
        "hazard": hazard,
        "clock_rebase_chars": round(bias, 3),
        "median_dense_rate": round(median_rate, 3),
    }


def _local_fuzzy_prefix_clock(
    chapter_text: str,
    segments: Iterable[dict[str, Any]],
    *,
    search_start: float,
    search_end: float,
    reference_time: float,
) -> tuple[list[dict[str, float | int]], float | None, dict[str, Any] | None]:
    """Align the opening LN prose against a local STT window as one sequence.

    Per-segment fuzzy matching is brittle with Whisper tiny: a phrase can be
    split into several short segments or one early segment can hallucinate a
    later sentence.  A local edit path lets insertions/substitutions be ignored
    while the following real prose still anchors LN offset zero.
    """

    normalized = normalize_reading_text(chapter_text)
    if len(normalized) < 40:
        return [], None, None
    floor = max(float(search_start), float(reference_time) - 1.5)
    ceiling = min(float(search_end), float(reference_time) + 120.0)
    selected: list[dict[str, Any]] = []
    for row in segments:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or start)
        except (TypeError, ValueError):
            continue
        if end < floor:
            continue
        if start > ceiling:
            break
        selected.append(row)
    if not selected:
        return [], None, None

    transcript, transcript_times = _transcript_clock(selected)
    if len(transcript) < 24:
        return [], None, None
    transcript = transcript[:1600]
    transcript_times = transcript_times[: len(transcript) + 1]
    novel_limit = min(
        len(normalized),
        max(320, min(1800, int(len(transcript) * 1.6) + 120)),
    )
    prefix = normalized[:novel_limit]

    equal_runs: list[tuple[int, int, int]] = []
    matched = 0
    for tag, src_start, src_end, dest_start, dest_end in Levenshtein.opcodes(
        prefix, transcript
    ).as_list():
        if tag != "equal":
            continue
        length = min(src_end - src_start, dest_end - dest_start)
        if length < 2:
            continue
        matched += length
        equal_runs.append((int(src_start), int(dest_start), int(length)))
    if not equal_runs:
        return [], None, None
    earliest = min(row[0] for row in equal_runs)
    coverage = matched / max(1, min(len(prefix), len(transcript)))
    if earliest > 96 or matched < 24 or coverage < 0.10:
        return [], None, None

    candidates: list[dict[str, float | int]] = []
    for novel_start, transcript_start, length in equal_runs:
        for delta in range(length + 1):
            transcript_index = transcript_start + delta
            if transcript_index >= len(transcript_times):
                break
            candidates.append(
                {
                    "offset": novel_start + delta,
                    "time": round(float(transcript_times[transcript_index]), 3),
                }
            )
    candidates.sort(key=lambda row: (float(row["time"]), float(row["offset"])))
    dense: list[dict[str, float | int]] = []
    for row in candidates:
        if dense and float(row["time"]) <= float(dense[-1]["time"]) + 0.0005:
            if float(row["offset"]) <= float(dense[-1]["offset"]) + 1e-6:
                continue
            continue
        if dense and float(row["offset"]) < float(dense[-1]["offset"]) - 1e-6:
            continue
        dense.append(row)
    if len(dense) < 8:
        return [], None, None

    first = dense[0]
    first_offset = float(first["offset"])
    rates: list[float] = []
    for left, right in pairwise(dense[:160]):
        chars = float(right["offset"]) - float(left["offset"])
        secs = float(right["time"]) - float(left["time"])
        if chars >= 1.0 and secs >= 0.05:
            rates.append(chars / secs)
    local_rate = max(2.0, min(10.0, float(statistics.median(rates)) if rates else 5.0))
    story_start = max(
        float(reference_time),
        float(first["time"]) - first_offset / local_rate,
    )
    output: list[dict[str, float | int]] = [
        {"offset": 0, "time": round(story_start, 3)}
    ]
    for row in dense:
        if float(row["time"]) <= story_start + 0.0005:
            continue
        if float(row["offset"]) < float(output[-1]["offset"]) - 1e-6:
            continue
        output.append(dict(row))
    if len(output) < 8 or float(output[-1]["offset"]) < 24.0:
        return [], None, None
    return output, story_start, {
        "mode": "local_fuzzy_prefix",
        "matched_characters": int(matched),
        "coverage": round(float(coverage), 4),
        "first_matched_offset": int(earliest),
        "verified_through_offset": int(float(output[-1]["offset"])),
        "local_rate": round(local_rate, 3),
        "segment_count": len(selected),
    }



def _hiragana_fold(value: str) -> str:
    normalized = normalize_reading_text(value)
    out: list[str] = []
    for character in normalized:
        if "ァ" <= character <= "ヶ":
            character = chr(ord(character) - 0x60)
        out.append(character)
    return "".join(out)


def _local_reading_hint_clock(
    reading_hints: Iterable[dict[str, Any]],
    segments: Iterable[dict[str, Any]],
    *,
    search_start: float,
    search_end: float,
    reference_time: float,
) -> tuple[list[dict[str, float | int]], float | None, dict[str, Any] | None]:
    """Align chapter-start Jiten readings against a local STT word window."""

    hints = [
        dict(row) for row in reading_hints
        if isinstance(row, dict)
        and int(row.get("offset_start") or 0) < 160
        and int(row.get("offset_end") or 0) > int(row.get("offset_start") or 0)
        and len(str(row.get("reading") or "")) >= 1
    ]
    if not hints:
        return [], None, None
    hints.sort(key=lambda row: (int(row.get("offset_start") or 0), int(row.get("offset_end") or 0)))

    speech_rows: list[dict[str, Any]] = []
    floor = max(float(search_start), float(reference_time) - 2.0)
    ceiling = min(float(search_end), float(reference_time) + 120.0)
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            segment_start = float(segment.get("start") or 0.0)
            segment_end = float(segment.get("end") or segment_start)
        except (TypeError, ValueError):
            continue
        if segment_end < floor:
            continue
        if segment_start > ceiling:
            break
        words = segment.get("words")
        rows = words if isinstance(words, list) and words else [segment]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                start = float(row.get("start") or segment_start)
                end = float(row.get("end") or segment_end)
            except (TypeError, ValueError):
                continue
            if end < floor or start > ceiling:
                continue
            text = _hiragana_fold(str(row.get("word") or row.get("text") or ""))
            if not text:
                continue
            speech_rows.append({"start": start, "end": max(start + 0.02, end), "text": text})
    if not speech_rows:
        return [], None, None

    phrase_hints: list[dict[str, Any]] = []
    for index, row in enumerate(hints[:24]):
        reading = _hiragana_fold(str(row.get("reading") or ""))
        surface = normalize_reading_text(str(row.get("surface") or ""))
        if len(reading) >= 4:
            phrase_hints.append({**row, "reading_phrase": reading, "surface_phrase": surface})
        combined_reading = reading
        combined_surface = surface
        end_offset = int(row.get("offset_end") or 0)
        for extra in range(1, 3):
            if index + extra >= len(hints):
                break
            next_row = hints[index + extra]
            next_start = int(next_row.get("offset_start") or 0)
            if next_start > end_offset + 6:
                break
            combined_reading += _hiragana_fold(str(next_row.get("reading") or ""))
            combined_surface += normalize_reading_text(str(next_row.get("surface") or ""))
            end_offset = int(next_row.get("offset_end") or end_offset)
            if len(combined_reading) >= 7:
                phrase_hints.append({
                    "offset_start": int(row.get("offset_start") or 0),
                    "offset_end": end_offset,
                    "reading_phrase": combined_reading,
                    "surface_phrase": combined_surface,
                })
    if not phrase_hints:
        return [], None, None

    candidates: list[dict[str, Any]] = []
    for phrase in phrase_hints:
        reading = str(phrase.get("reading_phrase") or "")
        surface = str(phrase.get("surface_phrase") or "")
        if len(reading) < 4:
            continue
        best: dict[str, Any] | None = None
        for index in range(len(speech_rows)):
            combined = ""
            window_start = float(speech_rows[index]["start"])
            for width in range(1, 7):
                if index + width > len(speech_rows):
                    break
                row = speech_rows[index + width - 1]
                window_end = float(row["end"])
                if window_end - window_start > 9.0:
                    break
                combined += str(row["text"])
                if len(combined) < 3:
                    continue
                reading_score = 100.0 if reading in combined else float(fuzz.ratio(reading, combined))
                surface_score = 100.0 if surface and surface in combined else (
                    float(fuzz.ratio(surface, combined)) if surface else 0.0
                )
                score = max(reading_score, surface_score)
                threshold = 92.0 if len(reading) < 6 else (84.0 if len(reading) < 10 else 78.0)
                if score < threshold:
                    continue
                candidate = {
                    "offset_start": int(phrase.get("offset_start") or 0),
                    "offset_end": int(phrase.get("offset_end") or 0),
                    "start": window_start,
                    "end": window_end,
                    "score": score,
                    "reading_length": len(reading),
                }
                if best is None or (
                    float(candidate["score"]),
                    -(float(candidate["end"]) - float(candidate["start"])),
                    float(candidate["start"]),
                ) > (
                    float(best["score"]),
                    -(float(best["end"]) - float(best["start"])),
                    float(best["start"]),
                ):
                    best = candidate
        if best is not None:
            candidates.append(best)

    candidates.sort(key=lambda row: (int(row["offset_start"]), float(row["start"]), -float(row["score"])))
    chain: list[dict[str, Any]] = []
    for row in candidates:
        if int(row["offset_start"]) > 48 and not chain:
            continue
        if chain:
            previous = chain[-1]
            if float(row["start"]) < float(previous["start"]) - 0.15:
                continue
            if int(row["offset_start"]) > int(previous["offset_end"]) + 28:
                continue
        if chain and int(row["offset_start"]) == int(chain[-1]["offset_start"]):
            previous = chain[-1]
            if (int(row["offset_end"]), float(row["score"])) > (int(previous["offset_end"]), float(previous["score"])):
                chain[-1] = row
            continue
        chain.append(row)

    if not chain:
        return [], None, None
    first = chain[0]
    covered = max(int(row["offset_end"]) for row in chain) - int(first["offset_start"])
    strong_single = (
        len(chain) == 1
        and int(first["offset_start"]) <= 8
        and int(first["offset_end"]) - int(first["offset_start"]) >= 3
        and int(first["reading_length"]) >= 6
        and float(first["score"]) >= 98.0
    )
    if not strong_single and (len(chain) < 2 or covered < 10):
        return [], None, None

    first_offset = int(first["offset_start"])
    story_start = max(float(reference_time), float(first["start"]) - first_offset / 5.0)
    output: list[dict[str, float | int]] = [{"offset": 0, "time": round(story_start, 3)}]
    for row in chain:
        start_offset = int(row["offset_start"])
        end_offset = int(row["offset_end"])
        start_time = max(story_start, float(row["start"]))
        end_time = max(start_time + 0.02, float(row["end"]))
        for offset, time_value in ((start_offset, start_time), (end_offset, end_time)):
            if offset < float(output[-1]["offset"]) - 1e-6:
                continue
            if time_value <= float(output[-1]["time"]) + 0.0005:
                continue
            output.append({"offset": offset, "time": round(time_value, 3)})
    if strong_single:
        if len(output) < 2 or float(output[-1]["offset"]) < 3.0:
            return [], None, None
    elif len(output) < 3 or float(output[-1]["offset"]) < 8.0:
        return [], None, None
    return output, story_start, {
        "mode": "phonetic_reading_prefix",
        "hint_count": len(hints),
        "candidate_count": len(candidates),
        "chain_count": len(chain),
        "verified_through_offset": int(float(output[-1]["offset"])),
        "first_hint_offset": first_offset,
        "first_hint_score": round(float(first["score"]), 2),
        "evidence": [
            {
                "offset_start": int(row["offset_start"]),
                "offset_end": int(row["offset_end"]),
                "start": round(float(row["start"]), 3),
                "end": round(float(row["end"]), 3),
                "score": round(float(row["score"]), 2),
            }
            for row in chain[:4]
        ],
    }


def _precision_reading_word_clock(
    reading_hints: Iterable[dict[str, Any]],
    segments: Iterable[dict[str, Any]],
    *,
    search_start: float,
    search_end: float,
    coarse_anchors: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Match reader/Jiten hints directly to cached precision-STT word rows.

    Unlike the broader phrase matcher, this keeps individual word boundaries
    such as ``終わり`` end -> pause -> ``しばらく`` start.  Short one-kana
    particles are intentionally skipped because they are too ambiguous.
    """

    speech_rows: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            segment_start = float(segment.get("start") or 0.0)
            segment_end = float(segment.get("end") or segment_start)
        except (TypeError, ValueError):
            continue
        if segment_end < float(search_start):
            continue
        if segment_start > float(search_end):
            break
        words = segment.get("words")
        rows = words if isinstance(words, list) and words else [segment]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                start = float(row.get("start") or segment_start)
                end = float(row.get("end") or segment_end)
            except (TypeError, ValueError):
                continue
            if end < float(search_start) or start > float(search_end):
                continue
            raw = str(row.get("word") or row.get("text") or "")
            folded = _hiragana_fold(raw)
            surface = normalize_reading_text(raw)
            if not folded and not surface:
                continue
            speech_rows.append(
                {
                    "start": start,
                    "end": max(start + 0.02, end),
                    "folded": folded,
                    "surface": surface,
                }
            )
    if not speech_rows:
        return [], None

    hints = [
        dict(row)
        for row in reading_hints
        if isinstance(row, dict)
        and int(row.get("offset_end") or 0) > int(row.get("offset_start") or 0)
        and int(row.get("offset_start") or 0) < 176
    ]
    hints.sort(key=lambda row: (int(row.get("offset_start") or 0), int(row.get("offset_end") or 0)))
    if not hints:
        return [], None

    coarse_rows = []
    for row in coarse_anchors or []:
        if not isinstance(row, dict):
            continue
        try:
            coarse_rows.append((float(row.get("offset") or 0.0), float(row.get("time") or 0.0)))
        except (TypeError, ValueError):
            continue
    coarse_rows.sort()

    def expected_time(offset: int) -> float | None:
        if len(coarse_rows) < 2:
            return None
        for index in range(1, len(coarse_rows)):
            left_offset, left_time = coarse_rows[index - 1]
            right_offset, right_time = coarse_rows[index]
            if right_offset < left_offset + 1e-6:
                continue
            if left_offset - 1e-6 <= offset <= right_offset + 1e-6:
                ratio = (float(offset) - left_offset) / (right_offset - left_offset)
                return left_time + ratio * (right_time - left_time)
        return None

    matches: list[dict[str, Any]] = []
    minimum_word_index = 0
    for hint in hints:
        offset_start = int(hint.get("offset_start") or 0)
        offset_end = int(hint.get("offset_end") or 0)
        reading = _hiragana_fold(str(hint.get("reading") or ""))
        surface = normalize_reading_text(str(hint.get("surface") or ""))
        # Single kana such as は/の appear everywhere and are not safe anchors.
        if max(len(reading), len(surface)) < 2:
            continue

        best: tuple[float, int, int, float, float] | None = None
        # Allow a small overlap with the previous match in case Whisper merged a
        # boundary, but keep the search strongly monotonic.
        start_index = max(0, minimum_word_index - 1)
        expected = expected_time(offset_start)
        for word_index in range(start_index, len(speech_rows)):
            first = speech_rows[word_index]
            if float(first["start"]) > float(search_end):
                break
            if expected is not None:
                delta = float(first["start"]) - float(expected)
                if delta < -3.0:
                    continue
                if delta > 3.0:
                    break
            combined_folded = ""
            combined_surface = ""
            for width in range(1, 4):
                end_index = word_index + width
                if end_index > len(speech_rows):
                    break
                last = speech_rows[end_index - 1]
                if float(last["end"]) - float(first["start"]) > 3.5:
                    break
                combined_folded += str(last["folded"])
                combined_surface += str(last["surface"])
                if not combined_folded and not combined_surface:
                    continue
                reading_score = (
                    100.0
                    if reading and (reading == combined_folded or reading in combined_folded)
                    else (float(fuzz.ratio(reading, combined_folded)) if reading and combined_folded else 0.0)
                )
                surface_score = (
                    100.0
                    if surface and (surface == combined_surface or surface in combined_surface)
                    else (float(fuzz.ratio(surface, combined_surface)) if surface and combined_surface else 0.0)
                )
                score = max(reading_score, surface_score)
                target_len = max(len(reading), len(surface))
                threshold = 96.0 if target_len <= 3 else (90.0 if target_len <= 6 else 84.0)
                if score < threshold:
                    continue
                candidate = (
                    score,
                    word_index,
                    end_index,
                    float(first["start"]),
                    float(last["end"]),
                )
                if best is None:
                    best = candidate
                else:
                    # Prefer higher score, then shorter/earlier windows.
                    best_span = best[4] - best[3]
                    candidate_span = candidate[4] - candidate[3]
                    if (candidate[0], -candidate_span, -candidate[3]) > (
                        best[0],
                        -best_span,
                        -best[3],
                    ):
                        best = candidate
            # Once we have a very strong nearby match, avoid scanning the whole
            # precision window and accidentally selecting a repeated word later.
            if best is not None and best[0] >= 99.9 and word_index > best[1] + 4:
                break

        if best is None:
            continue
        score, word_index, end_index, start, end = best
        if matches:
            previous = matches[-1]
            if start < float(previous["start"]) - 0.05:
                continue
            if offset_start < int(previous["offset_start"]):
                continue
        matches.append(
            {
                "offset_start": offset_start,
                "offset_end": offset_end,
                "start": start,
                "end": end,
                "score": score,
                "word_index": word_index,
                "end_index": end_index,
            }
        )
        minimum_word_index = max(minimum_word_index, end_index)

    if len(matches) < 3:
        return [], None

    output: list[dict[str, Any]] = []
    for row in matches:
        for offset, time_value in (
            (int(row["offset_start"]), float(row["start"])),
            (int(row["offset_end"]), float(row["end"])),
        ):
            candidate = {"offset": offset, "time": round(time_value, 3)}
            if output:
                previous = output[-1]
                if float(candidate["time"]) < float(previous["time"]) - 0.0005:
                    continue
                if float(candidate["offset"]) < float(previous["offset"]) - 1e-6:
                    continue
                if (
                    abs(float(candidate["time"]) - float(previous["time"])) <= 0.0005
                    and abs(float(candidate["offset"]) - float(previous["offset"])) <= 1e-6
                ):
                    continue
            output.append(candidate)

    if len(output) < 5 or float(output[-1]["offset"]) < 15.0:
        return [], None
    return output, {
        "mode": "precision_word_reading_prefix",
        "match_count": len(matches),
        "verified_through_offset": int(float(output[-1]["offset"])),
        "evidence": [
            {
                "offset_start": int(row["offset_start"]),
                "offset_end": int(row["offset_end"]),
                "start": round(float(row["start"]), 3),
                "end": round(float(row["end"]), 3),
                "score": round(float(row["score"]), 2),
            }
            for row in matches[:8]
        ],
    }


def _runtime_precision_reading_prefix(
    chapter: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Refine a recovered chapter prefix with cached word-level precision STT.

    A recovered spoken-title chapter can have the correct ``story_start`` and
    pause hold while still mapping the following 20-40 characters linearly.
    That makes short words such as 「しばらく」 finish far too early.  The
    precision chapter-start STT pass already stores word timestamps; combine
    those with current reader/Jiten reading hints and splice the resulting
    phonetic anchors into the live clock.  This is in-memory only and never
    rewrites the persisted alignment cache.
    """

    leading_debug = chapter.get("leading_prefix_debug")
    hints = chapter.get("_runtime_reading_hints")
    segments = chapter.get("_runtime_precision_segments")
    if not (
        isinstance(leading_debug, dict)
        and bool(leading_debug.get("chapter_marker"))
        and isinstance(hints, list)
        and hints
        and isinstance(segments, list)
        and segments
        and len(anchors) >= 2
    ):
        return anchors, 0, None

    prose_start, _hold_start = _chapter_recovered_story_hold(chapter)
    if prose_start is None:
        return anchors, 0, None

    try:
        search_start = float(
            leading_debug.get("precision_window_start")
            if leading_debug.get("precision_window_start") is not None
            else max(0.0, float(prose_start) - 4.0)
        )
        search_end = float(
            leading_debug.get("precision_window_end")
            if leading_debug.get("precision_window_end") is not None
            else float(prose_start) + 36.0
        )
    except (TypeError, ValueError):
        search_start = max(0.0, float(prose_start) - 4.0)
        search_end = float(prose_start) + 36.0

    reading_clock, reading_debug = _precision_reading_word_clock(
        hints,
        segments,
        search_start=search_start,
        search_end=search_end,
        coarse_anchors=anchors,
    )
    if not reading_clock or not isinstance(reading_debug, dict):
        reading_clock, _reading_start, reading_debug = _local_reading_hint_clock(
            hints,
            segments,
            search_start=search_start,
            search_end=search_end,
            reference_time=float(prose_start),
        )
    if not reading_clock or not isinstance(reading_debug, dict):
        return anchors, 0, None

    # For the real recovered-prefix bug we need enough evidence to cover the
    # first phrase after the pause, not merely the first token.  The matcher has
    # already applied strict fuzzy/phonetic thresholds; require a useful span
    # before replacing any live anchors.
    last_offset = float(reading_clock[-1].get("offset") or 0.0)
    if len(reading_clock) < 3 or last_offset < 15.0:
        return anchors, 0, None

    refined: list[dict[str, Any]] = [{"offset": 0, "time": round(float(prose_start), 3)}]
    for row in reading_clock:
        try:
            offset = float(row.get("offset") or 0.0)
            time_value = float(row.get("time") or 0.0)
        except (TypeError, ValueError):
            continue
        if time_value < float(prose_start) - 0.002:
            continue
        if abs(offset) <= 1e-6 and time_value <= float(prose_start) + 0.0005:
            continue
        candidate = {
            "offset": int(round(offset))
            if abs(offset - round(offset)) < 1e-6
            else round(offset, 4),
            "time": round(time_value, 3),
        }
        if refined:
            if float(candidate["time"]) <= float(refined[-1]["time"]) + 0.0005:
                if (
                    abs(float(candidate["offset"]) - float(refined[-1]["offset"])) <= 1e-6
                    and float(candidate["time"]) > float(refined[-1]["time"])
                ):
                    refined[-1] = candidate
                continue
            if float(candidate["offset"]) < float(refined[-1]["offset"]) - 1e-6:
                continue
        refined.append(candidate)
    if len(refined) < 3 or float(refined[-1]["offset"]) < 15.0:
        return anchors, 0, None

    story_index = next(
        (
            index
            for index, row in enumerate(anchors)
            if abs(float(row.get("time") or 0.0) - float(prose_start)) <= 0.002
            and abs(float(row.get("offset") or 0.0)) <= 1e-6
        ),
        None,
    )
    if story_index is None:
        return anchors, 0, None

    last = refined[-1]
    bridge_index = None
    wall_clock_bridge = False
    for index in range(story_index + 1, len(anchors)):
        try:
            offset = float(anchors[index].get("offset") or 0.0)
            time_value = float(anchors[index].get("time") or 0.0)
        except (TypeError, ValueError):
            continue
        if offset <= float(last["offset"]) + 1e-6:
            continue
        if time_value <= float(last["time"]) + 0.02:
            continue
        rate = (offset - float(last["offset"])) / max(
            0.02, time_value - float(last["time"])
        )
        if rate <= 16.0:
            bridge_index = index

            # Recovered chapter prefixes encode a pause as ``N-0.001`` at the
            # end of speech, the same offset held through silence, then ``N`` at
            # the next speech onset. If the first coarse bridge after a
            # precision-STT prefix would otherwise squeeze a long phrase into a
            # very high character rate, bridge to the *end* of that hold.
            if rate > 7.5 and index + 2 < len(anchors):
                try:
                    bridge_offset = float(anchors[index].get("offset") or 0.0)
                    bridge_time = float(anchors[index].get("time") or 0.0)
                    hold_offset = float(anchors[index + 1].get("offset") or 0.0)
                    hold_time = float(anchors[index + 1].get("time") or 0.0)
                    resume_offset = float(anchors[index + 2].get("offset") or 0.0)
                    resume_time = float(anchors[index + 2].get("time") or 0.0)
                except (TypeError, ValueError):
                    pass
                else:
                    whole_offset = round(bridge_offset)
                    encoded_pre_boundary = abs(
                        bridge_offset - (float(whole_offset) - 0.001)
                    ) <= 0.01
                    same_hold_offset = abs(hold_offset - bridge_offset) <= 0.01
                    resumes_at_boundary = abs(resume_offset - float(whole_offset)) <= 0.01
                    meaningful_pause = 0.25 <= hold_time - bridge_time <= 1.5
                    immediate_resume = 0.0 <= resume_time - hold_time <= 0.08
                    if (
                        encoded_pre_boundary
                        and same_hold_offset
                        and resumes_at_boundary
                        and meaningful_pause
                        and immediate_resume
                    ):
                        bridge_index = index + 2
                        wall_clock_bridge = True
            break

    merged = [
        *[dict(row) for row in anchors[:story_index]],
        *refined,
    ]
    if bridge_index is not None:
        coarse_tail = [dict(row) for row in anchors[bridge_index:]]
        if wall_clock_bridge and coarse_tail:
            coarse_tail[0]["wall_clock_from_previous"] = True
        merged.extend(coarse_tail)

    output: list[dict[str, Any]] = []
    for row in merged:
        try:
            time_value = round(float(row.get("time") or 0.0), 3)
            raw_offset = float(row.get("offset") or 0.0)
        except (TypeError, ValueError):
            continue
        offset_value: float | int = (
            int(round(raw_offset))
            if abs(raw_offset - round(raw_offset)) < 1e-6
            else round(raw_offset, 4)
        )
        if output:
            if time_value <= float(output[-1]["time"]) + 0.0005:
                continue
            if float(offset_value) < float(output[-1]["offset"]) - 1e-6:
                continue
        normalized_row: dict[str, Any] = {"offset": offset_value, "time": time_value}
        if bool(row.get("wall_clock_from_previous")):
            normalized_row["wall_clock_from_previous"] = True
        output.append(normalized_row)
    if len(output) < len(refined):
        return anchors, 0, None

    debug = {
        **reading_debug,
        "runtime": True,
        "precision_word_timing": True,
        "anchor_count": len(refined),
        "verified_through_offset": int(float(refined[-1]["offset"])),
        "wall_clock_pause_bridge": wall_clock_bridge,
    }
    return output, len(refined), debug



def _reading_mora_units(value: str) -> float:
    reading = _hiragana_fold(str(value or ""))
    if not reading:
        return 0.0
    small = set("ゃゅょぁぃぅぇぉゎゕゖ")
    units = 0.0
    for index, char in enumerate(reading):
        if char in small and index > 0:
            continue
        units += 1.0
    return max(1.0, units)


def _runtime_reading_weighted_bridge(
    chapter: dict[str, Any],
    anchors: list[dict[str, Any]],
    reading_debug: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Subdivide the first coarse gap after precision STT by spoken reading.

    Once precision word matching ends, the old clock falls back to character
    interpolation until the next coarse/pause anchor. Kanji-heavy phrases then
    consume too little time and visibly run ahead before catching up. Existing
    Jiten parse hints already contain token readings, so use their mora weight to
    place conservative in-memory token-boundary anchors while preserving the exact
    left/right audio anchors.
    """
    hints = chapter.get("_runtime_reading_hints")
    if not isinstance(hints, list) or not hints or not isinstance(reading_debug, dict):
        return anchors, 0, None
    try:
        verified = float(reading_debug.get("verified_through_offset") or 0.0)
    except (TypeError, ValueError):
        return anchors, 0, None
    if verified <= 0.0 or len(anchors) < 2:
        return anchors, 0, None

    left_index = None
    for index, row in enumerate(anchors):
        try:
            offset = float(row.get("offset") or 0.0)
        except (TypeError, ValueError):
            continue
        if offset <= verified + 1e-6:
            left_index = index
            continue
        break
    if left_index is None or left_index + 1 >= len(anchors):
        return anchors, 0, None
    left = anchors[left_index]
    right_index = left_index + 1
    # Skip zero-width pause-hold points at the same source offset; the first
    # strictly later source offset is the bridge endpoint.
    while right_index < len(anchors):
        try:
            if float(anchors[right_index].get("offset") or 0.0) > float(left.get("offset") or 0.0) + 0.5:
                break
        except (TypeError, ValueError):
            pass
        right_index += 1
    if right_index >= len(anchors):
        return anchors, 0, None
    right = anchors[right_index]
    wall_clock_bridge = bool(right.get("wall_clock_from_previous"))
    try:
        left_offset = float(left.get("offset") or 0.0)
        right_offset = float(right.get("offset") or 0.0)
        left_time = float(left.get("time") or 0.0)
        right_time = float(right.get("time") or 0.0)
    except (TypeError, ValueError):
        return anchors, 0, None
    span = right_offset - left_offset
    duration = right_time - left_time
    if not (2.0 <= span <= 64.0 and 0.25 <= duration <= 12.0):
        return anchors, 0, None

    rows: list[tuple[float, float, float]] = []
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        try:
            start = float(hint.get("offset_start") or 0.0)
            end = float(hint.get("offset_end") or 0.0)
        except (TypeError, ValueError):
            continue
        if end <= left_offset + 1e-6 or start >= right_offset - 1e-6:
            continue
        start = max(left_offset, start)
        end = min(right_offset, end)
        if end <= start + 1e-6:
            continue
        weight = _reading_mora_units(str(hint.get("reading") or ""))
        if weight <= 0.0:
            continue
        rows.append((start, end, weight))
    rows.sort(key=lambda row: (row[0], row[1]))
    non_overlapping: list[tuple[float, float, float]] = []
    cursor = left_offset
    for start, end, weight in rows:
        if start < cursor - 1e-6:
            continue
        non_overlapping.append((start, end, weight))
        cursor = end
    covered = sum(end - start for start, end, _weight in non_overlapping)
    if len(non_overlapping) < 2 or covered / max(1.0, span) < 0.35:
        return anchors, 0, None

    weighted_segments: list[tuple[float, float, float, bool]] = []
    cursor = left_offset
    for start, end, weight in non_overlapping:
        if start > cursor + 1e-6:
            weighted_segments.append((cursor, start, start - cursor, False))
        weighted_segments.append((start, end, weight, True))
        cursor = end
    if cursor < right_offset - 1e-6:
        weighted_segments.append((cursor, right_offset, right_offset - cursor, False))
    total_weight = sum(max(0.001, row[2]) for row in weighted_segments)
    if total_weight <= 0.0:
        return anchors, 0, None

    inserted: list[dict[str, Any]] = []
    cumulative = 0.0
    for start, end, weight, from_reading in weighted_segments:
        cumulative += max(0.001, weight)
        if end >= right_offset - 1e-6:
            break
        when = left_time + duration * (cumulative / total_weight)
        # Only token/gap boundaries that actually change raw linear timing are
        # useful; keep the live clock compact.
        linear_when = left_time + duration * ((end - left_offset) / span)
        if not from_reading and abs(when - linear_when) < 0.025:
            continue
        inserted_row: dict[str, Any] = {
            "offset": int(round(end)) if abs(end - round(end)) < 1e-6 else round(end, 4),
            "time": round(when, 3),
        }
        if wall_clock_bridge:
            inserted_row["wall_clock_from_previous"] = True
        inserted.append(inserted_row)
    if not inserted:
        return anchors, 0, None

    merged = [
        *[dict(row) for row in anchors[: left_index + 1]],
        *inserted,
        *[dict(row) for row in anchors[left_index + 1 :]],
    ]
    return merged, len(inserted), {
        "mode": "reading_weighted_post_precision_bridge",
        "left_offset": round(left_offset, 3),
        "right_offset": round(right_offset, 3),
        "left_time": round(left_time, 3),
        "right_time": round(right_time, 3),
        "hint_count": len(non_overlapping),
        "coverage": round(covered / max(1.0, span), 4),
        "anchor_count": len(inserted),
        "wall_clock_bridge": wall_clock_bridge,
    }

def _anchor_chain(
    novel: str,
    transcript: str,
    *,
    size: int,
    maximum_occurrences: int = 4,
) -> list[tuple[int, int]]:
    if len(novel) < size or len(transcript) < size:
        return []
    transcript_index: dict[str, list[int]] = {}
    for index in range(len(transcript) - size + 1):
        key = transcript[index : index + size]
        rows = transcript_index.setdefault(key, [])
        if len(rows) <= maximum_occurrences:
            rows.append(index)

    candidates: list[tuple[int, int]] = []
    for novel_index in range(len(novel) - size + 1):
        matches = transcript_index.get(novel[novel_index : novel_index + size]) or []
        if 0 < len(matches) <= maximum_occurrences:
            candidates.extend((novel_index, transcript_index_) for transcript_index_ in matches)
    if not candidates:
        return []
    # Descending transcript positions for equal novel positions prevent the
    # LIS from selecting several alternative matches for the same LN n-gram.
    candidates.sort(key=lambda item: (item[0], -item[1]))

    tails: list[int] = []
    tails_rows: list[int] = []
    previous = [-1] * len(candidates)
    for row_index, (_novel_index, transcript_index_) in enumerate(candidates):
        position = bisect.bisect_left(tails, transcript_index_)
        if position == len(tails):
            tails.append(transcript_index_)
            tails_rows.append(row_index)
        else:
            tails[position] = transcript_index_
            tails_rows[position] = row_index
        if position:
            previous[row_index] = tails_rows[position - 1]

    chain: list[tuple[int, int]] = []
    cursor = tails_rows[-1]
    while cursor >= 0:
        chain.append(candidates[cursor])
        cursor = previous[cursor]
    chain.reverse()

    # Keep almost every monotonic match.  Older builds kept one point per three
    # normalized characters.  That was enough to identify a chapter but could
    # drift visibly inside long sentences and around a chapter transition.
    # One point per two characters is still small compared with the cached STT
    # result and gives seeking/highlighting a substantially denser clock.
    compact: list[tuple[int, int]] = []
    for novel_index, transcript_index_ in chain:
        if compact and (
            novel_index <= compact[-1][0]
            or transcript_index_ <= compact[-1][1]
        ):
            continue
        if compact and novel_index - compact[-1][0] < 2:
            continue
        compact.append((novel_index, transcript_index_))
    return compact


def _best_dense_anchor_chain(novel: str, transcript: str) -> tuple[int, list[tuple[int, int]]]:
    """Return the densest reliable monotonic clock shared by LN and STT.

    Longer n-grams establish an unambiguous backbone.  A shorter chain is used
    only when it agrees with the same broad start/end corridor, which prevents
    common Japanese particles from creating a plausible but wrong timeline.
    """

    candidates: list[tuple[int, list[tuple[int, int]]]] = []
    for size, occurrences in ((11, 8), (9, 6), (7, 4), (5, 3), (4, 2)):
        chain = _anchor_chain(
            novel,
            transcript,
            size=size,
            maximum_occurrences=occurrences,
        )
        if len(chain) >= 8:
            candidates.append((size, chain))
    if not candidates:
        return 0, []

    backbone_size, backbone = candidates[0]
    best_size, best = backbone_size, backbone
    backbone_novel_span = max(1, backbone[-1][0] - backbone[0][0])
    backbone_audio_span = max(1, backbone[-1][1] - backbone[0][1])
    for size, chain in candidates[1:]:
        novel_overlap = min(backbone[-1][0], chain[-1][0]) - max(backbone[0][0], chain[0][0])
        audio_overlap = min(backbone[-1][1], chain[-1][1]) - max(backbone[0][1], chain[0][1])
        if novel_overlap < backbone_novel_span * 0.65 or audio_overlap < backbone_audio_span * 0.65:
            continue
        if len(chain) > len(best):
            best_size, best = size, chain
    return best_size, best


def _fuzzy_global_anchor_chain(
    novel: str,
    transcript: str,
    *,
    minimum_similarity: float = 0.48,
    maximum_characters: int = 180_000,
) -> tuple[int, list[tuple[int, int]], float]:
    """Recover a monotonic clock when Whisper errors destroy exact n-grams.

    Inspired by SubPlz/AudiobookTextSync's global character alignment, but uses
    RapidFuzz which Pudge already depends on.  It is deliberately a fallback:
    large or weakly related inputs are rejected before their mapping can affect
    playback.
    """

    if min(len(novel), len(transcript)) < 40:
        return 0, [], 0.0
    if max(len(novel), len(transcript)) > maximum_characters:
        return 0, [], 0.0
    length_ratio = min(len(novel), len(transcript)) / max(len(novel), len(transcript))
    if length_ratio < 0.40:
        return 0, [], 0.0

    similarity = float(
        Levenshtein.normalized_similarity(
            novel, transcript, score_cutoff=minimum_similarity
        )
    )
    if similarity < minimum_similarity:
        return 0, [], similarity

    # Equal runs from the global edit path are genuine character matches even
    # when substitutions/insertions between them prevented 4+-gram anchoring.
    anchor_size = 3
    anchors: list[tuple[int, int]] = []
    matched_characters = 0
    for tag, src_start, src_end, dest_start, dest_end in Levenshtein.opcodes(
        novel, transcript
    ).as_list():
        if tag != "equal":
            continue
        length = min(src_end - src_start, dest_end - dest_start)
        if length < anchor_size:
            continue
        matched_characters += length
        last_delta = max(0, length - anchor_size)
        deltas = list(range(0, last_delta + 1, 2))
        if deltas[-1] != last_delta:
            deltas.append(last_delta)
        anchors.extend((src_start + delta, dest_start + delta) for delta in deltas)

    coverage = matched_characters / max(1, min(len(novel), len(transcript)))
    if len(anchors) < 8 or coverage < 0.28:
        return 0, [], similarity
    return anchor_size, anchors, similarity


def _clip_activity_regions(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
) -> list[dict[str, float]]:
    clipped: list[dict[str, float]] = []
    for row in regions:
        if not isinstance(row, dict):
            continue
        try:
            region_start = max(float(start), float(row.get("start") or 0.0))
            region_end = min(float(end), float(row.get("end") or 0.0))
        except (TypeError, ValueError):
            continue
        if region_end - region_start >= 0.015:
            clipped.append(
                {"start": round(region_start, 3), "end": round(region_end, 3)}
            )
    return clipped


def _activity_ratio(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
    position: float,
) -> float:
    """Measure progress in spoken time, holding still through quiet gaps."""

    start = float(start)
    end = max(start + 0.02, float(end))
    position = max(start, min(end, float(position)))
    clipped = _clip_activity_regions(regions, start, end)
    total = sum(float(row["end"]) - float(row["start"]) for row in clipped)
    if total < max(0.04, (end - start) * 0.025):
        return (position - start) / (end - start)
    elapsed = 0.0
    for row in clipped:
        region_start = float(row["start"])
        region_end = float(row["end"])
        if position <= region_start:
            break
        elapsed += max(0.0, min(position, region_end) - region_start)
        if position < region_end:
            break
    return max(0.0, min(1.0, elapsed / total))


def _time_for_activity_ratio(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
    ratio: float,
) -> float:
    start = float(start)
    end = max(start + 0.02, float(end))
    ratio = max(0.0, min(1.0, float(ratio)))
    clipped = _clip_activity_regions(regions, start, end)
    total = sum(float(row["end"]) - float(row["start"]) for row in clipped)
    if total < max(0.04, (end - start) * 0.025):
        return start + ratio * (end - start)
    remaining = ratio * total
    for row in clipped:
        region_start = float(row["start"])
        length = float(row["end"]) - region_start
        if remaining < length - 1e-6:
            return region_start + remaining
        remaining -= length
    return float(clipped[-1]["end"])



def _chapter_uses_activity_clock(chapter: dict[str, Any]) -> bool:
    """Use VAD-weighted interpolation only on modern enriched LN clocks.

    Older/synthetic reading-audio-v3 payloads intentionally define a purely
    linear anchor clock even when they carry coarse speech_regions.  Real LN
    alignments that have punctuation-pause enrichment are dense enough for the
    activity clock to be meaningful and safe.
    """

    return bool(
        int(chapter.get("punctuation_pause_count") or 0) > 0
        or int(chapter.get("_runtime_punctuation_pause_count") or 0) > 0
        or bool(chapter.get("activity_clock"))
    )

def _anchor_segment_uses_activity_clock(
    chapter: dict[str, Any],
    right_anchor: dict[str, Any],
) -> bool:
    """Return whether one anchor segment should compress quiet VAD gaps.

    A precision-prefix bridge can deliberately borrow the following encoded
    pause to avoid racing through an under-timed coarse phrase.  That segment
    must use wall time; otherwise the chapter-wide activity clock removes the
    borrowed pause again and makes the repair a no-op.
    """

    return bool(
        _chapter_uses_activity_clock(chapter)
        and not bool(right_anchor.get("wall_clock_from_previous"))
    )

def _acoustic_chapter_start(
    estimated_start: float,
    first_anchor_time: float,
    speech_regions: Iterable[dict[str, Any]],
) -> float:
    """Do not extrapolate readable text backwards through leading silence.

    The exact-text matcher can know that a phrase near the first reliable anchor
    is correct while still having no evidence for the minutes/seconds before it.
    Extrapolating from character rate used to pin chapter offset 0 to audio 0,
    even when VAD showed that the first matched speech starts later.  Choose the
    speech region immediately containing/preceding the first anchor instead.
    """

    start = max(0.0, float(estimated_start))
    first_anchor = max(0.0, float(first_anchor_time))
    regions = sorted(
        (
            {"start": float(row.get("start") or 0.0), "end": float(row.get("end") or 0.0)}
            for row in speech_regions
            if isinstance(row, dict)
        ),
        key=lambda row: (row["start"], row["end"]),
    )
    candidates = [
        row
        for row in regions
        if row["end"] > row["start"]
        and row["start"] <= first_anchor + 0.15
        and row["end"] >= first_anchor - 2.0
    ]
    if not candidates:
        return start
    region = candidates[-1]
    speech_start = max(0.0, float(region["start"]))
    # Only move a synthetic extrapolated boundary forward.  Never move an
    # already plausible chapter start backwards or across a tiny VAD wobble.
    if speech_start - start < 0.25:
        return start
    return min(speech_start, first_anchor)


def _acoustic_chapter_boundary(
    left_time: float,
    right_time: float,
    regions: Iterable[dict[str, Any]],
) -> float:
    """Prefer the next phrase onset after the longest chapter-sized pause."""

    fallback = (float(left_time) + float(right_time)) / 2
    clipped = _clip_activity_regions(regions, float(left_time), float(right_time))
    if len(clipped) < 2:
        return fallback
    gaps = [
        (float(right["start"]) - float(left["end"]), float(right["start"]))
        for left, right in pairwise(clipped)
    ]
    gap, next_onset = max(gaps, default=(0.0, fallback))
    return next_onset if gap >= 0.18 else fallback


def _chapter_recovered_story_hold(
    chapter: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Return (story_start, hold_start) for a recovered leading prefix.

    ``leading_prefix_start`` used to be the only source for the final hold.  In
    real cached alignments the chapter boundary can already be rebased while
    ``leading_prefix_debug.story_start`` contains the later, verified prose
    onset.  Treat the debug value as authoritative for a spoken chapter marker
    and derive the hold start from marker/raw timing evidence.
    """

    leading_debug = chapter.get("leading_prefix_debug")
    if not isinstance(leading_debug, dict) or not bool(leading_debug.get("recovered")):
        return None, None

    story_candidates: list[float] = []
    for raw in (chapter.get("leading_prefix_start"), leading_debug.get("story_start")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0.0:
            story_candidates.append(value)
    if not story_candidates:
        return None, None

    # For a detected 第X幕 marker, never let an older/smaller serialized prefix
    # value pull prose into the title.  For ordinary prefix recovery preserve
    # the historical explicit leading_prefix_start behavior.
    if bool(leading_debug.get("chapter_marker")):
        debug_story = leading_debug.get("story_start")
        try:
            story_start = float(debug_story)
        except (TypeError, ValueError):
            story_start = max(story_candidates)
    else:
        explicit = chapter.get("leading_prefix_start")
        try:
            story_start = float(explicit)
        except (TypeError, ValueError):
            story_start = max(story_candidates)

    hold_candidates: list[float] = []
    for raw in (chapter.get("start"), leading_debug.get("marker_start")):
        try:
            hold_candidates.append(float(raw))
        except (TypeError, ValueError):
            pass
    for row in chapter.get("anchors") or []:
        if not isinstance(row, dict):
            continue
        try:
            hold_candidates.append(float(row.get("time")))
        except (TypeError, ValueError):
            continue
    hold_start = min(hold_candidates) if hold_candidates else story_start
    return story_start, hold_start


def _raw_anchors_with_leading_hold(chapter: dict[str, Any]) -> list[dict[str, float | int]]:
    """Return final-source anchors with a zero-width spoken-title hold."""

    source_anchors = [row for row in chapter.get("anchors") or [] if isinstance(row, dict)]
    prose_start, hold_start = _chapter_recovered_story_hold(chapter)
    if prose_start is not None and hold_start is not None and prose_start > hold_start + 0.001:
        # Any positive offset before the verified prose onset belongs to a
        # borrowed title/prefix clock, not to readable LN prose.  Also discard
        # the same borrowed plateau immediately after story_start so the first
        # real semantic anchor remains the first positive offset.
        borrowed_prefix_ceiling = max(
            (
                float(row.get("offset") or 0.0)
                for row in source_anchors
                if float(row.get("time") or 0.0) < prose_start - 0.001
                and float(row.get("offset") or 0.0) > 0.0
            ),
            default=0.0,
        )
        source_anchors = sorted(
            (
                row
                for row in source_anchors
                if float(row.get("time") or 0.0) >= prose_start - 0.001
                and float(row.get("offset") or 0.0) > borrowed_prefix_ceiling + 0.001
            ),
            key=lambda row: (float(row.get("time") or 0.0), float(row.get("offset") or 0.0)),
        )
        # The explicit hold owns every zero-offset point. Keeping an older raw
        # 0@marker-time anchor after 0@story-start made this list non-monotonic
        # and left correctness dependent on a later pruning pass.
        return [
            {"offset": 0, "time": hold_start},
            {"offset": 0, "time": prose_start},
            *source_anchors,
            {"offset": int(chapter["normalized_length"]), "time": float(chapter["end"])},
        ]
    return [
        {"offset": 0, "time": float(chapter["start"])},
        *source_anchors,
        {"offset": int(chapter["normalized_length"]), "time": float(chapter["end"])},
    ]


def align_light_novel_to_transcript(
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    duration: float,
    model: str,
    speech_regions: list[dict[str, Any]] | None = None,
    chapter_start_segments: dict[int, list[dict[str, Any]]] | None = None,
    chapter_start_reading_hints: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    transcript, transcript_times = _transcript_clock(segments)
    chapter_ranges: list[dict[str, Any]] = []
    novel_parts: list[str] = []
    cursor = 0
    for chapter in chapters:
        spoken_text = chapter_audio_text(str(chapter.get("text") or ""))
        normalized = normalize_reading_text(spoken_text)
        if len(normalized) < 12:
            continue
        start = cursor
        novel_parts.append(normalized)
        cursor += len(normalized)
        chapter_ranges.append(
            {
                "chapter_index": int(chapter["chapter_index"]),
                "title": str(chapter.get("title") or ""),
                "global_start": start,
                "global_end": cursor,
                "length": len(normalized),
                "normalized_text": normalized,
                "punctuation_boundaries": _punctuation_boundaries(spoken_text),
            }
        )
    novel = "".join(novel_parts)
    if len(transcript) < 40 or len(novel) < 40:
        raise ValueError("STT produced too little Japanese text for alignment")

    anchor_size, anchors = _best_dense_anchor_chain(novel, transcript)
    alignment_method = "exact-ngram"
    fuzzy_similarity = None
    if len(anchors) < 8:
        anchor_size, anchors, fuzzy_similarity = _fuzzy_global_anchor_chain(novel, transcript)
        alignment_method = "fuzzy-global"
    if len(anchors) < 8:
        raise ValueError("Not enough matching STT/LN anchors")

    aligned: list[dict[str, Any]] = []
    for chapter in chapter_ranges:
        local = [
            (novel_index - chapter["global_start"], transcript_index_)
            for novel_index, transcript_index_ in anchors
            if chapter["global_start"] <= novel_index < chapter["global_end"]
        ]
        if len(local) < 2:
            continue
        span = local[-1][0] - local[0][0] + anchor_size
        if len(local) < 4 and span < chapter["length"] * 0.20:
            continue
        clock = _dense_anchor_clock(
            local,
            anchor_size=anchor_size,
            transcript_times=transcript_times,
            chapter_length=int(chapter["length"]),
        )
        leading_start = None
        leading_debug: dict[str, Any] = {"attempted": False}
        rough_first_time = float(clock[0]["time"]) if clock else 0.0
        chapter_index = int(chapter.get("chapter_index") or 0)
        precision_segments = (chapter_start_segments or {}).get(chapter_index) or []
        prefix_segments = precision_segments or segments
        clock, leading_start, leading_debug = _recover_leading_prefix_clock(
            str(chapter.get("normalized_text") or ""),
            prefix_segments,
            clock,
            chapter_title=str(chapter.get("title") or ""),
            chapter_index=chapter_index,
            force_verify=True,
            search_start=max(0.0, rough_first_time - 25.0),
            search_end=min(float(duration), rough_first_time + 75.0),
            reading_hints=(chapter_start_reading_hints or {}).get(chapter_index) or [],
        )
        if precision_segments:
            leading_debug = {
                **leading_debug,
                "precision_chapter_start_stt": True,
                "precision_segment_count": len(precision_segments),
            }
            # A precision pass is allowed to fail closed.  If it still cannot
            # verify the prefix, retry only the prefix matcher with the reusable
            # whole-book transcript before falling back to degraded geometry.
            if not bool(leading_debug.get("recovered")):
                fallback_clock, fallback_start, fallback_debug = _recover_leading_prefix_clock(
                    str(chapter.get("normalized_text") or ""),
                    segments,
                    clock,
                    chapter_title=str(chapter.get("title") or ""),
                    chapter_index=chapter_index,
                    force_verify=True,
                    search_start=max(0.0, rough_first_time - 25.0),
                    search_end=min(float(duration), rough_first_time + 75.0),
                    reading_hints=(chapter_start_reading_hints or {}).get(chapter_index) or [],
                )
                if bool(fallback_debug.get("recovered")):
                    clock, leading_start = fallback_clock, fallback_start
                    leading_debug = {
                        **fallback_debug,
                        "precision_chapter_start_stt": True,
                        "precision_segment_count": len(precision_segments),
                        "precision_match_failed": True,
                    }
        if not clock:
            continue
        if (
            chapter is chapter_ranges[0]
            and bool(leading_debug.get("attempted"))
            and not bool(leading_debug.get("recovered"))
            and int(leading_debug.get("first_offset") or 0) >= max(128, int(chapter["length"]) // 50)
            and float(clock[0].get("time") or 0.0) <= 180.0
        ):
            # Do not fail a deterministic Retry here.  Preserve the unsafe
            # evidence so the final leading-clock guard can replace it with a
            # physically reachable degraded bridge instead.
            leading_debug = {
                **leading_debug,
                "unsafe_unverified_prefix": True,
            }
        first_time = float(clock[0]["time"])
        last_time = float(clock[-1]["time"])
        character_rate = (
            max(0.25, (local[-1][0] - local[0][0]) / max(0.25, last_time - first_time))
            if len(local) > 1
            else 5.0
        )
        estimated_start = max(0.0, first_time - local[0][0] / character_rate)
        estimated_end = min(
            max(estimated_start + 0.25, float(duration)),
            last_time + max(0, chapter["length"] - local[-1][0]) / character_rate,
        )
        aligned.append(
            {
                "chapter_index": chapter["chapter_index"],
                "title": chapter["title"],
                "normalized_length": chapter["length"],
                "first_anchor_time": first_time,
                "last_anchor_time": last_time,
                "estimated_start": estimated_start,
                "estimated_end": estimated_end,
                "anchors": clock,
                "leading_prefix_start": leading_start,
                "leading_prefix_debug": leading_debug,
                "punctuation_boundaries": list(
                    chapter.get("punctuation_boundaries") or []
                ),
                "confidence": round(min(1.0, span / max(1, chapter["length"])), 4),
            }
        )
    if not aligned:
        raise ValueError("STT text did not match any readable LN chapter")

    aligned_by_index = {int(row["chapter_index"]): row for row in aligned}
    ordered_ranges = sorted(chapter_ranges, key=lambda row: int(row["chapter_index"]))
    if len(aligned_by_index) >= 2:
        available_indexes = sorted(aligned_by_index)
        for position, source in enumerate(ordered_ranges):
            chapter_index = int(source["chapter_index"])
            if chapter_index in aligned_by_index:
                continue
            left_candidates = [value for value in available_indexes if value < chapter_index]
            right_candidates = [value for value in available_indexes if value > chapter_index]
            if not left_candidates or not right_candidates:
                continue
            left = aligned_by_index[left_candidates[-1]]
            right = aligned_by_index[right_candidates[0]]
            left_end = float(left.get("estimated_end", left["last_anchor_time"]))
            right_start = float(right.get("estimated_start", right["first_anchor_time"]))
            gap_duration = right_start - left_end
            if gap_duration < 0.25:
                continue
            between = [row for row in ordered_ranges if left_candidates[-1] < int(row["chapter_index"]) < right_candidates[0]]
            total_chars = sum(int(row["length"]) for row in between) or 1
            elapsed = 0.0
            for row in between:
                span = gap_duration * int(row["length"]) / total_chars
                start_time = left_end + elapsed
                end_time = start_time + span
                elapsed += span
                if int(row["chapter_index"]) != chapter_index:
                    continue
                aligned.append(
                    {
                        "chapter_index": int(row["chapter_index"]),
                        "title": row["title"],
                        "normalized_length": int(row["length"]),
                        "first_anchor_time": start_time,
                        "last_anchor_time": end_time,
                        "estimated_start": start_time,
                        "estimated_end": end_time,
                        "anchors": [
                            {"offset": 0, "time": round(start_time, 3)},
                            {"offset": int(row["length"]), "time": round(end_time, 3)},
                        ],
                        "punctuation_boundaries": list(row.get("punctuation_boundaries") or []),
                        "confidence": 0.35,
                        "interpolated": True,
                    }
                )
                aligned_by_index[chapter_index] = aligned[-1]
                available_indexes = sorted(aligned_by_index)
                break

    aligned.sort(key=lambda row: int(row["chapter_index"]))

    for index, chapter in enumerate(aligned):
        if index:
            previous = aligned[index - 1]
            boundary = _acoustic_chapter_boundary(
                float(previous["last_anchor_time"]),
                float(chapter["first_anchor_time"]),
                speech_regions or [],
            )
            previous["end"] = round(max(float(previous["start"]) + 0.25, boundary), 3)
            chapter["start"] = round(max(0.0, boundary), 3)
        else:
            recovered_leading_start = chapter.get("leading_prefix_start")
            if recovered_leading_start is not None:
                chapter["start"] = round(max(0.0, float(recovered_leading_start)), 3)
            else:
                chapter["start"] = round(
                    _acoustic_chapter_start(
                        float(chapter["estimated_start"]),
                        float(chapter["first_anchor_time"]),
                        speech_regions or [],
                    ),
                    3,
                )
    aligned[-1]["end"] = round(
        min(max(float(aligned[-1]["start"]) + 0.25, float(duration)), float(aligned[-1]["estimated_end"])),
        3,
    )
    for chapter in aligned:
        chapter.setdefault("end", round(float(chapter["estimated_end"]), 3))
        # For non-first chapters the acoustic boundary can be the spoken
        # 第X幕 marker. Keep offset zero until the recovered prose start.
        raw_anchors = _raw_anchors_with_leading_hold(chapter)
        merged_anchors: list[dict[str, float | int]] = []
        for row in sorted(
            raw_anchors,
            key=lambda item: (float(item["time"]), float(item["offset"])),
        ):
            time_value = round(float(row["time"]), 3)
            offset_value = float(row["offset"])
            if merged_anchors and abs(time_value - float(merged_anchors[-1]["time"])) < 0.0005:
                previous_offset = float(merged_anchors[-1]["offset"])
                # Small instantaneous skips are legitimate when an audiobook
                # omits a short heading/technical prefix.  Hundreds of
                # characters at one timestamp are not: that is the v163
                # failure this guard is meant to block.
                if abs(previous_offset) < 1e-6 and offset_value - previous_offset > 64.0:
                    continue
                if offset_value <= previous_offset + 1e-6:
                    continue
            if merged_anchors and offset_value < float(merged_anchors[-1]["offset"]) - 1e-6:
                continue
            rounded = round(offset_value)
            output_offset: float | int = (
                int(rounded) if abs(offset_value - rounded) < 1e-6 else round(offset_value, 4)
            )
            merged_anchors.append({"offset": output_offset, "time": time_value})
        leading_debug = chapter.get("leading_prefix_debug")
        if isinstance(leading_debug, dict) and leading_debug.get("recovered"):
            merged_anchors = _prune_unreachable_leading_anchors(
                merged_anchors,
                max_rate=float(leading_debug.get("max_bridge_rate") or 10.0),
                verified_through_offset=float(
                    leading_debug.get("verified_through_offset") or 0.0
                ),
            )
        if chapter is aligned[0]:
            hazard = _unsafe_leading_anchor_jump(merged_anchors)
            if hazard is not None:
                merged_anchors, degraded = _safe_degraded_leading_bridge(
                    merged_anchors,
                    chapter_length=int(chapter["normalized_length"]),
                    chapter_start=float(chapter["start"]),
                    chapter_end=float(chapter["end"]),
                    max_rate=10.0,
                )
                hazard = _unsafe_leading_anchor_jump(merged_anchors)
                if hazard is not None:
                    raise ValueError(
                        "Unsafe first-chapter audiobook clock after degraded recovery: "
                        f"{hazard['left_offset']:.0f}->{hazard['right_offset']:.0f} chars "
                        f"at {hazard['characters_per_second']:.1f} chars/s"
                    )
                if degraded is not None:
                    leading_debug = chapter.get("leading_prefix_debug")
                    if not isinstance(leading_debug, dict):
                        leading_debug = {}
                    chapter["leading_prefix_debug"] = {
                        **leading_debug,
                        "attempted": True,
                        "recovered": True,
                        "degraded": True,
                        "bridge_found": True,
                        "fallback": degraded,
                    }
        chapter["anchors"] = merged_anchors
        chapter["speech_regions"] = _clip_activity_regions(
            speech_regions or [],
            float(chapter["start"]),
            float(chapter["end"]),
        )
        leading_debug = chapter.get("leading_prefix_debug")
        fallback = (
            leading_debug.get("fallback")
            if isinstance(leading_debug, dict)
            and isinstance(leading_debug.get("fallback"), dict)
            else {}
        )
        fallback_mode = str(fallback.get("mode") or "")
        if fallback_mode in {"safe_rejoin", "linear_chapter"}:
            # A degraded bridge is not text evidence.  Punctuation injection
            # used to slice that synthetic line into 50/100-character chunks,
            # making it look like a precise clock and causing the highlight to
            # race ahead.  Keep the bridge visibly sparse instead.
            chapter.pop("punctuation_boundaries", None)
            pause_count = 0
        else:
            chapter["anchors"], pause_count = _inject_punctuation_pause_anchors(
                list(chapter["anchors"]),
                chapter.pop("punctuation_boundaries", []),
                chapter["speech_regions"],
            )

        # Punctuation/VAD enrichment can itself create a transient fast edge
        # inside the recovered prose prefix (real S&W v1: 8->19.999 and
        # 25->32.999). Run the verified-rate guard after that enrichment and
        # only before the verifier endpoint. The old pre-enrichment,
        # whole-chapter pass missed these synthetic anchors and pruned
        # thousands of legitimate anchors later in the chapter.
        leading_debug = chapter.get("leading_prefix_debug")
        if isinstance(leading_debug, dict) and leading_debug.get("recovered"):
            max_bridge_rate = float(leading_debug.get("max_bridge_rate") or 0.0)
            verified_through_offset = float(
                leading_debug.get("verified_through_offset") or 0.0
            )
            if max_bridge_rate > 0.0 and verified_through_offset > 0.0:
                chapter["anchors"], local_rate_debug = _prune_rejoining_local_rate_outliers(
                    list(chapter["anchors"]),
                    max_rate=max_bridge_rate,
                    preferred_rate=float(leading_debug.get("verified_rate") or 0.0),
                    max_offset_exclusive=verified_through_offset,
                )
                chapter["leading_prefix_debug"] = {
                    **leading_debug,
                    "local_rate_prune": local_rate_debug,
                }

        chapter["punctuation_pause_count"] = pause_count
        for key in ("first_anchor_time", "last_anchor_time", "estimated_start", "estimated_end", "leading_prefix_start"):
            chapter.pop(key, None)

    return {
        "schema": "reading-audio-v3",
        "model": str(model),
        "duration": round(float(duration), 3),
        "transcript_characters": len(transcript),
        "novel_characters": len(novel),
        "anchor_count": sum(len(row.get("anchors") or []) for row in aligned),
        "matched_anchor_count": len(anchors),
        "punctuation_pause_count": sum(
            int(row.get("punctuation_pause_count") or 0) for row in aligned
        ),
        "anchor_size": anchor_size,
        "alignment_method": alignment_method,
        "fuzzy_similarity": round(float(fuzzy_similarity), 4) if fuzzy_similarity is not None else None,
        "leading_prefix_recovered": bool(
            aligned
            and isinstance(aligned[0].get("leading_prefix_debug"), dict)
            and aligned[0]["leading_prefix_debug"].get("recovered")
        ),
        "chapters": aligned,
        "confidence": round(sum(float(row["confidence"]) for row in aligned) / len(aligned), 4),
    }


def _runtime_anchors_for_chapter(chapter: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the live clock, enriching old cached alignments in memory.

    Older ``reading-audio-v3`` cache files can predate punctuation-pause
    injection. They still contain acoustic speech regions, so once the caller
    attaches current LN punctuation boundaries we can recover missing hold
    anchors without rebuilding STT/alignment or rewriting the cache file.
    """

    cached = chapter.get("_runtime_enriched_anchors")
    if isinstance(cached, list) and cached:
        return cached

    leading_debug = chapter.get("leading_prefix_debug")
    if isinstance(leading_debug, dict) and bool(leading_debug.get("chapter_marker")):
        anchors = _raw_anchors_with_leading_hold(chapter)
    else:
        anchors = [
            dict(row) for row in chapter.get("anchors") or [] if isinstance(row, dict)
        ]

    boundaries = chapter.get("_runtime_punctuation_boundaries")
    stored_pause_count = int(chapter.get("punctuation_pause_count") or 0)
    runtime_pause_count = 0
    speech_regions = list(chapter.get("speech_regions") or [])

    recovered_prefix_end_time: float | None = None
    if isinstance(leading_debug, dict) and bool(leading_debug.get("chapter_marker")):
        prose_start, _hold_start = _chapter_recovered_story_hold(chapter)
        if prose_start is not None:
            for row in anchors:
                try:
                    row_time = float(row.get("time") or 0.0)
                    row_offset = float(row.get("offset") or 0.0)
                except (TypeError, ValueError):
                    continue
                if row_time > float(prose_start) + 0.0005 and row_offset > 0.001:
                    recovered_prefix_end_time = row_time
                    break

    anchors, runtime_reading_anchor_count, runtime_reading_debug = (
        _runtime_precision_reading_prefix(chapter, anchors)
    )
    chapter["_runtime_reading_hint_anchor_count"] = runtime_reading_anchor_count
    chapter["_runtime_reading_hint_debug"] = runtime_reading_debug

    # Precision STT may extend far beyond the originally recovered prefix.
    # Re-apply punctuation/VAD holds through the *whole precision span*, not
    # merely through the first coarse positive anchor. Otherwise a new precise
    # word onset can overwrite an existing pause hold (real S&W v2 ch3: まず).
    if runtime_reading_anchor_count > 0 and isinstance(runtime_reading_debug, dict):
        try:
            verified_offset = float(
                runtime_reading_debug.get("verified_through_offset") or 0.0
            )
        except (TypeError, ValueError):
            verified_offset = 0.0
        if verified_offset > 0.0:
            precision_end_time = None
            for row in anchors:
                try:
                    row_offset = float(row.get("offset") or 0.0)
                    row_time = float(row.get("time") or 0.0)
                except (TypeError, ValueError):
                    continue
                if row_offset <= verified_offset + 1e-6:
                    precision_end_time = row_time
                    continue
                break
            if precision_end_time is not None:
                recovered_prefix_end_time = max(
                    float(recovered_prefix_end_time or 0.0),
                    float(precision_end_time),
                )

    if isinstance(boundaries, list) and boundaries and len(anchors) >= 2:
        if (
            isinstance(leading_debug, dict)
            and bool(leading_debug.get("chapter_marker"))
            and stored_pause_count > 0
        ):
            # Stored pause anchors can exist for the rest of the chapter while
            # the recovered spoken-title cleanup deliberately discards the
            # borrowed positive prefix. Re-inject pauses only inside the
            # recovered prefix. When word-level precision anchors are present,
            # keep them and add the acoustic hold around them instead of
            # collapsing back to a two-point linear clock.
            prose_start, _hold_start = _chapter_recovered_story_hold(chapter)
            if prose_start is not None:
                story_index = next(
                    (
                        index
                        for index, row in enumerate(anchors)
                        if abs(float(row.get("time") or 0.0) - float(prose_start)) <= 0.002
                        and abs(float(row.get("offset") or 0.0)) <= 1e-6
                    ),
                    None,
                )
                if story_index is not None:
                    if runtime_reading_anchor_count > 0 and recovered_prefix_end_time is not None:
                        prefix_end_index = max(
                            story_index + 1,
                            next(
                                (
                                    index
                                    for index in range(story_index + 1, len(anchors))
                                    if float(anchors[index].get("time") or 0.0)
                                    >= float(recovered_prefix_end_time) - 0.0005
                                ),
                                len(anchors) - 1,
                            ),
                        )
                        prefix = [
                            dict(row)
                            for row in anchors[story_index : prefix_end_index + 1]
                        ]
                        enriched_prefix, prefix_pause_count = _inject_punctuation_pause_anchors(
                            prefix,
                            boundaries,
                            speech_regions,
                        )
                        if prefix_pause_count > 0:
                            anchors = [
                                *anchors[:story_index],
                                *enriched_prefix,
                                *anchors[prefix_end_index + 1 :],
                            ]
                            runtime_pause_count += prefix_pause_count
                    else:
                        positive_index = next(
                            (
                                index
                                for index in range(story_index + 1, len(anchors))
                                if float(anchors[index].get("offset") or 0.0) > 0.001
                            ),
                            None,
                        )
                        if positive_index is not None:
                            prefix = [
                                dict(anchors[story_index]),
                                dict(anchors[positive_index]),
                            ]
                            enriched_prefix, prefix_pause_count = _inject_punctuation_pause_anchors(
                                prefix,
                                boundaries,
                                speech_regions,
                            )
                            if prefix_pause_count > 0:
                                anchors = [
                                    *anchors[:story_index],
                                    *enriched_prefix,
                                    *anchors[positive_index + 1 :],
                                ]
                                runtime_pause_count += prefix_pause_count
        elif stored_pause_count <= 0:
            anchors, runtime_pause_count = _inject_punctuation_pause_anchors(
                anchors,
                boundaries,
                speech_regions,
            )

    anchors, runtime_reading_bridge_count, runtime_reading_bridge_debug = (
        _runtime_reading_weighted_bridge(
            chapter, anchors, runtime_reading_debug
        )
    )
    chapter["_runtime_reading_bridge_anchor_count"] = runtime_reading_bridge_count
    chapter["_runtime_reading_bridge_debug"] = runtime_reading_bridge_debug

    anchors = _prune_unreachable_leading_anchors(anchors, max_rate=16.0)
    chapter["_runtime_enriched_anchors"] = anchors
    chapter["_runtime_punctuation_pause_count"] = runtime_pause_count
    return anchors


def audio_position_for_light_novel(
    alignment: dict[str, Any], chapter_index: int, progress: float
) -> float | None:
    chapter = next(
        (
            row
            for row in alignment.get("chapters") or []
            if int(row.get("chapter_index") or 0) == int(chapter_index)
        ),
        None,
    )
    if not isinstance(chapter, dict):
        return None
    target = max(0.0, min(1.0, float(progress))) * max(1, int(chapter["normalized_length"]))
    anchors = _runtime_anchors_for_chapter(chapter)
    for left, right in pairwise(anchors):
        left_offset, right_offset = float(left["offset"]), float(right["offset"])
        if target <= right_offset:
            ratio = (target - left_offset) / max(1.0, right_offset - left_offset)
            left_time = float(left["time"])
            right_time = float(right["time"])
            if _anchor_segment_uses_activity_clock(chapter, right):
                return _time_for_activity_ratio(
                    chapter.get("speech_regions") or [],
                    left_time,
                    right_time,
                    ratio,
                )
            return left_time + max(0.0, min(1.0, ratio)) * (right_time - left_time)
    return float(chapter["end"])


def audio_position_for_light_novel_offset(
    alignment: dict[str, Any], chapter_index: int, character_offset: int
) -> float | None:
    """Resolve a normalized LN character offset on the linear anchor clock."""

    chapter = next(
        (
            row
            for row in alignment.get("chapters") or []
            if int(row.get("chapter_index") or 0) == int(chapter_index)
        ),
        None,
    )
    if not isinstance(chapter, dict):
        return None
    anchors = _runtime_anchors_for_chapter(chapter)
    if not anchors:
        return None
    target = max(0.0, min(float(character_offset), float(chapter.get("normalized_length") or 0)))
    exact_times = [
        float(row.get("time") or 0.0)
        for row in anchors
        if abs(float(row.get("offset") or 0.0) - target) < 1e-6
    ]
    if exact_times:
        return max(exact_times)
    for left, right in pairwise(anchors):
        left_offset = float(left.get("offset") or 0.0)
        right_offset = float(right.get("offset") or left_offset)
        if target <= right_offset:
            ratio = (target - left_offset) / max(1.0, right_offset - left_offset)
            left_time = float(left.get("time") or 0.0)
            right_time = float(right.get("time") or left_time)
            if _anchor_segment_uses_activity_clock(chapter, right):
                return _time_for_activity_ratio(
                    chapter.get("speech_regions") or [],
                    left_time,
                    right_time,
                    ratio,
                )
            return left_time + max(0.0, min(1.0, ratio)) * (right_time - left_time)
    return float(anchors[-1].get("time") or chapter.get("end") or 0.0)

def _runtime_audio_position_index(alignment: dict[str, Any]) -> dict[str, Any]:
    """Build the immutable live-playback lookup once per loaded alignment.

    paired_state polls several times per second. Re-pruning and linearly scanning
    tens of thousands of anchors on every poll is unnecessary and can starve the
    WebView scroll thread. The alignment payload object itself is cached by the
    audiobook service, so a private derived index is safe to reuse in memory.
    """

    cached = alignment.get("_runtime_audio_position_index")
    if isinstance(cached, dict) and cached.get("chapters"):
        return cached
    rows: list[dict[str, Any]] = []
    for chapter in alignment.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        # Re-apply proven leading holds and recover punctuation pause anchors
        # for old cache files before building the immutable live lookup index.
        anchors = _runtime_anchors_for_chapter(chapter)
        if not anchors:
            continue
        if not anchors:
            continue
        times = [float(row["time"]) for row in anchors]
        try:
            declared_start = float(chapter.get("start") or times[0])
            declared_end = float(chapter.get("end") or times[-1])
        except (TypeError, ValueError):
            declared_start, declared_end = times[0], times[-1]
        effective_start_candidates = [declared_start, times[0]]
        leading_debug = chapter.get("leading_prefix_debug")
        if isinstance(leading_debug, dict) and bool(leading_debug.get("chapter_marker")):
            try:
                effective_start_candidates.append(float(leading_debug.get("marker_start")))
            except (TypeError, ValueError):
                pass
        rows.append(
            {
                "chapter": chapter,
                "anchors": anchors,
                "times": times,
                # Spoken 第X幕 starts ownership of the new chapter even though
                # readable prose remains held at offset 0 until story_start.
                # This prevents a one-poll bounce to the previous chapter.
                "start": min(effective_start_candidates),
                "end": max(declared_end, times[-1]),
            }
        )
    rows.sort(
        key=lambda item: (
            float(item["start"]),
            int(item["chapter"].get("chapter_index") or 0),
        )
    )
    result = {"chapters": rows}
    alignment["_runtime_audio_position_index"] = result
    return result


def light_novel_position_for_audio(
    alignment: dict[str, Any], position: float
) -> dict[str, Any] | None:
    index = _runtime_audio_position_index(alignment)
    chapter_rows = list(index.get("chapters") or [])
    if not chapter_rows:
        return None
    value = float(position)
    matching = [
        item
        for item in chapter_rows
        if float(item["start"]) <= value < float(item["end"])
    ]
    if matching:
        # At an acoustic boundary the ranges may overlap by a few hundred ms.
        # Once the later chapter has started, never bounce back to the previous
        # chapter: that forces a full reader DOM replacement and visible flicker.
        selected = max(
            matching,
            key=lambda item: (
                float(item["start"]),
                int(item["chapter"].get("chapter_index") or 0),
            ),
        )
    else:
        selected = min(
            chapter_rows,
            key=lambda item: min(
                abs(value - float(item["start"])),
                abs(value - float(item["end"])),
            ),
        )
    chapter = selected["chapter"]
    anchors = selected["anchors"]
    anchor_times = selected["times"]

    leading_debug = chapter.get("leading_prefix_debug")
    fallback = (
        leading_debug.get("fallback")
        if isinstance(leading_debug, dict)
        and isinstance(leading_debug.get("fallback"), dict)
        else {}
    )
    fallback_mode = str(fallback.get("mode") or "")
    if (
        isinstance(leading_debug, dict)
        and bool(leading_debug.get("degraded"))
        and fallback_mode in {"safe_rejoin", "linear_chapter"}
    ):
        try:
            hold_until = float(fallback.get("rejoin_time") or chapter.get("end") or 0.0)
        except (TypeError, ValueError):
            hold_until = float(chapter.get("end") or 0.0)
        if float(position) < hold_until - 1e-6:
            length = max(1, int(chapter["normalized_length"]))
            start_time = float(chapter.get("start") or anchors[0].get("time") or 0.0)
            return {
                "chapter_index": int(chapter["chapter_index"]),
                "chapter_progress": 0.0,
                "chapter_char_offset": 0,
                "chapter_char_offset_exact": 0.0,
                "chapter_char_count": length,
                "anchor_window": {
                    "left_time": start_time,
                    "left_offset": 0.0,
                    "right_time": hold_until,
                    "right_offset": 0.0,
                    "path": [
                        {"time": start_time, "offset": 0.0},
                        {"time": hold_until, "offset": 0.0},
                    ],
                    "degraded_hold": True,
                },
            }

    target_offset = 0.0
    left_anchor = anchors[0]
    right_anchor = anchors[0]
    speech_regions = list(chapter.get("speech_regions") or [])
    if len(anchors) == 1:
        anchor_index = 0
        target_offset = float(anchors[0]["offset"])
    elif value > anchor_times[-1]:
        target_offset = float(chapter["normalized_length"])
        anchor_index = max(0, len(anchors) - 2)
        left_anchor, right_anchor = anchors[-2], anchors[-1]
    else:
        right_index = max(1, bisect.bisect_left(anchor_times, value))
        right_index = min(len(anchors) - 1, right_index)
        anchor_index = right_index - 1
        left_anchor, right_anchor = anchors[anchor_index], anchors[right_index]
        left_time = float(left_anchor["time"])
        right_time = float(right_anchor["time"])
        if abs(value - right_time) < 1e-6:
            target_offset = float(right_anchor["offset"])
        else:
            ratio = (
                _activity_ratio(speech_regions, left_time, right_time, value)
                if _anchor_segment_uses_activity_clock(chapter, right_anchor)
                else (value - left_time) / max(0.02, right_time - left_time)
            )
            ratio = max(0.0, min(1.0, ratio))
            target_offset = float(left_anchor["offset"]) + ratio * (
                float(right_anchor["offset"]) - float(left_anchor["offset"])
            )

    anchor_path: list[dict[str, Any]] = []
    horizon = float(position) + 2.5
    for row in anchors[anchor_index:]:
        if not isinstance(row, dict):
            continue
        try:
            path_time = float(row["time"])
            path_offset = float(row["offset"])
        except (KeyError, TypeError, ValueError):
            continue
        path_row: dict[str, Any] = {"time": path_time, "offset": path_offset}
        if bool(row.get("wall_clock_from_previous")):
            path_row["wall_clock_from_previous"] = True
        anchor_path.append(path_row)
        if len(anchor_path) >= 2 and path_time >= horizon:
            break
        if len(anchor_path) >= 64:
            break

    length = max(1, int(chapter["normalized_length"]))
    anchor_window: dict[str, Any] = {
        "left_time": float(left_anchor["time"]),
        "left_offset": float(left_anchor["offset"]),
        "right_time": float(right_anchor["time"]),
        "right_offset": float(right_anchor["offset"]),
        "path": anchor_path,
    }
    stored_pause_count = int(chapter.get("punctuation_pause_count") or 0)
    runtime_pause_count = int(chapter.get("_runtime_punctuation_pause_count") or 0)
    if stored_pause_count > 0:
        anchor_window["stored_punctuation_pause_count"] = stored_pause_count
    if runtime_pause_count > 0:
        anchor_window["runtime_punctuation_pause_count"] = runtime_pause_count
    if _chapter_uses_activity_clock(chapter):
        anchor_window["activity_clock"] = True
    runtime_reading_count = int(chapter.get("_runtime_reading_hint_anchor_count") or 0)
    if runtime_reading_count > 0:
        anchor_window["runtime_reading_hint_anchor_count"] = runtime_reading_count
        runtime_reading_debug = chapter.get("_runtime_reading_hint_debug")
        if isinstance(runtime_reading_debug, dict):
            anchor_window["runtime_reading_hint_mode"] = str(
                runtime_reading_debug.get("mode") or ""
            )
            anchor_window["runtime_reading_verified_through_offset"] = int(
                runtime_reading_debug.get("verified_through_offset") or 0
            )
    activity_right_time = (
        float(anchor_path[-1]["time"])
        if anchor_path
        else float(right_anchor["time"])
    )
    window_activity = _clip_activity_regions(
        speech_regions,
        float(left_anchor["time"]),
        max(float(right_anchor["time"]), activity_right_time),
    )
    if window_activity:
        anchor_window["activity"] = window_activity
    return {
        "chapter_index": int(chapter["chapter_index"]),
        "chapter_progress": max(0.0, min(1.0, target_offset / length)),
        "chapter_char_offset": int(round(target_offset)),
        "chapter_char_offset_exact": round(target_offset, 4),
        "chapter_char_count": length,
        "anchor_window": anchor_window,
    }
