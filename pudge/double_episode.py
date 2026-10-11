"""Japanese subtitles for a file that holds two episodes ("Show - 01-02", audit 17А).

Subtitle sites publish one file per episode. A single-episode file aligned to
the whole double video would cover only half of it, so each episode is aligned
on its own part of the audio and the results are joined:

1. Part A runs from 0 to the last line of the first episode plus 4 minutes.
   The first episode's candidates are aligned on it (part A starts at 0, so
   its times are already the file's times).
2. The second episode starts after the last aligned line of the first one and
   no later than ``duration - length of episode two``. Each bound allows up to
   150 s of ending song and preview after the last line; their overlap is the
   estimate. Part B starts 30 s before the earliest possible start, and the
   second episode's candidates are moved by the estimate first, so the aligner
   only corrects the remaining error (well inside its offset range).
3. The second episode's aligned lines are moved back by the part's start and
   appended. Where the second episode starts (the boundary) is reported so the
   first episode can be counted as watched there.

Parts are small mono 16 kHz FLAC files with the video's own subtitle tracks
(a timing reference for the aligner when the file has them).
"""

from __future__ import annotations

import hashlib
import statistics
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .subtitle_formats import parse_srt, write_srt

PART_A_TAIL = 240.0
TAIL_MAX = 150.0
PART_B_LEAD = 30.0

Cue = tuple[float, float, str]
AlignFn = Callable[[Path, list[Any]], tuple[Any, Path | None, dict[str, object]]]
CutFn = Callable[[Path, float, float | None, Path], Path]
SrtFn = Callable[[Path], Path | None]


@dataclass
class DoubleEpisodeResult:
    subtitle: Path | None
    boundary: float | None = None
    reason: str = ""
    first: Any = None
    second: Any = None
    alignment: dict[str, object] = field(default_factory=dict)


