from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import unicodedata
from bisect import bisect_left
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..subtitle_formats import parse_srt


BENCHMARK_SCHEMA = "pudge-subtitle-benchmark-v1"
ORACLE_REPORT_SCHEMA = "pudge-subtitle-oracle-report-v1"
DEFAULT_NGRAM = 7

_ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
_HTML_TAG_RE = re.compile(r"<[^<>]*>")


@dataclass(slots=True, frozen=True)
class TimedCharacter:
    char: str
    time: float
    cue_index: int


@dataclass(slots=True, frozen=True)
class TextAnchor:
    candidate_pos: int
    oracle_pos: int
    candidate_time: float
    oracle_time: float
    candidate_cue: int
    oracle_cue: int

    @property
    def error_seconds(self) -> float:
        return self.candidate_time - self.oracle_time


@dataclass(slots=True, frozen=True)
class CueMatch:
    candidate_cue: int
    oracle_cue: int
    votes: int
    candidate_start: float
    candidate_end: float
    oracle_start: float
    oracle_end: float

    @property
    def start_error_seconds(self) -> float:
        return self.candidate_start - self.oracle_start

    @property
    def end_error_seconds(self) -> float:
        return self.candidate_end - self.oracle_end


@dataclass(slots=True)
class SubtitleOracleReport:
    schema: str
    evaluable: bool
    same_episode: bool
    good_alignment: bool
    reason: str
    ngram: int
    candidate_characters: int
    oracle_characters: int
    anchor_count: int
    candidate_coverage: float
    oracle_coverage: float
    content_match_score: float
    signed_median_error_seconds: float | None
    median_abs_error_seconds: float | None
    p90_abs_error_seconds: float | None
    p99_abs_error_seconds: float | None
    max_abs_error_seconds: float | None
    within_025_ratio: float | None
    within_050_ratio: float | None
    within_100_ratio: float | None
    within_200_ratio: float | None
    cue_match_count: int
    cue_start_median_abs_error_seconds: float | None
    cue_start_p90_abs_error_seconds: float | None
    cue_end_median_abs_error_seconds: float | None
    cue_end_p90_abs_error_seconds: float | None
    longest_bad_1s_span_seconds: float | None
    longest_bad_2s_span_seconds: float | None
    regions: dict[str, dict[str, object]]
    worst_spans: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_benchmark_text(text: str) -> str:
    """Normalize subtitle dialogue for oracle matching, not for playback.

    Timing evaluation must survive ASS styling, punctuation differences and cue
    re-segmentation while still rejecting a different episode.  Keep Japanese,
    letters and digits; discard layout/punctuation-only noise.
    """

    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\\N", "").replace("\\n", "")
    value = _ASS_TAG_RE.sub("", value)
    value = _HTML_TAG_RE.sub("", value)
    chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category and category[0] in {"L", "N"}:
            chars.append(char.casefold())
    return "".join(chars)


def _timed_characters(cues: Sequence[tuple[float, float, str]]) -> list[TimedCharacter]:
    result: list[TimedCharacter] = []
    for cue_index, (start, end, text) in enumerate(cues):
        normalized = normalize_benchmark_text(text)
        if not normalized:
            continue
        duration = max(0.001, float(end) - float(start))
        length = len(normalized)
        for index, char in enumerate(normalized):
            # Character-centre interpolation makes this robust to a 1->2 cue
            # split while still measuring local timing drift inside long cues.
            fraction = (index + 0.5) / length
            result.append(
                TimedCharacter(
                    char=char,
                    time=float(start) + duration * fraction,
                    cue_index=cue_index,
                )
            )
    return result


def _unique_ngram_positions(chars: Sequence[TimedCharacter], ngram: int) -> dict[str, int]:
    if ngram <= 0 or len(chars) < ngram:
        return {}
    text = "".join(item.char for item in chars)
    positions: dict[str, int] = {}
    repeated: set[str] = set()
    for position in range(0, len(text) - ngram + 1):
        key = text[position : position + ngram]
        if key in repeated:
            continue
        if key in positions:
            positions.pop(key, None)
            repeated.add(key)
        else:
            positions[key] = position
    return positions


