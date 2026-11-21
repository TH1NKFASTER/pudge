"""Dot runs in Japanese manga OCR text, cleaned when the text is served.

MangaOCR (Mokuro) reads an ellipsis as separate dots ("．．．", "・・・", "。。。", "...", "‥").
The text the reader shows, the iPhone packages and the look-up parse get "…" instead ("……"
from four dots on); a text box of only dots (two or more) is "…". A single dot stays ("ジョン
・スミス", a box of only "。"). Runs are found within one line of the box as Mokuro read it:
dots at the end of one line and the start of the next are never joined.

The stored OCR (cache and artifact) stays as it was read: no re-OCR is needed and the rule can
change later. The boxes of the reader follow the text: per-character (or per-word) boxes of
the dots are merged into one, the line boxes get the same text; a box whose per-character
boxes do not spell its text is served unchanged.
"""

from __future__ import annotations

import re
from typing import Any

# Dots MangaOCR writes for an ellipsis, with how many dots each one stands for.
_DOT_UNITS = {
    "．": 1,
    ".": 1,
    "・": 1,
    "･": 1,
    "。": 1,
    "｡": 1,
    "·": 1,
    "‧": 1,
    "‥": 2,
    "…": 3,
    "⋯": 3,
}
_DOTS = frozenset(_DOT_UNITS)
_DOT_RUN = re.compile("[" + re.escape("".join(_DOT_UNITS)) + "]+")
_FULL_STOPS = frozenset("。｡")
_ELLIPSIS = "…"


def _units(run: str) -> int:
    return sum(_DOT_UNITS[character] for character in run)


def _run_replacement(run: str) -> str:
    """A run of dots as it is served: '…' / '……' (from four dots); a single dot stays."""
    if set(run) == {_ELLIPSIS}:
        return run  # already ellipses
    if (
        len(run) > 1
        and run[-1] in _FULL_STOPS
        and any(character not in _FULL_STOPS for character in run[:-1])
    ):
        # "…。" / "・・・。": the full stop after the ellipsis ends the sentence.
        return _run_replacement(run[:-1]) + run[-1]
    if _units(run) < 2:
        return run
    return _ELLIPSIS * 2 if _units(run) >= 4 else _ELLIPSIS


def _compact(text: str) -> str:
    return "".join(character for character in str(text or "") if not character.isspace())


def _only_dots(text: str) -> bool:
    compact = _compact(text)
    return bool(compact) and all(character in _DOTS for character in compact) and _units(compact) >= 2


def clean_dot_runs(text: str, *, whole_box: bool = True) -> str:
    """One line's text with its dot runs as ellipses; with `whole_box` a box of only dots is '…'."""
    value = str(text or "")
    if whole_box and _only_dots(value):
        return _ELLIPSIS
    return _DOT_RUN.sub(lambda match: _run_replacement(match.group(0)), value)


def _split(count: int, parts: int) -> list[int]:
    """count items in `parts` contiguous groups as even as possible (earlier ones larger)."""
    base, extra = divmod(count, parts)
    return [base + (1 if index < extra else 0) for index in range(parts)]


def _clean_chunk(text: str, first_index: int) -> tuple[str, list[list[int]]]:
    """One line's cleaned text and, for each of its non-space characters, the non-space
    characters of the box (indices from `first_index` on) it was made from."""
    pieces: list[str] = []
    sources: list[list[int]] = []
    index = first_index
    position = 0

    def keep(chunk: str) -> None:
        nonlocal index
        pieces.append(chunk)
        for character in chunk:
            if not character.isspace():
                sources.append([index])
                index += 1

    for match in _DOT_RUN.finditer(text):
        keep(text[position : match.start()])
        run = match.group(0)
        replacement = _run_replacement(run)
        indices = list(range(index, index + len(run)))
        index += len(run)
        pieces.append(replacement)
        if replacement == run:
            sources.extend([item] for item in indices)
        else:
            # The full stop kept after an ellipsis ("・・・。" -> "…。") is the run's last dot.
            tail = [[indices.pop()]] if replacement[-1] in _FULL_STOPS and run[-1] == replacement[-1] else []
            body = replacement[:-1] if tail else replacement
            start = 0
            for size in _split(len(indices), len(body)):
                sources.append(indices[start : start + size])
                start += size
            sources.extend(tail)
        position = match.end()
    keep(text[position:])
    return "".join(pieces), sources