def work_dir_for(video: Path, cache_dir: Path) -> Path:
    stat = video.stat()
    key = hashlib.sha1(f"{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    return cache_dir / "double-episodes" / key


def cut_audio_part(ffmpeg: str, video: Path, start: float, end: float | None, output: Path) -> Path:
    """Cut [start, end) of the first audio track (mono 16 kHz FLAC) plus subtitle tracks."""
    output.parent.mkdir(parents=True, exist_ok=True)
    base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        base += ["-ss", f"{start:.3f}"]
    base += ["-i", str(video)]
    if end is not None:
        base += ["-t", f"{max(1.0, end - start):.3f}"]
    audio = ["-map", "0:a:0", "-c:a", "flac", "-ac", "1", "-ar", "16000"]
    attempts = (
        base + audio + ["-map", "0:s?", "-c:s", "copy", str(output)],
        base + audio + ["-sn", str(output)],  # a subtitle codec Matroska cannot hold
    )
    error = ""
    for command in attempts:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
        if completed.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return output
        error = (completed.stderr or "").strip()[-400:]
    raise RuntimeError(f"ffmpeg could not cut {video.name} [{start:.1f}, {end}]: {error}")


def _cues(path: Path | None, to_srt: SrtFn) -> list[Cue]:
    if path is None:
        return []
    srt = path if path.suffix.casefold() == ".srt" else to_srt(path)
    if srt is None or not srt.is_file():
        return []
    return sorted(parse_srt(srt), key=lambda cue: cue[0])


def _last_end(cues: Sequence[Cue]) -> float:
    return max((end for _start, end, _text in cues), default=0.0)


def _start_shift(original: Sequence[Cue], aligned: Sequence[Cue]) -> float | None:
    """How far the aligner moved the beginning of an episode (median of the first lines)."""
    if not original or not aligned:
        return None
    count = min(10, len(original), len(aligned))
    if len(original) == len(aligned):
        return statistics.median(aligned[i][0] - original[i][0] for i in range(count))
    return aligned[0][0] - original[0][0]


def prepare_double_episode(
    video: Path,
    first_candidates: Sequence[Any],
    second_candidates: Sequence[Any],
    *,
    duration: float,
    work_dir: Path,
    cut: CutFn,
    align: AlignFn,
    to_srt: SrtFn,
    logger: Any = None,
) -> DoubleEpisodeResult:
    def log(message: str, *args: object) -> None:
        if logger is not None:
            logger.info(message, *args)

    if not first_candidates or not second_candidates:
        missing = "first" if not first_candidates else "second"
        return DoubleEpisodeResult(None, reason=f"no_{missing}_episode_subtitles")
    if duration <= 0:
        return DoubleEpisodeResult(None, reason="unknown_duration")
    work_dir.mkdir(parents=True, exist_ok=True)

    first_lengths = [_last_end(_cues(candidate.path, to_srt)) for candidate in first_candidates]
    first_length = max(first_lengths, default=0.0)
    if first_length <= 0 or first_length >= duration:
        return DoubleEpisodeResult(None, reason="first_episode_subtitles_unreadable")
    part_a_end = min(duration, first_length + PART_A_TAIL)
    part_a = cut(video, 0.0, part_a_end, work_dir / "part-a.mkv")
    best_a, path_a, result_a = align(part_a, list(first_candidates))
    cues_a = _cues(path_a, to_srt)
    if best_a is None or not cues_a:
        return DoubleEpisodeResult(None, reason="first_episode_not_aligned", alignment=dict(result_a or {}))
    first_end = _last_end(cues_a)

    # Episode two starts in [first_end, duration - its length]; ending + preview
    # after each last line is at most TAIL_MAX.
    part_b_start = max(0.0, first_end - PART_B_LEAD)
    shifted: list[Any] = []
    originals: dict[Path, list[Cue]] = {}
    for index, candidate in enumerate(second_candidates):
        cues = _cues(candidate.path, to_srt)
        length = _last_end(cues)
        if not cues or length <= 0:
            continue
        low = max(first_end, duration - length - TAIL_MAX)
        high = min(duration - length, first_end + TAIL_MAX)
        if low > high + PART_B_LEAD:
            log(
                "SKIP step=double_episode.second_candidate name=%s reason=does_not_fit length=%.1f first_end=%.1f duration=%.1f",
                getattr(candidate, "name", candidate.path.name),
                length,
                first_end,
                duration,
            )
            continue
        estimate = (low + high) / 2.0 if low <= high else low
        offset = estimate - part_b_start
        moved = [(start + offset, end + offset, text) for start, end, text in cues]
        path = write_srt(moved, work_dir / f"second-{index + 1}.srt", preserve_order=True)
        originals[path] = cues
        shifted.append(replace(candidate, path=path))
    if not shifted:
        return DoubleEpisodeResult(None, reason="second_episode_does_not_fit", first=best_a)
    part_b = cut(video, part_b_start, None, work_dir / "part-b.mkv")
    best_b, path_b, result_b = align(part_b, shifted)
    cues_b = _cues(path_b, to_srt)
    if best_b is None or not cues_b:
        return DoubleEpisodeResult(
            None,
            reason="second_episode_not_aligned",
            first=best_a,
            alignment={**dict(result_a or {}), "second_episode": dict(result_b or {})},
        )
    original_b = originals.get(Path(best_b.path), [])
    moved_b = [(start + part_b_start, end + part_b_start, text) for start, end, text in cues_b]
    shift = _start_shift(original_b, cues_b)
    boundary = part_b_start + shift if shift is not None else moved_b[0][0]
    boundary = min(max(boundary, first_end), max(first_end, duration - 60.0))

    merged = [cue for cue in cues_a if cue[0] < boundary] + [
        cue for cue in moved_b if cue[0] >= first_end - 1.0
    ]
    output = write_srt(merged, work_dir / f"{video.stem}.double.ja.srt", preserve_order=False)
    alignment = dict(result_a or {})
    alignment["double_episode"] = {
        "boundary": round(boundary, 3),
        "first_end": round(first_end, 3),
        "part_b_start": round(part_b_start, 3),
        "first_candidate": getattr(best_a, "name", ""),
        "second_candidate": getattr(best_b, "name", ""),
        "second_alignment": {
            key: result_b.get(key)
            for key in (
                "engine",
                "sync_was_successful",
                "reason",
                "candidate_quality_reason",
                "offset_seconds",
            )
            if key in (result_b or {})
        },
    }
    log(
        "RESULT step=double_episode.prepare video=%s boundary=%.1f first=%s second=%s cues=%s",
        video.name,
        boundary,
        getattr(best_a, "name", ""),
        getattr(best_b, "name", ""),
        len(merged),
    )
    return DoubleEpisodeResult(output, boundary=boundary, first=best_a, second=best_b, alignment=alignment)