def _longest_monotonic_anchor_chain(
    pairs: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Longest strictly-monotonic chain by oracle position in O(n log n)."""

    if not pairs:
        return []
    ordered = sorted(set(pairs), key=lambda item: (item[0], item[1]))
    tails: list[int] = []
    tail_indexes: list[int] = []
    previous = [-1] * len(ordered)

    for index, (_candidate_pos, oracle_pos) in enumerate(ordered):
        slot = bisect_left(tails, oracle_pos)
        if slot > 0:
            previous[index] = tail_indexes[slot - 1]
        if slot == len(tails):
            tails.append(oracle_pos)
            tail_indexes.append(index)
        else:
            tails[slot] = oracle_pos
            tail_indexes[slot] = index

    current = tail_indexes[-1]
    chain: list[tuple[int, int]] = []
    while current >= 0:
        chain.append(ordered[current])
        current = previous[current]
    chain.reverse()
    return chain


def build_text_anchors(
    candidate_cues: Sequence[tuple[float, float, str]],
    oracle_cues: Sequence[tuple[float, float, str]],
    *,
    ngram: int = DEFAULT_NGRAM,
) -> tuple[list[TextAnchor], dict[str, float | int]]:
    candidate_chars = _timed_characters(candidate_cues)
    oracle_chars = _timed_characters(oracle_cues)
    candidate_positions = _unique_ngram_positions(candidate_chars, ngram)
    oracle_positions = _unique_ngram_positions(oracle_chars, ngram)

    common_pairs = [
        (candidate_pos, oracle_positions[key])
        for key, candidate_pos in candidate_positions.items()
        if key in oracle_positions
    ]
    chain = _longest_monotonic_anchor_chain(common_pairs)

    anchors: list[TextAnchor] = []
    candidate_covered: set[int] = set()
    oracle_covered: set[int] = set()
    centre = ngram // 2
    for candidate_pos, oracle_pos in chain:
        candidate_index = min(len(candidate_chars) - 1, candidate_pos + centre)
        oracle_index = min(len(oracle_chars) - 1, oracle_pos + centre)
        candidate_item = candidate_chars[candidate_index]
        oracle_item = oracle_chars[oracle_index]
        anchors.append(
            TextAnchor(
                candidate_pos=candidate_pos,
                oracle_pos=oracle_pos,
                candidate_time=candidate_item.time,
                oracle_time=oracle_item.time,
                candidate_cue=candidate_item.cue_index,
                oracle_cue=oracle_item.cue_index,
            )
        )
        candidate_covered.update(range(candidate_pos, min(len(candidate_chars), candidate_pos + ngram)))
        oracle_covered.update(range(oracle_pos, min(len(oracle_chars), oracle_pos + ngram)))

    candidate_coverage = len(candidate_covered) / max(1, len(candidate_chars))
    oracle_coverage = len(oracle_covered) / max(1, len(oracle_chars))
    if candidate_coverage <= 0.0 or oracle_coverage <= 0.0:
        content_match_score = 0.0
    else:
        content_match_score = 2.0 / ((1.0 / candidate_coverage) + (1.0 / oracle_coverage))
    return anchors, {
        "candidate_characters": len(candidate_chars),
        "oracle_characters": len(oracle_chars),
        "candidate_coverage": candidate_coverage,
        "oracle_coverage": oracle_coverage,
        "content_match_score": content_match_score,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _rounded(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _ratio_within(values: Sequence[float], seconds: float) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if abs(value) <= seconds) / len(values)


def _cue_matches(
    anchors: Sequence[TextAnchor],
    candidate_cues: Sequence[tuple[float, float, str]],
    oracle_cues: Sequence[tuple[float, float, str]],
) -> list[CueMatch]:
    votes: dict[tuple[int, int], int] = {}
    for anchor in anchors:
        key = (anchor.candidate_cue, anchor.oracle_cue)
        votes[key] = votes.get(key, 0) + 1

    by_candidate: dict[int, list[tuple[int, int]]] = {}
    for (candidate_cue, oracle_cue), count in votes.items():
        by_candidate.setdefault(candidate_cue, []).append((oracle_cue, count))

    matches: list[CueMatch] = []
    for candidate_cue, options in sorted(by_candidate.items()):
        options.sort(key=lambda item: (-item[1], item[0]))
        oracle_cue, count = options[0]
        # Require more than a single accidental n-gram vote for cue-level start/end
        # metrics. Character-level metrics still retain all monotonic anchors.
        if count < 2:
            continue
        if candidate_cue >= len(candidate_cues) or oracle_cue >= len(oracle_cues):
            continue
        candidate_start, candidate_end, _ = candidate_cues[candidate_cue]
        oracle_start, oracle_end, _ = oracle_cues[oracle_cue]
        matches.append(
            CueMatch(
                candidate_cue=candidate_cue,
                oracle_cue=oracle_cue,
                votes=count,
                candidate_start=float(candidate_start),
                candidate_end=float(candidate_end),
                oracle_start=float(oracle_start),
                oracle_end=float(oracle_end),
            )
        )
    return matches


def _long_bad_span(
    anchors: Sequence[TextAnchor],
    *,
    threshold: float,
    max_anchor_gap_seconds: float = 30.0,
) -> tuple[float, dict[str, object] | None]:
    if not anchors:
        return 0.0, None
    ordered = sorted(anchors, key=lambda item: item.oracle_time)
    best_duration = 0.0
    best: dict[str, object] | None = None
    start: TextAnchor | None = None
    previous: TextAnchor | None = None
    peak = 0.0
    count = 0

    def finish() -> None:
        nonlocal best_duration, best, start, previous, peak, count
        if start is None or previous is None:
            return
        duration = max(0.0, previous.oracle_time - start.oracle_time)
        if duration > best_duration or (
            duration == best_duration and count > int((best or {}).get("anchors", 0))
        ):
            best_duration = duration
            best = {
                "threshold_seconds": threshold,
                "oracle_start_seconds": round(start.oracle_time, 3),
                "oracle_end_seconds": round(previous.oracle_time, 3),
                "duration_seconds": round(duration, 3),
                "peak_abs_error_seconds": round(peak, 3),
                "anchors": count,
            }

    for anchor in ordered:
        bad = abs(anchor.error_seconds) > threshold
        contiguous = previous is None or anchor.oracle_time - previous.oracle_time <= max_anchor_gap_seconds
        if bad and (start is None or contiguous):
            if start is None:
                start = anchor
                count = 0
                peak = 0.0
            previous = anchor
            count += 1
            peak = max(peak, abs(anchor.error_seconds))
            continue
        if start is not None:
            finish()
            start = None
            previous = None
            count = 0
            peak = 0.0
        if bad:
            start = previous = anchor
            count = 1
            peak = abs(anchor.error_seconds)
        else:
            previous = anchor
    if start is not None:
        finish()
    return best_duration, best


def _region_report(anchors: Sequence[TextAnchor]) -> dict[str, object]:
    errors = [anchor.error_seconds for anchor in anchors]
    absolute = [abs(value) for value in errors]
    return {
        "anchors": len(anchors),
        "signed_median_error_seconds": _rounded(statistics.median(errors) if errors else None),
        "median_abs_error_seconds": _rounded(statistics.median(absolute) if absolute else None),
        "p90_abs_error_seconds": _rounded(_percentile(absolute, 0.90)),
        "p99_abs_error_seconds": _rounded(_percentile(absolute, 0.99)),
        "within_1s_ratio": _rounded(_ratio_within(errors, 1.0)),
        "within_2s_ratio": _rounded(_ratio_within(errors, 2.0)),
    }


def _default_regions(anchors: Sequence[TextAnchor]) -> dict[str, list[TextAnchor]]:
    if not anchors:
        return {}
    end = max(anchor.oracle_time for anchor in anchors)
    middle_start = end * 0.25
    middle_end = end * 0.75
    return {
        "first_120s": [anchor for anchor in anchors if anchor.oracle_time <= 120.0],
        "first_300s": [anchor for anchor in anchors if anchor.oracle_time <= 300.0],
        "middle": [anchor for anchor in anchors if middle_start <= anchor.oracle_time <= middle_end],
        "last_300s": [anchor for anchor in anchors if anchor.oracle_time >= max(0.0, end - 300.0)],
    }


def _custom_region_anchors(
    anchors: Sequence[TextAnchor],
    regions: dict[str, tuple[float, float]] | None,
) -> dict[str, list[TextAnchor]]:
    result = _default_regions(anchors)
    for name, bounds in (regions or {}).items():
        start, end = float(bounds[0]), float(bounds[1])
        if end < start:
            start, end = end, start
        result[str(name)] = [anchor for anchor in anchors if start <= anchor.oracle_time <= end]
    return result


def evaluate_subtitle_cues(
    candidate_cues: Sequence[tuple[float, float, str]],
    oracle_cues: Sequence[tuple[float, float, str]],
    *,
    ngram: int = DEFAULT_NGRAM,
    regions: dict[str, tuple[float, float]] | None = None,
) -> SubtitleOracleReport:
    anchors, coverage = build_text_anchors(candidate_cues, oracle_cues, ngram=ngram)
    errors = [anchor.error_seconds for anchor in anchors]
    absolute = [abs(value) for value in errors]
    cue_matches = _cue_matches(anchors, candidate_cues, oracle_cues)
    cue_start_abs = [abs(match.start_error_seconds) for match in cue_matches]
    cue_end_abs = [abs(match.end_error_seconds) for match in cue_matches]

    candidate_coverage = float(coverage["candidate_coverage"])
    oracle_coverage = float(coverage["oracle_coverage"])
    content_match_score = float(coverage["content_match_score"])

    # This gate is deliberately content-only.  A catastrophically mis-timed but
    # otherwise correct subtitle must remain evaluable so the benchmark can expose it.
    same_episode = bool(
        len(anchors) >= 20
        and content_match_score >= 0.30
        and min(candidate_coverage, oracle_coverage) >= 0.20
    )
    evaluable = same_episode

    longest_bad_1s, span_1s = _long_bad_span(anchors, threshold=1.0)
    longest_bad_2s, span_2s = _long_bad_span(anchors, threshold=2.0)
    p90 = _percentile(absolute, 0.90)
    p99 = _percentile(absolute, 0.99)
    within_1 = _ratio_within(errors, 1.0)
    within_2 = _ratio_within(errors, 2.0)

    good_alignment = bool(
        evaluable
        and p90 is not None
        and p99 is not None
        and within_1 is not None
        and within_2 is not None
        and p90 <= 1.0
        and p99 <= 2.0
        and within_1 >= 0.95
        and within_2 >= 0.985
        and longest_bad_2s <= 15.0
    )
    if not candidate_cues:
        reason = "candidate_has_no_cues"
    elif not oracle_cues:
        reason = "oracle_has_no_cues"
    elif not same_episode:
        reason = "insufficient_text_match"
    elif good_alignment:
        reason = "good"
    elif longest_bad_2s > 15.0:
        reason = "catastrophic_region"
    elif p90 is not None and p90 > 1.0:
        reason = "p90_timing_error"
    else:
        reason = "timing_quality_failed"

    region_reports = {
        name: _region_report(rows) for name, rows in _custom_region_anchors(anchors, regions).items() if rows
    }
    worst_spans = [span for span in (span_2s, span_1s) if span is not None]

    return SubtitleOracleReport(
        schema=ORACLE_REPORT_SCHEMA,
        evaluable=evaluable,
        same_episode=same_episode,
        good_alignment=good_alignment,
        reason=reason,
        ngram=int(ngram),
        candidate_characters=int(coverage["candidate_characters"]),
        oracle_characters=int(coverage["oracle_characters"]),
        anchor_count=len(anchors),
        candidate_coverage=round(candidate_coverage, 4),
        oracle_coverage=round(oracle_coverage, 4),
        content_match_score=round(content_match_score, 4),
        signed_median_error_seconds=_rounded(statistics.median(errors) if errors else None),
        median_abs_error_seconds=_rounded(statistics.median(absolute) if absolute else None),
        p90_abs_error_seconds=_rounded(p90),
        p99_abs_error_seconds=_rounded(p99),
        max_abs_error_seconds=_rounded(max(absolute) if absolute else None),
        within_025_ratio=_rounded(_ratio_within(errors, 0.25)),
        within_050_ratio=_rounded(_ratio_within(errors, 0.50)),
        within_100_ratio=_rounded(within_1),
        within_200_ratio=_rounded(within_2),
        cue_match_count=len(cue_matches),
        cue_start_median_abs_error_seconds=_rounded(
            statistics.median(cue_start_abs) if cue_start_abs else None
        ),
        cue_start_p90_abs_error_seconds=_rounded(_percentile(cue_start_abs, 0.90)),
        cue_end_median_abs_error_seconds=_rounded(statistics.median(cue_end_abs) if cue_end_abs else None),
        cue_end_p90_abs_error_seconds=_rounded(_percentile(cue_end_abs, 0.90)),
        longest_bad_1s_span_seconds=round(longest_bad_1s, 3) if anchors else None,
        longest_bad_2s_span_seconds=round(longest_bad_2s, 3) if anchors else None,
        regions=region_reports,
        worst_spans=worst_spans,
    )


def evaluate_subtitle_files(
    candidate: Path,
    oracle: Path,
    *,
    ngram: int = DEFAULT_NGRAM,
    regions: dict[str, tuple[float, float]] | None = None,
) -> SubtitleOracleReport:
    return evaluate_subtitle_cues(
        parse_srt(Path(candidate)),
        parse_srt(Path(oracle)),
        ngram=ngram,
        regions=regions,
    )


def report_case_id(*, media_id: int | None, episode: int | None, release_name: str) -> str:
    raw = f"{media_id or 0}:{episode or 0}:{release_name}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _numeric(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            values.append(float(value))
    return values


def aggregate_oracle_reports(reports: Iterable[dict[str, Any] | SubtitleOracleReport]) -> dict[str, object]:
    rows = [row.as_dict() if isinstance(row, SubtitleOracleReport) else dict(row) for row in reports]
    evaluable = [row for row in rows if bool(row.get("evaluable"))]
    good = [row for row in evaluable if bool(row.get("good_alignment"))]
    p90_values = _numeric(evaluable, "p90_abs_error_seconds")
    p99_values = _numeric(evaluable, "p99_abs_error_seconds")
    coverage_values = _numeric(evaluable, "oracle_coverage")
    bad_span_values = _numeric(evaluable, "longest_bad_2s_span_seconds")
    return {
        "schema": BENCHMARK_SCHEMA,
        "total": len(rows),
        "evaluable": len(evaluable),
        "same_episode": sum(1 for row in rows if bool(row.get("same_episode"))),
        "good": len(good),
        "good_ratio": round(len(good) / max(1, len(evaluable)), 4),
        "unevaluable_ratio": round((len(rows) - len(evaluable)) / max(1, len(rows)), 4),
        "median_episode_p90_seconds": _rounded(statistics.median(p90_values) if p90_values else None),
        "p90_episode_p90_seconds": _rounded(_percentile(p90_values, 0.90)),
        "median_episode_p99_seconds": _rounded(statistics.median(p99_values) if p99_values else None),
        "median_oracle_coverage": _rounded(statistics.median(coverage_values) if coverage_values else None),
        "p90_longest_bad_2s_span_seconds": _rounded(_percentile(bad_span_values, 0.90)),
        "reason_counts": {
            reason: sum(1 for row in rows if str(row.get("reason") or "") == reason)
            for reason in sorted({str(row.get("reason") or "") for row in rows})
            if reason
        },
    }


def write_json(path: Path, payload: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
