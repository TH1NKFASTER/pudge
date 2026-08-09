from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

PGS_MAGIC = b"PG"
PGS_CLOCK = 90_000.0
PDS_SEGMENT = 0x14
ODS_SEGMENT = 0x15
PCS_SEGMENT = 0x16
END_SEGMENT = 0x80
HEADER_SIZE = 13


class PGSParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PGSSegment:
    offset: int
    pts: int
    dts: int
    segment_type: int
    payload_length: int
    payload: bytes

    @property
    def time_seconds(self) -> float:
        return self.pts / PGS_CLOCK


@dataclass(frozen=True, slots=True)
class PGSCue:
    start: float
    end: float


def iter_pgs_segments(data: bytes) -> Iterable[PGSSegment]:
    """Yield raw SUP/PGS segments with their 90 kHz timestamps."""
    offset = 0
    length = len(data)
    while offset < length:
        if offset + HEADER_SIZE > length:
            raise PGSParseError(f"truncated PGS header at byte {offset}")
        if data[offset : offset + 2] != PGS_MAGIC:
            raise PGSParseError(f"missing PG magic at byte {offset}")
        pts = int.from_bytes(data[offset + 2 : offset + 6], "big")
        dts = int.from_bytes(data[offset + 6 : offset + 10], "big")
        segment_type = data[offset + 10]
        payload_length = int.from_bytes(data[offset + 11 : offset + 13], "big")
        payload_start = offset + HEADER_SIZE
        payload_end = payload_start + payload_length
        if payload_end > length:
            raise PGSParseError(f"truncated PGS payload at byte {offset}")
        yield PGSSegment(
            offset=offset,
            pts=pts,
            dts=dts,
            segment_type=segment_type,
            payload_length=payload_length,
            payload=data[payload_start:payload_end],
        )
        offset = payload_end


def _pcs_object_count(payload: bytes) -> int | None:
    # PCS layout: width(2), height(2), frame_rate(1), composition_number(2),
    # composition_state(1), palette_update_flag(1), palette_id(1), object_count(1).
    return payload[10] if len(payload) >= 11 else None


def parse_pgs_cues(path: Path, *, minimum_duration: float = 0.08) -> list[PGSCue]:
    """Extract visible bitmap intervals from a standalone .sup file.

    PGS has no explicit duration on a bitmap packet. A cue ends at the next PCS
    display update; an empty PCS clears the screen.
    """
    data = path.read_bytes()
    presentations: list[tuple[float, bool]] = []
    for segment in iter_pgs_segments(data):
        if segment.segment_type != PCS_SEGMENT:
            continue
        object_count = _pcs_object_count(segment.payload)
        if object_count is None:
            continue
        time_seconds = segment.time_seconds
        visible = object_count > 0
        if presentations and abs(time_seconds - presentations[-1][0]) < 0.001:
            presentations[-1] = (time_seconds, visible)
        else:
            presentations.append((time_seconds, visible))

    cues: list[PGSCue] = []
    for index, (start, visible) in enumerate(presentations):
        if not visible:
            continue
        end = presentations[index + 1][0] if index + 1 < len(presentations) else start + 4.0
        if end - start >= minimum_duration:
            cues.append(PGSCue(start=start, end=end))
    return cues


def onset_times(cues: Iterable[PGSCue], *, merge_within: float = 0.12) -> list[float]:
    result: list[float] = []
    for cue in cues:
        if result and cue.start - result[-1] <= merge_within:
            continue
        result.append(cue.start)
    return result


def onset_match_score(
    candidate_starts: list[float],
    reference_starts: list[float],
    *,
    tolerance: float,
) -> dict[str, float | int]:
    """Greedy one-to-one onset matching with tolerance in seconds."""
    candidate = sorted(candidate_starts)
    reference = sorted(reference_starts)
    i = j = matched = 0
    errors: list[float] = []
    while i < len(candidate) and j < len(reference):
        delta = candidate[i] - reference[j]
        if abs(delta) <= tolerance:
            matched += 1
            errors.append(abs(delta))
            i += 1
            j += 1
        elif delta < -tolerance:
            i += 1
        else:
            j += 1
    denominator = max(1, min(len(candidate), len(reference)))
    coverage = matched / denominator
    mean_error = sum(errors) / len(errors) if errors else float("inf")
    return {
        "matched": matched,
        "candidate_count": len(candidate),
        "reference_count": len(reference),
        "coverage": round(coverage, 4),
        "mean_error_seconds": round(mean_error, 4) if errors else -1.0,
    }


def build_time_mapper(knots: list[tuple[float, float]]) -> Callable[[float], float]:
    """Build a monotonic piecewise-linear mapping from source to target time."""
    if not knots:
        return lambda value: value
    ordered: list[tuple[float, float]] = []
    for source, target in sorted(knots):
        if ordered and abs(source - ordered[-1][0]) < 0.001:
            ordered[-1] = (source, max(target, ordered[-1][1]))
        else:
            ordered.append((source, target))

    # ALASS should already be monotonic. Clamp tiny malformed reversals so the
    # rewritten SUP remains valid for demuxers and mpv.
    monotonic: list[tuple[float, float]] = []
    previous_target = float("-inf")
    for source, target in ordered:
        target = max(target, previous_target)
        monotonic.append((source, target))
        previous_target = target

    sources = [item[0] for item in monotonic]
    shifts = [target - source for source, target in monotonic]

    def mapper(value: float) -> float:
        index = bisect.bisect_right(sources, value)
        if index <= 0:
            shift = shifts[0]
        elif index >= len(sources):
            shift = shifts[-1]
        else:
            left_source = sources[index - 1]
            right_source = sources[index]
            left_shift = shifts[index - 1]
            right_shift = shifts[index]
            span = right_source - left_source
            ratio = 0.0 if span <= 0 else (value - left_source) / span
            shift = left_shift + ratio * (right_shift - left_shift)
        return max(0.0, value + shift)

    return mapper


def retime_sup(source: Path, destination: Path, mapper: Callable[[float], float]) -> None:
    """Rewrite PTS/DTS of every raw SUP packet using the supplied time map."""
    data = bytearray(source.read_bytes())
    previous_pts = 0
    for segment in iter_pgs_segments(bytes(data)):
        old_seconds = segment.pts / PGS_CLOCK
        new_pts = int(round(mapper(old_seconds) * PGS_CLOCK))
        new_pts = max(previous_pts, min(new_pts, 0xFFFFFFFF))
        delta = new_pts - segment.pts
        if segment.dts:
            new_dts = max(0, min(segment.dts + delta, 0xFFFFFFFF))
        else:
            new_dts = 0
        struct.pack_into(">I", data, segment.offset + 2, new_pts)
        struct.pack_into(">I", data, segment.offset + 6, new_dts)
        previous_pts = new_pts
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