def _line_lengths(region: dict[str, Any], text: str) -> list[int] | None:
    """Non-space characters per line of the box (Mokuro's lines) when its line boxes spell its
    text; None otherwise (the box is then one line)."""
    provenance = region.get("provenance")
    lines = provenance.get("line_boxes") if isinstance(provenance, dict) else None
    if not isinstance(lines, list) or not lines:
        return None
    if not all(isinstance(line, dict) and isinstance(line.get("text"), str) for line in lines):
        return None
    if "".join(_compact(line["text"]) for line in lines) != _compact(text):
        return None
    return [len(_compact(line["text"])) for line in lines]


def _chunks(text: str, lengths: list[int]) -> list[str]:
    """The text cut at its line ends (counted in non-space characters); a space goes with
    the line it follows."""
    chunks: list[str] = []
    current: list[str] = []
    line, seen = 0, 0
    for character in text:
        if not character.isspace():
            while line < len(lengths) - 1 and seen >= lengths[line]:
                chunks.append("".join(current))
                current, line, seen = [], line + 1, 0
            seen += 1
        current.append(character)
    chunks.append("".join(current))
    chunks.extend("" for _ in range(len(lengths) - len(chunks)))
    return chunks


def _clean_text(text: str, lengths: list[int] | None) -> tuple[str, list[list[int]], list[str] | None]:
    """The box's cleaned text, the sources of its non-space characters and each line's
    cleaned text (None without lines)."""
    if _only_dots(text):
        return _ELLIPSIS, [list(range(len(_compact(text))))], None
    if not lengths:
        cleaned, sources = _clean_chunk(text, 0)
        return cleaned, sources, None
    pieces: list[str] = []
    sources: list[list[int]] = []
    first = 0
    for chunk in _chunks(text, lengths):
        cleaned, chunk_sources = _clean_chunk(chunk, first)
        first += len(_compact(chunk))
        pieces.append(cleaned)
        sources.extend(chunk_sources)
    return "".join(pieces), sources, pieces


def _union(boxes: list[dict[str, Any]]) -> dict[str, Any]:
    left = min(float(box.get("x") or 0) for box in boxes)
    bottom = min(float(box.get("y") or 0) for box in boxes)
    right = max(float(box.get("x") or 0) + float(box.get("width") or 0) for box in boxes)
    top = max(float(box.get("y") or 0) + float(box.get("height") or 0) for box in boxes)
    return {
        "x": round(left, 6),
        "y": round(bottom, 6),
        "width": round(max(0.0, right - left), 6),
        "height": round(max(0.0, top - bottom), 6),
    }


def _follow_segments(
    segments: list[Any], text: str, cleaned: str, sources: list[list[int]]
) -> list[dict[str, Any]] | None:
    """The boxes for the cleaned text: boxes whose characters make one served character are
    merged (three dot boxes -> one "…" box; a word box keeps its place, with its new text).
    None when the boxes do not spell the text (they cannot follow it)."""
    if not all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in segments):
        return None
    if "".join(_compact(segment["text"]) for segment in segments) != _compact(text):
        return None
    owner: list[int] = []
    for number, segment in enumerate(segments):
        owner.extend([number] * len(_compact(segment["text"])))
    # Boxes joined by a served character are one box (they are next to each other).
    head = list(range(len(segments)))
    for indices in sources:
        first, last = owner[indices[0]], owner[indices[-1]]
        for number in range(first + 1, last + 1):
            old = head[number]
            head = [head[first] if value == old else value for value in head]
    out = [character for character in cleaned if not character.isspace()]
    texts: dict[int, list[str]] = {}
    for character, indices in zip(out, sources):
        texts.setdefault(head[owner[indices[0]]], []).append(character)
    result: list[dict[str, Any]] = []
    for group in sorted(set(head)):
        members = [segments[number] for number in range(len(segments)) if head[number] == group]
        if group not in texts:
            continue  # a box without characters
        merged = members[0] if len(members) == 1 else {**members[0], **_union(members)}
        result.append({**merged, "text": "".join(texts[group])})
    return result


