from __future__ import annotations

import bisect
import unicodedata
from typing import Any, Iterable


_IGNORED_CATEGORIES = {"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Zl", "Zp", "Zs"}


def normalize_reading_text(value: str) -> str:
    """Normalize LN/STT text while preserving every spoken letter and digit."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) not in _IGNORED_CATEGORIES
    )


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


def _anchor_chain(novel: str, transcript: str, *, size: int) -> list[tuple[int, int]]:
    if len(novel) < size or len(transcript) < size:
        return []
    transcript_index: dict[str, list[int]] = {}
    for index in range(len(transcript) - size + 1):
        key = transcript[index : index + size]
        rows = transcript_index.setdefault(key, [])
        if len(rows) <= 4:
            rows.append(index)

    candidates: list[tuple[int, int]] = []
    for novel_index in range(len(novel) - size + 1):
        matches = transcript_index.get(novel[novel_index : novel_index + size]) or []
        if 0 < len(matches) <= 4:
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

    # Dense overlapping n-grams describe the same spoken word. Keep a compact
    # clock that is still fine-grained enough for word highlighting.
    compact: list[tuple[int, int]] = []
    for novel_index, transcript_index_ in chain:
        if compact and (
            novel_index <= compact[-1][0]
            or transcript_index_ <= compact[-1][1]
        ):
            continue
        if compact and novel_index - compact[-1][0] < 3:
            continue
        compact.append((novel_index, transcript_index_))
    return compact


def align_light_novel_to_transcript(
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    duration: float,
    model: str,
) -> dict[str, Any]:
    transcript, transcript_times = _transcript_clock(segments)
    chapter_ranges: list[dict[str, Any]] = []
    novel_parts: list[str] = []
    cursor = 0
    for chapter in chapters:
        normalized = normalize_reading_text(str(chapter.get("text") or ""))
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
            }
        )
    novel = "".join(novel_parts)
    if len(transcript) < 40 or len(novel) < 40:
        raise ValueError("STT produced too little Japanese text for alignment")

    anchor_size = 7
    anchors = _anchor_chain(novel, transcript, size=anchor_size)
    if len(anchors) < 8:
        anchor_size = 5
        anchors = _anchor_chain(novel, transcript, size=anchor_size)
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
        clock = [
            {
                "offset": int(offset),
                "time": round(float(transcript_times[min(transcript_index_, len(transcript_times) - 1)]), 3),
            }
            for offset, transcript_index_ in local
        ]
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
                "confidence": round(min(1.0, span / max(1, chapter["length"])), 4),
            }
        )
    if not aligned:
        raise ValueError("STT text did not match any readable LN chapter")

    for index, chapter in enumerate(aligned):
        if index:
            previous = aligned[index - 1]
            boundary = (
                float(previous["last_anchor_time"])
                + float(chapter["first_anchor_time"])
            ) / 2
            previous["end"] = round(max(float(previous["start"]) + 0.25, boundary), 3)
            chapter["start"] = round(max(0.0, boundary), 3)
        else:
            chapter["start"] = round(float(chapter["estimated_start"]), 3)
    aligned[-1]["end"] = round(
        min(max(float(aligned[-1]["start"]) + 0.25, float(duration)), float(aligned[-1]["estimated_end"])),
        3,
    )
    for chapter in aligned:
        chapter.setdefault("end", round(float(chapter["estimated_end"]), 3))
        chapter["anchors"] = [
            {"offset": 0, "time": chapter["start"]},
            *chapter["anchors"],
            {"offset": chapter["normalized_length"], "time": chapter["end"]},
        ]
        for key in ("first_anchor_time", "last_anchor_time", "estimated_start", "estimated_end"):
            chapter.pop(key, None)

    return {
        "schema": "reading-audio-v1",
        "model": str(model),
        "duration": round(float(duration), 3),
        "transcript_characters": len(transcript),
        "novel_characters": len(novel),
        "anchor_count": len(anchors),
        "chapters": aligned,
        "confidence": round(sum(float(row["confidence"]) for row in aligned) / len(aligned), 4),
    }


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
    anchors = list(chapter.get("anchors") or [])
    for left, right in zip(anchors, anchors[1:]):
        left_offset, right_offset = float(left["offset"]), float(right["offset"])
        if target <= right_offset:
            ratio = (target - left_offset) / max(1.0, right_offset - left_offset)
            return float(left["time"]) + ratio * (float(right["time"]) - float(left["time"]))
    return float(chapter["end"])


def light_novel_position_for_audio(
    alignment: dict[str, Any], position: float
) -> dict[str, Any] | None:
    chapters = [row for row in alignment.get("chapters") or [] if isinstance(row, dict)]
    if not chapters:
        return None
    chapter = next(
        (
            row
            for row in chapters
            if float(row["start"]) <= float(position) < float(row["end"])
        ),
        min(
            chapters,
            key=lambda row: min(
                abs(float(position) - float(row["start"])),
                abs(float(position) - float(row["end"])),
            ),
        ),
    )
    anchors = list(chapter.get("anchors") or [])
    target_offset = 0.0
    for left, right in zip(anchors, anchors[1:]):
        if float(position) <= float(right["time"]):
            ratio = (float(position) - float(left["time"])) / max(0.02, float(right["time"]) - float(left["time"]))
            target_offset = float(left["offset"]) + max(0.0, min(1.0, ratio)) * (float(right["offset"]) - float(left["offset"]))
            break
    else:
        target_offset = float(chapter["normalized_length"])
    length = max(1, int(chapter["normalized_length"]))
    return {
        "chapter_index": int(chapter["chapter_index"]),
        "chapter_progress": max(0.0, min(1.0, target_offset / length)),
        "chapter_char_offset": int(round(target_offset)),
        "chapter_char_count": length,
    }