def clean_region_dots(region: dict[str, Any]) -> dict[str, Any]:
    """A served OCR region with its dot runs cleaned (a new dict; the input is untouched)."""
    if not isinstance(region, dict):
        return region
    text = str(region.get("text") or "")
    if not any(character in _DOTS for character in text):
        return region
    lengths = _line_lengths(region, text)
    cleaned, sources, cleaned_lines = _clean_text(text, lengths)
    provenance = region.get("provenance") if isinstance(region.get("provenance"), dict) else None
    lines = provenance.get("line_boxes") if provenance else None
    new_lines: list[Any] | None = None
    if isinstance(lines, list) and lines:
        if _only_dots(text) and lengths is not None:
            # A box of only dots: one "…" line over the lines' dots.
            new_lines = [{**lines[0], **_union(lines), "text": _ELLIPSIS}]
        elif cleaned_lines is not None:
            new_lines = [
                line if _compact(value) == _compact(line["text"]) else {**line, "text": value.strip()}
                for line, value in zip(lines, cleaned_lines)
            ]
        else:
            new_lines = [
                {**line, "text": clean_dot_runs(line["text"], whole_box=False)}
                if isinstance(line, dict) and isinstance(line.get("text"), str)
                else line
                for line in lines
            ]
    lines_changed = new_lines is not None and new_lines != lines
    if cleaned == text and not lines_changed:
        return region
    result = {**region}
    if cleaned != text:
        segments = region.get("segments")
        if isinstance(segments, list) and segments:
            followed = _follow_segments(segments, text, cleaned, sources)
            if followed is None:
                return region  # boxes that cannot follow: the box is served as it was read
            result["segments"] = followed
        result["text"] = cleaned
        result.setdefault("raw_text", text)
    if lines_changed and provenance is not None:
        result["provenance"] = {**provenance, "line_boxes": new_lines}
    return result


# ------------------------------------------------------------------- look-up parses
def offset_map(source: str, target: str) -> list[int] | None:
    """For each offset 0..len(source) of a text, its offset in the cleaned copy `target` (dot
    runs as ellipses); None when the two are not such a pair."""
    mapping = [0] * (len(source) + 1)
    i = j = 0
    while i < len(source) or j < len(target):
        if i < len(source) and j < len(target) and source[i] not in _DOTS and target[j] not in _DOTS:
            if source[i] != target[j]:
                return None
            mapping[i] = j
            i, j = i + 1, j + 1
            continue
        if i >= len(source) or j >= len(target) or source[i] not in _DOTS or target[j] not in _DOTS:
            return None
        # A block of dots in both: their ends meet; a full stop ending both meets its own.
        end_i, end_j = i, j
        while end_i < len(source) and source[end_i] in _DOTS:
            end_i += 1
        while end_j < len(target) and target[end_j] in _DOTS:
            end_j += 1
        blocks = [(i, end_i, j, end_j)]
        if (
            end_i - i > 1
            and end_j - j > 1
            and source[end_i - 1] in _FULL_STOPS
            and source[end_i - 1] == target[end_j - 1]
        ):
            blocks = [(i, end_i - 1, j, end_j - 1), (end_i - 1, end_i, end_j - 1, end_j)]
        for start_i, stop_i, start_j, stop_j in blocks:
            for offset in range(start_i, stop_i):
                share = (offset - start_i) / max(1, stop_i - start_i)
                mapping[offset] = start_j + int(share * (stop_j - start_j))
        i, j = end_i, end_j
    mapping[len(source)] = len(target)
    return mapping


def remap_parse(parsed: dict[str, Any], source: str, target: str) -> dict[str, Any] | None:
    """A cached Jiten parse of `source` (a text box as recognized) as the parse of its served
    text `target`: the same words at the served text's offsets. None when the parse or the
    texts do not line up (the served text is then parsed itself)."""
    if not isinstance(parsed, dict) or str(parsed.get("parseProvider") or "jiten") != "jiten":
        return None
    paragraphs = parsed.get("paragraphs")
    tokens = parsed.get("tokens")
    targets = [part.strip() for part in str(target).split("\n") if part.strip()]
    if not isinstance(paragraphs, list) or not isinstance(tokens, list):
        return None
    if len(paragraphs) != len(targets) or len(tokens) != len(paragraphs):
        return None
    new_tokens: list[list[dict[str, Any]]] = []
    for paragraph, cleaned, row in zip(paragraphs, targets, tokens):
        mapping = offset_map(str(paragraph), cleaned)
        if mapping is None or not isinstance(row, list):
            return None
        converted: list[dict[str, Any]] = []
        for token in row:
            if not isinstance(token, dict):
                return None
            try:
                start, end = int(token["start"]), int(token["end"])
            except (KeyError, TypeError, ValueError):
                return None
            if not 0 <= start <= end <= len(str(paragraph)):
                return None
            new_start, new_end = mapping[start], mapping[end]
            if new_end <= new_start:
                continue  # dots only, now part of an ellipsis of the token before
            item = {**token, "start": new_start, "end": new_end}
            if isinstance(token.get("surface"), str):
                item["surface"] = cleaned[new_start:new_end]
            converted.append(item)
        new_tokens.append(converted)
    return {**parsed, "paragraphs": targets, "tokens": new_tokens}
