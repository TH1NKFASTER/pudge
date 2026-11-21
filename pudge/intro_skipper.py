"""Local OP/ED detector (PUDGE_IMPLEMENTATION task 6).

Idea: the opening (and ending) of one title is the same audio in several
episodes, at a different time.  For every finished local episode of one
AniList entry we fingerprint the first 6 and the last 7 minutes of the
Japanese audio track (``audio_fingerprints``), align them against up to four
neighbouring episodes and keep a stretch of 30–180 s (configurable heuristic)
that is shared.  Each file gets its *own* boundaries (the median of the
partners that agree on them); a second agreeing partner raises confidence.

Only files already on disk are analysed; nothing is downloaded for analysis.
For titles whose episodes are still expected, ``intro_library`` keeps the
fingerprints of found OP/ED variants (and, until a variant is confirmed, the
head/tail of episodes) after the files are deleted, so a lone new episode is
matched against them first; a changed OP/ED falls back to cross-episode
detection and becomes another variant.  A single episode without such data,
silence, a short logo (< min length), a recap or a changed OP that no other
episode shares produce no segment.

Caches (``cache_dir/intro-skipper/``, quota via ``CacheRegistry``):

* ``fingerprints/<key>-head|tail.npz`` — key = stat identity
  (``MediaIdentity.fingerprint``: path + size + mtime, *not* a content hash)
  + audio stream + algorithm version;
* ``results/<identity>.json`` — status ``found`` / ``empty`` / ``error`` with
  its own retry time, segments, partners.  A replaced file has another
  identity, so its old intervals are never used.

Heavy work runs only under ``WorkScheduler.acquire_heavy(BACKGROUND,
foreground_sensitive=True)`` and stops between files when playback or
higher-priority work appears.  Missing numpy/FFmpeg/ffprobe disables the
analysis, never playback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import audio_timeline
from .fsutil import atomic_write_json
from .audio_fingerprints import (
    ENGINE_VERSION,
    AudioFingerprint,
    FingerprintError,
    best_shared_run,
    decode_region,
    fingerprint_samples,
    load_fingerprint,
    numpy_available,
    save_fingerprint,
)
from .intro_library import TitleLibrary, VariantRef, episodes_expected, match_variant, prune_libraries
from .process_priority import BACKGROUND_NICE

# intro-2: a stretch broken by a few seconds is one stretch; intro-3: a stretch is extended by
# short clearly-matching pieces a few seconds before/after it (an ED found ~5-9 s late).
DETECTOR_VERSION = "intro-3"
ALGORITHM_VERSION = f"{ENGINE_VERSION}+{DETECTOR_VERSION}"
# Results of these older detectors still serve playback until the background pass has
# recomputed the file (a detector update must not take every Skip button away for hours).
PLAYBACK_FALLBACK_VERSIONS = frozenset({f"{ENGINE_VERSION}+intro-2"})
SOURCE = "local-fingerprint"
HEAD_SECONDS = 6 * 60.0
TAIL_SECONDS = 7 * 60.0
MIN_FILE_SECONDS = 120.0
EMPTY_RETRY_SECONDS = 7 * 24 * 3600.0
ERROR_RETRY_SECONDS = 6 * 3600.0
CACHE_CATEGORY = "intro-skipper"
CACHE_MAX_BYTES = 256 * 1024 * 1024
CACHE_MAX_AGE_SECONDS = 180 * 24 * 3600.0
READY_EXCLUDED_STATES = {"downloading", "waiting", "trying", "missing"}


@dataclass(frozen=True)
class MediaSegment:
    kind: str  # "intro" | "outro"
    start_seconds: float
    end_seconds: float
    source: str
    confidence: float
    media_fingerprint: str  # MediaIdentity.fingerprint (stat identity) of the analysed file


@dataclass(frozen=True)
class DetectorSettings:
    min_seconds: float = 30.0
    max_seconds: float = 180.0
    max_bit_error: float = 0.33
    max_partners: int = 4
    agree_seconds: float = 3.0


@dataclass(frozen=True)
class EpisodeFile:
    path: Path
    episode: int
    identity: str  # MediaIdentity.fingerprint


@dataclass
class AnalysisReport:
    media_id: int | None = None
    fingerprinted: int = 0
    analysed: int = 0
    found: int = 0
    empty: int = 0
    errors: int = 0
    repaired: int = 0
    stopped: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def progressed(self) -> bool:
        """Did anything: fingerprinted, analysed, failed or repaired a file."""
        return bool(self.fingerprinted or self.analysed or self.errors or self.repaired)


def identity_of(path: Path) -> str:
    from .identity import MediaIdentity

    return MediaIdentity(media_id=None, media_episode=None, video_path=Path(path)).fingerprint


class IntroSkipperCache:
    def __init__(self, cache_dir: Path) -> None:
        self.root = Path(cache_dir).expanduser() / "intro-skipper"
        self.written: list[Path] = []

    def fingerprint_path(self, identity: str, stream: int | None, region: str) -> Path:
        key = hashlib.sha256(f"{identity}|{stream}|{ENGINE_VERSION}".encode()).hexdigest()[:32]
        return self.root / "fingerprints" / f"{key}-{region}.npz"

    def result_path(self, identity: str) -> Path:
        return self.root / "results" / f"{identity[:48]}.json"

    def read_result(self, identity: str) -> dict[str, Any] | None:
        if not identity:
            return None
        try:
            data = json.loads(self.result_path(identity).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("identity") != identity:
            return None
        return data

    def write_result(self, identity: str, payload: dict[str, Any]) -> None:
        path = self.result_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A failed dump (a value json cannot write, a full disk) left a stray .result.* file.
        atomic_write_json(path, {**payload, "identity": identity}, ensure_ascii=False, sort_keys=True)
        self.written.append(path)


def segments_for_video(cache_dir: Path, video: Path) -> list[MediaSegment]:
    """Ready segments of one file (cache only, cheap; for playback). Never analyses."""
    identity = identity_of(Path(video))
    data = IntroSkipperCache(cache_dir).read_result(identity)
    if data and data.get("status") == "error" and isinstance(data.get("fallback"), dict):
        # A recompute failed (a busy file): the segments found before still skip meanwhile.
        data = {**data["fallback"], "status": "found"}
    if not data or data.get("status") != "found":
        return []
    if data.get("algorithm") != ALGORITHM_VERSION and data.get("algorithm") not in PLAYBACK_FALLBACK_VERSIONS:
        return []
    result: list[MediaSegment] = []
    for row in data.get("segments") or []:
        try:
            segment = MediaSegment(
                kind=str(row["kind"]),
                start_seconds=float(row["start_seconds"]),
                end_seconds=float(row["end_seconds"]),
                source=str(row.get("source") or SOURCE),
                confidence=float(row.get("confidence") or 0.0),
                media_fingerprint=identity,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if segment.kind in {"intro", "outro"} and 0 <= segment.start_seconds < segment.end_seconds:
            result.append(segment)
    return result


SESSION_VERSION = 1


def _session_rows(segments: Sequence[MediaSegment]) -> list[dict[str, Any]]:
    return [
        {
            "kind": segment.kind,
            "start": segment.start_seconds,
            "end": segment.end_seconds,
            "confidence": segment.confidence,
            "source": segment.source,
        }
        for segment in segments
    ]


def write_playback_session(cache_dir: Path, video: Path, *, pending: bool = False) -> Path | None:
    """Session JSON for the mpv skip script, from *ready* segments only.

    Never analyses or waits.  Returns None when the file has no segments, unless
    ``pending``: then the session has no segments yet and ``"pending": true`` (the
    skip script re-reads it until segments arrive or ``pending`` turns false; see
    ``refresh_playback_session``). The caller deletes the file when mpv exits.
    """
    import secrets

    video = Path(video)
    segments = segments_for_video(cache_dir, video)
    if not segments and not pending:
        return None
    session_id = secrets.token_hex(8)
    payload: dict[str, Any] = {
        "version": SESSION_VERSION,
        "session_id": session_id,
        "video": str(video),
        "video_absolute": str(video.expanduser().resolve()),
        "fingerprint": segments[0].media_fingerprint if segments else identity_of(video),
        "segments": _session_rows(segments),
    }
    if not segments:
        payload["pending"] = True
    directory = _sessions_dir(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def _sessions_dir(cache_dir: Path) -> Path:
    return Path(cache_dir).expanduser() / "intro-skipper" / "sessions"


def refresh_playback_session(cache_dir: Path, session: Path, *, final: bool = False) -> int | None:
    """Put segments that became ready into a pending session (mpv picks them up).

    Returns the number of segments in the session, or None when it is gone / not readable.
    ``final``: detection for this playback is over; a session still without segments
    stops being pending (the skip script stops re-reading it). Only ready segments are
    written, as at launch; a session that is not pending is left as it is.
    """
    session = Path(session)
    try:
        data = json.loads(session.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return None
    if not data.get("pending"):
        return len(data["segments"])
    segments = segments_for_video(cache_dir, Path(str(data.get("video") or "")))
    if not segments and not final:
        return 0
    payload = {**data, "segments": _session_rows(segments), "pending": False}
    if segments:
        payload["fingerprint"] = segments[0].media_fingerprint
    if not session.exists():
        return None  # mpv exited and its session was removed meanwhile: never recreate it
    fd, tmp = tempfile.mkstemp(prefix=".session.", dir=str(session.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, session)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        return None
    return len(segments)


SESSION_REFRESH_MAX_AGE_SECONDS = 12 * 3600.0


def refresh_playback_sessions(cache_dir: Path, logger: logging.Logger | None = None) -> int:
    """After a pass: feed every open pending session whose video now has segments."""
    directory = _sessions_dir(cache_dir)
    if not directory.is_dir():
        return 0
    updated = 0
    now = time.time()
    for path in directory.glob("*.json"):
        try:
            if now - path.stat().st_mtime > SESSION_REFRESH_MAX_AGE_SECONDS:
                continue  # left by a player that crashed
            pending = json.loads(path.read_text(encoding="utf-8")).get("pending")
        except (OSError, ValueError, AttributeError):
            continue
        if pending and refresh_playback_session(cache_dir, path):
            updated += 1
    if updated and logger is not None:
        logger.info("EVENT step=intro_skipper.session_refresh sessions=%s", updated)
    return updated


# ---------------------------------------------------------------- probing ---


def probe_audio(video: Path, ffprobe: str, *, timeout: float = 30.0) -> tuple[float, int | None]:
    """(duration seconds, Japanese audio stream index or None for the first track)."""
    from .subtitles.stt import _select_japanese_audio_stream

    try:
        completed = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        duration = float(json.loads(completed.stdout or "{}")["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise FingerprintError(f"ffprobe duration failed: {exc}") from exc
    index, details = _select_japanese_audio_stream(Path(video), ffprobe)
    if index is None and details.get("selection_reason") in {"no_audio_streams"}:
        raise FingerprintError("no audio stream")
    if index is None and details.get("selection_reason") == "ambiguous_multiple_audio_streams":
        raise FingerprintError("ambiguous audio streams (no Japanese tag)")
    return duration, index


def _meta_path(cache: IntroSkipperCache, identity: str) -> Path:
    return cache.root / "fingerprints" / f"{hashlib.sha256(identity.encode()).hexdigest()[:32]}.json"


def cached_fingerprints(
    cache: IntroSkipperCache, identity: str
) -> tuple[AudioFingerprint, AudioFingerprint, float] | None:
    """(head, tail, duration) from the per-file cache; works after the file is gone."""
    try:
        meta = json.loads(_meta_path(cache, identity).read_text(encoding="utf-8"))
        stream = meta.get("stream")
        duration = float(meta["duration"])
        if meta.get("engine") == ENGINE_VERSION:
            head = load_fingerprint(cache.fingerprint_path(identity, stream, "head"))
            tail = load_fingerprint(cache.fingerprint_path(identity, stream, "tail"))
            if head is not None and tail is not None:
                return head[0], tail[0], duration
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _early_legacy_fingerprints(cache: IntroSkipperCache, item: EpisodeFile, ffprobe: str) -> bool:
    """Fingerprints made before the playback timeline whose opening times are off.

    A track that starts late made them early by its delay; audio that starts before
    the video lost its lead in the opening region (a seek to the first keyframe).
    A timestamp gap inside a fingerprinted region (timeline version 2) made what
    followed it early by the gap.
    Such a file is fingerprinted again and its old result dropped. Other files keep
    theirs, marked as checked so that no later process probes them again.
    """
    meta_path = _meta_path(cache, item.identity)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict) or not Path(item.path).is_file():
        return False
    try:
        version = int(meta.get("timeline") or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= audio_timeline.VERSION:
        return False
    stream = meta.get("stream")
    try:
        index = int(stream) if stream is not None else None
    except (TypeError, ValueError):
        index = None
    found = audio_timeline.timeline(item.path, ffprobe)
    if found is None:
        return False  # not probed (yet): nothing known, nothing stamped
    moved = bool(version < 1 and (found.delay(index=index) or found.video_lead()))
    if not moved:
        try:
            duration = float(meta.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        gaps = _gaps_of(item.path, ffprobe, found, index)
        if gaps is None:
            return False  # not scanned (yet): nothing stamped
        moved = any(_gap_in_region(gaps, start, end) for start, end in _regions(duration or HEAD_SECONDS))
    if not moved:
        try:
            meta_path.write_text(json.dumps({**meta, "timeline": audio_timeline.VERSION}), encoding="utf-8")
        except OSError:
            pass
        return False
    cache.result_path(item.identity).unlink(missing_ok=True)
    return True


def _gaps_of(
    path: Path, ffprobe: str, found: audio_timeline.AudioTimeline, index: int | None
) -> tuple[tuple[float, float], ...] | None:
    """Timestamp gaps inside the track, at playback times."""
    gaps = audio_timeline.inner_gaps(path, ffprobe, index=index)
    if gaps is None:
        return None
    return tuple((at - found.format_start, length) for at, length in gaps)


def _regions(duration: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """The head and tail regions that are fingerprinted (playback times)."""
    return (0.0, min(HEAD_SECONDS, duration)), (max(0.0, duration - TAIL_SECONDS), duration)


def _gap_in_region(gaps: tuple[tuple[float, float], ...], start: float, end: float) -> bool:
    # A region read with -ss starts at the right time: only a gap inside it collapses.
    return any(start - 0.5 < at < end for at, _length in gaps)


def _fingerprints_for(
    item: EpisodeFile,
    cache: IntroSkipperCache,
    *,
    ffmpeg: str,
    ffprobe: str,
    allow_compute: bool,
    cancel_check: Callable[[], bool] | None,
) -> tuple[AudioFingerprint, AudioFingerprint, float, bool] | None:
    """Cached or new (head, tail, duration, computed); None when not cached and not allowed to compute."""
    meta_path = _meta_path(cache, item.identity)
    cached = cached_fingerprints(cache, item.identity)
    if cached is not None and not _early_legacy_fingerprints(cache, item, ffprobe):
        return (*cached, False)
    if not allow_compute:
        return None
    duration, stream = probe_audio(item.path, ffprobe)
    if duration < MIN_FILE_SECONDS:
        raise FingerprintError(f"too short for OP/ED ({duration:.0f} s)")
    (_head_start, head_len), (tail_start, _end) = _regions(duration)
    # Times are playback times (the mpv skip compares them with time-pos): a
    # track that starts after the file start is decoded with that silence first.
    # Audio that starts before the video: a region from 0 is read without a seek.
    found = audio_timeline.timeline(item.path, ffprobe)
    delay = found.delay(index=stream) if found is not None else 0.0  # no index: the first track
    timeline_kwargs: dict[str, Any] = {}
    if delay:
        timeline_kwargs["leading_silence"] = delay
    elif found is not None and found.video_lead():
        timeline_kwargs["from_file_start"] = True
    # A gap inside a region (a remux seam) is filled: that region is decoded from the
    # file start (only such files pay for it).
    gaps = _gaps_of(item.path, ffprobe, found, stream) if found is not None else None
    head_kwargs = dict(timeline_kwargs)
    tail_kwargs = dict(timeline_kwargs)
    if gaps and _gap_in_region(gaps, 0.0, head_len):
        head_kwargs["fill_gaps"] = True
    if gaps and _gap_in_region(gaps, tail_start, duration):
        tail_kwargs["fill_gaps"] = True
    head = fingerprint_samples(
        decode_region(
            item.path,
            ffmpeg=ffmpeg,
            start_seconds=0.0,
            duration_seconds=head_len,
            stream_index=stream,
            cancel_check=cancel_check,
            nice=BACKGROUND_NICE,
            **head_kwargs,
        ),
        start_seconds=0.0,
    )
    tail = fingerprint_samples(
        decode_region(
            item.path,
            ffmpeg=ffmpeg,
            start_seconds=tail_start,
            duration_seconds=duration - tail_start,
            stream_index=stream,
            cancel_check=cancel_check,
            nice=BACKGROUND_NICE,
            **tail_kwargs,
        ),
        start_seconds=tail_start,
    )
    info = {"identity": item.identity, "stream": stream}
    head_path = cache.fingerprint_path(item.identity, stream, "head")
    tail_path = cache.fingerprint_path(item.identity, stream, "tail")
    save_fingerprint(head_path, head, meta={**info, "region": "head"})
    save_fingerprint(tail_path, tail, meta={**info, "region": "tail"})
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "engine": ENGINE_VERSION,
                "stream": stream,
                "duration": duration,
                "identity": item.identity,
                "timeline": audio_timeline.VERSION,
            }
        ),
        encoding="utf-8",
    )
    cache.written.extend([head_path, tail_path, meta_path])
    return head, tail, duration, True


# --------------------------------------------------------------- matching ---


def _consensus(
    candidates: list[tuple[float, float, float]], agree: float
) -> tuple[float, float, int, float] | None:
    """(start, end, supporters, mean BER) of the largest group of agreeing candidates."""
    best: tuple[int, float, float, list[tuple[float, float, float]]] | None = None
    for start, end, _ber in candidates:
        group = [c for c in candidates if abs(c[0] - start) <= agree and abs(c[1] - end) <= agree]
        key = (
            len(group),
            statistics.median(c[1] - c[0] for c in group),
            -statistics.mean(c[2] for c in group),
        )
        if best is None or key > (best[0], best[1], best[2]):
            best = (key[0], key[1], key[2], group)
    if best is None:
        return None
    group = best[3]
    return (
        statistics.median(c[0] for c in group),
        statistics.median(c[1] for c in group),
        len(group),
        statistics.mean(c[2] for c in group),
    )


CONFIRMED_SUPPORT = 3  # a confirmed library variant counts like three agreeing partners
VARIANT_EDGE_SLACK = 1.0  # seconds: partner consensus covering a variant match and reaching past it


def _confidence(supporters: int, ber: float) -> float:
    base = 0.6 if supporters <= 1 else 0.8 if supporters == 2 else 0.9
    return round(max(0.3, base - max(0.0, ber - 0.2)), 3)


def detect_segments(
    target: EpisodeFile,
    target_fp: tuple[AudioFingerprint, AudioFingerprint, float],
    partners: Sequence[tuple[EpisodeFile, tuple[AudioFingerprint, AudioFingerprint, float]]],
    settings: DetectorSettings,
    variants: dict[str, Sequence[VariantRef]] | None = None,
) -> list[MediaSegment]:
    """Segments of ``target`` from stored OP/ED ``variants`` or its shared audio with ``partners`` (pure)."""
    return [segment for segment, _variant in _detect(target, target_fp, partners, settings, variants or {})]


def _match_library(
    mine: AudioFingerprint, refs: Sequence[VariantRef], settings: DetectorSettings
) -> tuple[float, float, float, VariantRef] | None:
    best: tuple[float, float, float, VariantRef] | None = None
    for ref in refs:
        run = match_variant(
            mine,
            ref.fingerprint,
            min_seconds=settings.min_seconds,
            max_seconds=settings.max_seconds,
            max_bit_error=settings.max_bit_error,
        )
        if run is None:
            continue
        if best is None or run.duration > best[1] - best[0] + 0.5:
            best = (run.a_start, run.a_end, run.bit_error_rate, ref)
    return best


def _partner_consensus(
    mine: AudioFingerprint,
    index: int,
    partners: Sequence[tuple[EpisodeFile, tuple[AudioFingerprint, AudioFingerprint, float]]],
    settings: DetectorSettings,
) -> tuple[float, float, int, float] | None:
    candidates: list[tuple[float, float, float]] = []
    for _partner, fps in partners:
        run = best_shared_run(
            mine,
            fps[index],
            min_seconds=settings.min_seconds,
            max_seconds=settings.max_seconds,
            max_bit_error=settings.max_bit_error,
        )
        if run is not None:
            candidates.append((run.a_start, run.a_end, run.bit_error_rate))
    return _consensus(candidates, settings.agree_seconds)


def _detect(
    target: EpisodeFile,
    target_fp: tuple[AudioFingerprint, AudioFingerprint, float],
    partners: Sequence[tuple[EpisodeFile, tuple[AudioFingerprint, AudioFingerprint, float]]],
    settings: DetectorSettings,
    variants: dict[str, Sequence[VariantRef]],
) -> list[tuple[MediaSegment, str | None]]:
    head, tail, duration = target_fp
    segments: list[MediaSegment] = []
    found: list[tuple[MediaSegment, str | None]] = []
    for kind, mine, index in (("intro", head, 0), ("outro", tail, 1)):
        stored = _match_library(mine, variants.get(kind) or (), settings)
        if stored is not None and partners:
            shared = _partner_consensus(mine, index, partners, settings)
            if shared is not None and shared[1] - shared[0] <= settings.max_seconds:
                if (
                    not stored[3].confirmed
                    and shared[0] < stored[1]
                    and stored[0] < shared[1]
                    and (shared[1] - shared[0]) >= (stored[1] - stored[0]) + 5.0
                ):
                    # An unconfirmed variant can be a piece of the real OP (made before broken
                    # pieces were joined: Assassination Classroom E08's 48 s "OP"). A longer
                    # stretch the partners share over it wins.
                    stored = None
                elif (
                    shared[0] <= stored[0] + VARIANT_EDGE_SLACK
                    and shared[1] >= stored[1] - VARIANT_EDGE_SLACK
                    and max(stored[0] - shared[0], shared[1] - stored[1]) >= VARIANT_EDGE_SLACK
                ):
                    # A variant cut from an older, shorter detection (an ED found a few seconds
                    # late) is grown to what the partners share around it; ``observe`` then
                    # re-cuts the variant, so later lone episodes get the full span too.
                    stored = (min(stored[0], shared[0]), max(stored[1], shared[1]), stored[2], stored[3])
        if stored is not None:
            start, end, ber, ref = stored
            start, end = max(0.0, start), min(duration, end)
            if settings.min_seconds <= end - start <= settings.max_seconds and not any(
                s.start_seconds < end and start < s.end_seconds for s in segments
            ):
                supporters = CONFIRMED_SUPPORT if ref.confirmed else ref.episodes
                segment = MediaSegment(
                    kind=kind,
                    start_seconds=round(start, 2),
                    end_seconds=round(end, 2),
                    source=SOURCE,
                    confidence=_confidence(supporters, ber),
                    media_fingerprint=target.identity,
                )
                segments.append(segment)
                found.append((segment, ref.id))
                continue
        agreed = _partner_consensus(mine, index, partners, settings)
        if agreed is None:
            continue
        start, end, supporters, ber = agreed
        start, end = max(0.0, start), min(duration, end)
        if not (settings.min_seconds <= end - start <= settings.max_seconds):
            continue
        if any(s.start_seconds < end and start < s.end_seconds for s in segments):
            continue  # short file: the tail window saw the OP again
        segment = MediaSegment(
            kind=kind,
            start_seconds=round(start, 2),
            end_seconds=round(end, 2),
            source=SOURCE,
            confidence=_confidence(supporters, ber),
            media_fingerprint=target.identity,
        )
        segments.append(segment)
        found.append((segment, None))
    return found


def _needs_analysis(result: dict[str, Any] | None, partner_ids: set[str], now: float) -> bool:
    if result is None or result.get("algorithm") != ALGORITHM_VERSION:
        return True
    status = result.get("status")
    known = set(result.get("partners") or [])
    new_partners = bool(partner_ids - known)
    if status == "error":
        return now >= float(result.get("retry_after") or 0)
    if status == "empty":
        return new_partners or now >= float(result.get("retry_after") or 0)
    if status == "found":
        rows = result.get("segments") or []
        confidence = min((float(s.get("confidence") or 0) for s in rows), default=0.0)
        both = {str(s.get("kind")) for s in rows} >= {"intro", "outro"}
        return new_partners and (confidence < 0.9 or not both)
    return True


def _stored_files(library: TitleLibrary | None, exclude: set[int]) -> list[tuple[EpisodeFile, Any]]:
    if library is None:
        return []
    return [
        (EpisodeFile(path=Path(), episode=episode, identity=identity), fps)
        for episode, identity, fps in library.stored_partners(exclude)
    ]


def _partner_ids(
    pool: Sequence[EpisodeFile], item: EpisodeFile, settings: DetectorSettings, library
) -> set[str]:
    ids = {p.identity for p in _nearest(pool, item, settings.max_partners)}
    return ids | (library.variant_ids() if library is not None else set())


def _in_error_backoff(result: dict[str, Any] | None, now: float) -> bool:
    return bool(
        result
        and result.get("status") == "error"
        and result.get("algorithm") == ALGORITHM_VERSION
        and now < float(result.get("retry_after") or 0)
    )


def _library_copies(files: Sequence[EpisodeFile]) -> dict[int, str]:
    """Episode -> identity of the one file per episode the title library keeps.

    The library has one slot per episode. With two files of one episode number (an upgrade
    kept both; Baccano's "S00E01 ... OVA 1" filed as episode 1 next to S01E01) it keeps the
    last one by path, so ``title_needs_analysis`` and ``analyze_title`` agree on which copy
    it must have observed. The other copies still get their own per-file results.
    """
    chosen: dict[int, str] = {}
    for item in sorted(files, key=lambda item: (item.episode, str(item.path))):
        chosen[item.episode] = item.identity
    return chosen


def title_needs_analysis(
    files: Sequence[EpisodeFile],
    cache: IntroSkipperCache,
    settings: DetectorSettings,
    now: float,
    library: TitleLibrary | None = None,
) -> bool:
    """Same partner choice as ``analyze_title``; files in an error back-off are left out."""
    results = {item.identity: cache.read_result(item.identity) for item in files}
    if any(_same_episode_twins(item, results[item.identity], cache) for item in files):
        return True  # found together with another copy of the same episode: forget and redo
    usable = [item for item in files if not _in_error_backoff(results[item.identity], now)]
    copies = _library_copies(usable)
    has_library = library is not None and library.has_reference(item.episode for item in usable)
    if len(usable) < 2 and not (usable and has_library):
        # A lone episode is still fingerprinted into the title's library, so the next one has
        # a partner: a weekly show whose watched episode was deleted before the next one came
        # never had two files at once, and never got an intro skip.
        return bool(
            library is not None
            and usable
            and any(not library.has_episode(item.episode, item.identity) for item in usable)
        )
    if library is not None and any(
        not library.observed(episode, identity) for episode, identity in copies.items()
    ):
        return True
    stored = library.stored_ids({i.episode for i in usable}) if library is not None else []
    pool = list(usable) + [EpisodeFile(path=Path(), episode=e, identity=i) for e, i in stored]
    return any(
        _needs_analysis(results[item.identity], _partner_ids(pool, item, settings, library), now)
        for item in usable
    )


def _same_episode_twins(
    item: EpisodeFile, result: dict[str, Any] | None, cache: IntroSkipperCache
) -> list[str]:
    """Partners of a found result that were other copies of the same episode."""
    if not result or result.get("status") != "found":
        return []
    twins = []
    for partner in result.get("partners") or []:
        if str(partner).startswith("variant:") or partner == item.identity:
            continue
        other = cache.read_result(str(partner))
        if other is not None and other.get("episode") == item.episode:
            twins.append(str(partner))
    return twins


def _forget_same_episode_pairs(
    files: Sequence[EpisodeFile],
    cache: IntroSkipperCache,
    library: TitleLibrary | None,
    log: logging.Logger,
) -> int:
    """Drop what two copies of one episode "found" together (before _nearest left them out).

    The result goes (the file is analysed again with real partners), and so do the library
    variants that analysis created: heard in that episode only and made at the same moment.
    """
    forgotten = 0
    for item in files:
        result = cache.read_result(item.identity)
        if not result or result.get("status") != "found":
            continue
        twins = _same_episode_twins(item, result, cache)
        if not twins:
            continue
        cache.result_path(item.identity).unlink(missing_ok=True)
        dropped: list[str] = []
        if library is not None:
            checked = float(result.get("checked_at") or 0.0)
            for variant_id, row in list(library.data.get("variants", {}).items()):
                episodes = [int(value) for value in row.get("episodes") or []]
                if (
                    episodes == [int(item.episode)]
                    and abs(float(row.get("created_at") or 0.0) - checked) <= 120.0
                ):
                    library._remove_variant(variant_id)
                    dropped.append(variant_id)
        forgotten += 1
        log.info(
            "REPAIR step=intro_skipper.same_episode_pair episode=%s path=%s twins=%s variants_dropped=%s",
            item.episode,
            item.path.name,
            len(twins),
            ",".join(dropped) or "-",
        )
    return forgotten


def _nearest(items: Sequence[EpisodeFile], target: EpisodeFile, limit: int) -> list[EpisodeFile]:
    # Another copy of the same episode (an upgrade kept both) shares all of its audio, not
    # only the opening: Kusuriya no Hitorigoto S3 #3 got "the opening" at 4:05-6:00.
    others = [item for item in items if item.identity != target.identity and item.episode != target.episode]
    others.sort(key=lambda item: (abs(item.episode - target.episode), item.episode))
    return others[:limit]


def analyze_title(
    files: Sequence[EpisodeFile],
    *,
    cache: IntroSkipperCache,
    ffmpeg: str,
    ffprobe: str,
    settings: DetectorSettings = DetectorSettings(),
    should_stop: Callable[[], str | None] = lambda: None,
    max_new_fingerprints: int = 4,
    logger: logging.Logger | None = None,
    now: Callable[[], float] = time.time,
    library: TitleLibrary | None = None,
    prefer: Iterable[str] = (),
) -> AnalysisReport:
    """``prefer``: identities fingerprinted first (a just downloaded or playing episode), so
    the fingerprint budget is never spent on older files before them."""
    log = logger or logging.getLogger(__name__)
    report = AnalysisReport()
    try:
        return _analyze_title(
            files,
            report=report,
            cache=cache,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            settings=settings,
            should_stop=should_stop,
            max_new_fingerprints=max_new_fingerprints,
            log=log,
            stamp=now(),
            library=library,
            prefer=frozenset(prefer),
        )
    finally:
        if library is not None:
            library.reconcile(settings, log)
            library.save()


def _analyze_title(
    files: Sequence[EpisodeFile],
    *,
    report: AnalysisReport,
    cache: IntroSkipperCache,
    ffmpeg: str,
    ffprobe: str,
    settings: DetectorSettings,
    should_stop: Callable[[], str | None],
    max_new_fingerprints: int,
    log: logging.Logger,
    stamp: float,
    library: TitleLibrary | None,
    prefer: frozenset[str] = frozenset(),
) -> AnalysisReport:
    ordered = sorted(files, key=lambda item: (item.episode, str(item.path)))
    fingerprints: dict[str, tuple[AudioFingerprint, AudioFingerprint, float]] = {}
    budget = max(0, int(max_new_fingerprints))

    def cancel() -> bool:
        return should_stop() is not None

    usable: list[EpisodeFile] = []
    # Preferred files first (stable: episode order otherwise).
    for item in sorted(ordered, key=lambda item: item.identity not in prefer):
        reason = should_stop()
        if reason:
            report.stopped = reason
            return report
        if _in_error_backoff(cache.read_result(item.identity), stamp):
            continue  # as title_needs_analysis: an error of an older algorithm is retried
        try:
            fps = _fingerprints_for(
                item, cache, ffmpeg=ffmpeg, ffprobe=ffprobe, allow_compute=budget > 0, cancel_check=cancel
            )
        except FingerprintError as exc:
            if str(exc) == "cancelled":
                report.stopped = should_stop() or "cancelled"
                return report
            report.errors += 1
            log.info("FAIL step=intro_skipper.fingerprint path=%s error=%r", item.path.name, str(exc)[:200])
            previous = cache.read_result(item.identity) or {}
            if previous.get("status") == "error":
                previous = previous.get("fallback") or {}
            cache.write_result(
                item.identity,
                {
                    "algorithm": ALGORITHM_VERSION,
                    "status": "error",
                    "error": str(exc)[:300],
                    "checked_at": stamp,
                    "retry_after": stamp + ERROR_RETRY_SECONDS,
                    "segments": [],
                    "partners": [],
                    "path": str(item.path),
                    # What playback used before this failed recompute (an earlier version's
                    # "found"): kept for segments_for_video until a recompute succeeds.
                    **(
                        {
                            "fallback": {
                                "algorithm": previous.get("algorithm"),
                                "segments": list(previous.get("segments") or []),
                            }
                        }
                        if previous.get("status", "found") == "found" and previous.get("segments")
                        else {}
                    ),
                },
            )
            continue
        usable.append(item)  # what title_needs_analysis counts next time (no error back-off)
        if fps is None:
            continue
        if fps[3]:
            budget -= 1
            report.fingerprinted += 1
        fingerprints[item.identity] = fps[:3]

    ready = [item for item in ordered if item.identity in fingerprints]
    report.repaired = _forget_same_episode_pairs(ready, cache, library, log)
    # The files title_needs_analysis expects in the library (one per episode number).
    copies = _library_copies(usable)

    def in_library(item: EpisodeFile) -> bool:
        return library is not None and copies.get(item.episode) == item.identity

    if library is not None:
        for item in ready:
            if in_library(item):
                library.add_episode(item.episode, item.identity, fingerprints[item.identity])
    stored = _stored_files(library, {item.episode for item in ready})
    stored_fps = {partner.identity: fps for partner, fps in stored}
    pool = list(ready) + [partner for partner, _fps in stored]
    for item in ready:
        reason = should_stop()
        if reason:
            report.stopped = reason
            return report
        variants = (
            {"intro": library.variants("intro"), "outro": library.variants("outro")}
            if library is not None
            else {}
        )
        partners = _nearest(pool, item, settings.max_partners)
        if not partners and not any(variants.values()):
            continue  # one episode alone proves nothing
        partner_ids = _partner_ids(pool, item, settings, library)
        existing = cache.read_result(item.identity)
        if not _needs_analysis(existing, partner_ids, stamp):
            if in_library(item) and not library.observed(item.episode, item.identity):
                library.observe(
                    item.episode,
                    item.identity,
                    fingerprints[item.identity],
                    _result_segments(existing),
                    settings,
                )
            continue
        found = _detect(
            item,
            fingerprints[item.identity],
            [(p, fingerprints.get(p.identity) or stored_fps[p.identity]) for p in partners],
            settings,
            variants,
        )
        segments = [segment for segment, _variant in found]
        if in_library(item):
            library.observe(
                item.episode,
                item.identity,
                fingerprints[item.identity],
                [(s.kind, s.start_seconds, s.end_seconds, variant) for s, variant in found],
                settings,
            )
        report.analysed += 1
        status = "found" if segments else "empty"
        if segments:
            report.found += 1
        else:
            report.empty += 1
        cache.write_result(
            item.identity,
            {
                "algorithm": ALGORITHM_VERSION,
                "status": status,
                "segments": [asdict(segment) for segment in segments],
                # Variants this file itself just gave are no news for it next time.
                "partners": sorted(partner_ids | (library.variant_ids() if library is not None else set())),
                "checked_at": stamp,
                "retry_after": stamp + EMPTY_RETRY_SECONDS if not segments else 0,
                "path": str(item.path),
                "episode": item.episode,
                "settings": asdict(settings),
            },
        )
        log.info(
            "RESULT step=intro_skipper.detect episode=%s status=%s partners=%s stored=%s segments=%s",
            item.episode,
            status,
            len(partners),
            sum(1 for p in partners if p.identity in stored_fps),
            ",".join(
                f"{s.kind}:{s.start_seconds:.1f}-{s.end_seconds:.1f}@{s.confidence}"
                + (f"(variant {variant})" if variant else "")
                for s, variant in found
            )
            or "-",
        )
    return report


def _result_segments(result: dict[str, Any] | None) -> list[tuple[str, float, float, str | None]]:
    if not result or result.get("status") != "found":
        return []
    rows = []
    for row in result.get("segments") or []:
        try:
            rows.append((str(row["kind"]), float(row["start_seconds"]), float(row["end_seconds"]), None))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


# -------------------------------------------------------- background pass ---


def _ready_files(db: Any, media_id: int, incomplete: Iterable[Path]) -> list[EpisodeFile]:
    blocked = [Path(path).expanduser() for path in incomplete]
    files: list[EpisodeFile] = []
    seen: set[str] = set()
    for row in db.episodes(media_id):
        if row.episode is None or str(row.state or "") in READY_EXCLUDED_STATES:
            continue
        path = Path(row.video_path).expanduser()
        if not path.is_file():
            continue
        if any(path == root or root in path.parents for root in blocked):
            continue
        identity = identity_of(path)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        files.append(EpisodeFile(path=path, episode=int(row.episode), identity=identity))
    return files


def tools_missing(ffmpeg: str, ffprobe: str) -> list[str]:
    missing = []
    if not numpy_available():
        missing.append("numpy")
    for name, value in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        candidate = Path(str(value)).expanduser()
        if not (candidate.is_file() or shutil.which(str(value))):
            missing.append(name)
    return missing


def _resolve(tool: str) -> str:
    candidate = Path(str(tool)).expanduser()
    return str(candidate) if candidate.is_file() else (shutil.which(str(tool)) or str(tool))


NO_PROGRESS_FILE = "no-progress.json"
NO_PROGRESS_RECHECK_SECONDS = 24 * 3600.0
DEFERRAL_LOG_SECONDS = 3600.0
_deferral_logged: dict[str, float] = {}


def _log_deferral(log: logging.Logger, reason: str) -> None:
    """The pass waits (mpv open, heavy work): say why, at most once an hour per reason."""
    now = time.monotonic()
    last = _deferral_logged.get(reason)
    if last is not None and now - last < DEFERRAL_LOG_SECONDS:
        return
    _deferral_logged[reason] = now
    log.info(
        "SKIP step=intro_skipper.pass reason=deferred wait=%s log_every_s=%d", reason, DEFERRAL_LOG_SECONDS
    )


def _title_signature(media_id: int, files: Sequence[EpisodeFile], cache: IntroSkipperCache) -> str:
    """What one title's analysis starts from: its files, their results and its library."""
    rows = []
    for item in sorted(files, key=lambda item: (item.episode, item.identity, str(item.path))):
        result = cache.read_result(item.identity) or {}
        rows.append(
            [
                item.episode,
                item.identity,
                result.get("algorithm"),
                result.get("status"),
                result.get("checked_at"),
                sorted(str(p) for p in result.get("partners") or []),
            ]
        )
    library: Any = None
    path = cache.root / "library" / str(int(media_id)) / "library.json"
    try:
        library = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        library = None
    if isinstance(library, dict):
        library = {key: value for key, value in library.items() if key != "updated_at"}
    payload = json.dumps([ALGORITHM_VERSION, rows, library], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recheck_at(files: Sequence[EpisodeFile], cache: IntroSkipperCache, now: float) -> float:
    """When a title that did nothing is looked at again although nothing changed: the next
    retry time of one of its results, at the latest after a day."""
    due = now + NO_PROGRESS_RECHECK_SECONDS
    for item in files:
        result = cache.read_result(item.identity) or {}
        retry = float(result.get("retry_after") or 0)
        if now < retry < due:
            due = retry
    return due


def _load_no_progress(cache: IntroSkipperCache) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((cache.root / NO_PROGRESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    memo: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("until"), (int, float)):
            memo[str(key)] = value
    return memo


def _save_no_progress(cache: IntroSkipperCache, memo: dict[str, dict[str, Any]], now: float) -> None:
    kept = {key: value for key, value in memo.items() if float(value.get("until") or 0) > now}
    path = cache.root / NO_PROGRESS_FILE
    try:
        if not kept:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, kept, sort_keys=True)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "FAIL step=intro_skipper.no_progress_save error=%r", str(exc)[:200]
        )


def _admission(scheduler: Any, paused_ok: bool) -> str | None:
    """Why the pass waits, or None. ``paused_ok``: the agent's own step beside a paused video
    mpv (never inside the maintenance lock); "paused_recently" is not waited out here."""
    if paused_ok:
        return scheduler.background_wait_reason(foreground_paused_ok=True)
    return scheduler.background_wait_reason()


def _open_title(db: Any, scheduler: Any) -> int | None:
    """media_id of the video open in the (paused) foreground mpv, if it is in the library."""
    probe = getattr(scheduler, "foreground_video", None)
    video = probe() if callable(probe) else None
    if video is None:
        return None
    return _title_of(db, Path(video))


def _same_video(a: Path, b: Path) -> bool:
    a, b = Path(a).expanduser(), Path(b).expanduser()
    if a == b:
        return True
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _title_of(db: Any, video: Path) -> int | None:
    """media_id of a library file, or None."""
    video = Path(video).expanduser()
    finder = getattr(db, "episode_by_path", None)
    row = None
    if callable(finder):
        try:
            row = finder(video)
        except Exception:  # noqa: BLE001 - ordering is a nicety
            row = None
    if row is None:
        wanted = {video, video.resolve()}
        try:
            row = next(
                (
                    item
                    for item in db.episodes()
                    if Path(str(item.video_path)).expanduser() in wanted
                    or Path(str(item.video_path)).expanduser().resolve() in wanted
                ),
                None,
            )
        except Exception:  # noqa: BLE001 - ordering is a nicety
            row = None
    media_id = getattr(row, "media_id", None) if row is not None else None
    return int(media_id) if media_id is not None else None


PLAYBACK_MAX_NEW_FINGERPRINTS = 3  # the playing file and, at most, two partners


def _playback_admission(scheduler: Any, video: Path) -> str | None:
    """Why the step for a playing video waits, or None.

    Its own video in the foreground does not block it (one file's head and tail, at
    background priority, in a few seconds); another video does, and so do the power rules.
    Without a foreground (mpv closed) the ordinary background rules apply.
    """
    probe = getattr(scheduler, "foreground_video", None)
    current = probe() if callable(probe) else None
    if current is None:
        return _admission(scheduler, False)
    if not _same_video(Path(current), video):
        return "foreground"
    policy = getattr(scheduler, "power_policy", None)
    check = getattr(policy, "background_block_reason", None)
    return check(resource="cpu") if callable(check) else None


WANTED_FILE = "wanted.json"
WANTED_MAX_AGE_SECONDS = 24 * 3600.0
WANTED_MAX_ENTRIES = 64


def _wanted_update(cache: IntroSkipperCache, change: Callable[[dict[str, float]], dict[str, float]]) -> None:
    """Read-modify-write of the wanted list under a file lock (window, agent and workers)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Pudge ships on macOS
        fcntl = None  # type: ignore[assignment]
    cache.root.mkdir(parents=True, exist_ok=True)
    path = cache.root / WANTED_FILE
    with open(cache.root / ".wanted.lock", "a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            current = {
                str(key): float(value)
                for key, value in (data.items() if isinstance(data, dict) else ())
                if isinstance(value, (int, float))
            }
            changed = change(dict(current))
            if changed == current:
                return
            if not changed:
                path.unlink(missing_ok=True)
                return
            atomic_write_json(path, changed, sort_keys=True)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def request_analysis(cache_dir: Path, paths: Iterable[Path], *, now: float | None = None) -> int:
    """Remember files the next pass analyses first (a completed download).

    Cheap (no numpy, no FFmpeg): safe on the window's poll thread. Entries go once the
    file has a result, is gone, or after a day.
    """
    stamp = time.time() if now is None else float(now)
    keys = [str(Path(path).expanduser()) for path in paths if str(path or "").strip()]
    if not keys:
        return 0

    def add(current: dict[str, float]) -> dict[str, float]:
        for key in keys:
            current[key] = stamp
        newest = sorted(current.items(), key=lambda row: row[1], reverse=True)[:WANTED_MAX_ENTRIES]
        return dict(newest)

    _wanted_update(IntroSkipperCache(cache_dir), add)
    return len(keys)


def _wanted_done(key: str, requested_at: float, cache: IntroSkipperCache, now: float) -> bool:
    if now - requested_at > WANTED_MAX_AGE_SECONDS:
        return True
    path = Path(key)
    if not path.is_file():
        return True
    return cache.read_result(identity_of(path)) is not None


def _wanted_paths(cache: IntroSkipperCache, now: float) -> list[Path]:
    try:
        data = json.loads((cache.root / WANTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    rows = [
        (str(key), float(value))
        for key, value in data.items()
        if isinstance(value, (int, float)) and not _wanted_done(str(key), float(value), cache, now)
    ]
    return [Path(key) for key, _at in sorted(rows, key=lambda row: row[1], reverse=True)]


def _forget_wanted(cache: IntroSkipperCache, now: float) -> None:
    if not (cache.root / WANTED_FILE).exists():
        return
    try:
        _wanted_update(
            cache,
            lambda current: {
                key: value for key, value in current.items() if not _wanted_done(key, value, cache, now)
            },
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("FAIL step=intro_skipper.wanted error=%r", str(exc)[:200])


def analysed(cache_dir: Path, video: Path) -> bool:
    """The file has a result of any status (found, empty or error) for its current identity."""
    identity = identity_of(Path(video))
    return bool(identity) and IntroSkipperCache(cache_dir).read_result(identity) is not None


def detection_enabled(config: Any) -> bool:
    """OP/ED detection is on: the skip button is on and PUDGE_INTRO_DETECTION is not 0."""
    if os.environ.get("PUDGE_INTRO_DETECTION", "1").strip() == "0":
        return False
    return bool(getattr(getattr(config, "playback", None), "skip_segments_enabled", True))


def run_background_pass(
    *,
    db: Any,
    cache_dir: Path,
    ffmpeg: str,
    ffprobe: str,
    scheduler: Any,
    logger: logging.Logger | None = None,
    incomplete_paths: Iterable[Path] = (),
    enabled: bool = True,
    cancel_check: Callable[[], bool] | None = None,
    max_new_fingerprints: int = 4,
    settings: DetectorSettings = DetectorSettings(),
    paused_ok: bool = False,
    prefer_paths: Iterable[Path] = (),
    playback_video: Path | None = None,
) -> dict[str, Any]:
    """One low-priority step: work on the first title that needs analysis.

    ``prefer_paths`` (and the files ``request_analysis`` remembered): their titles come
    first and their files are fingerprinted first (a just downloaded episode is matched
    against the season's cached fingerprints in seconds, not after other titles).

    ``playback_video``: the step for a video that is playing without segments (see
    ``PlaybackDetection``). Only its title is looked at; the step may run while that video
    is the foreground (power rules still apply) and stops when another one is.

    ``paused_ok`` (only the agent's separate step, see ``AnimeManager.run_paused_intro_detection``):
    the pass may run while the video mpv is paused on AC power, the open title first, and
    stops within ~2 s when playback resumes.

    A title whose analysis did nothing (all of its files known, nothing to compare) does not
    hold up the titles after it: the pass goes on to the next one, and the title is left
    alone until its files, their results or its library change (or a retry is due).
    """
    from .work_scheduler import WorkPriority

    log = logger or logging.getLogger(__name__)
    if not enabled or os.environ.get("PUDGE_INTRO_DETECTION", "1").strip() == "0":
        return {"status": "disabled"}
    cache = IntroSkipperCache(cache_dir)
    all_animes = list(db.anime_list())
    try:
        prune_libraries(cache.root, all_animes, logger=log)
    except Exception as exc:  # noqa: BLE001 - library bookkeeping never breaks the agent
        log.warning("FAIL step=intro_skipper.library_prune error=%r", str(exc)[:200])
    missing = tools_missing(ffmpeg, ffprobe)
    if missing:
        log.info("SKIP step=intro_skipper.pass reason=tool_missing tools=%s", ",".join(missing))
        return {"status": "tool_missing", "missing": missing}
    playing = Path(playback_video).expanduser() if playback_video is not None else None

    def admission() -> str | None:
        if playing is not None:
            return _playback_admission(scheduler, playing)
        return _admission(scheduler, paused_ok)

    # The only background work that may run while the video mpv is paused (opt-in).
    reason = admission()
    if reason is not None:
        _log_deferral(log, str(reason))
        return {"status": "deferred", "reason": reason}

    incomplete = list(incomplete_paths)
    stamp = time.time()
    animes = sorted(all_animes, key=lambda anime: (anime.status != "CURRENT", anime.title.casefold()))
    open_media = _open_title(db, scheduler) if paused_ok else None
    if open_media is not None:
        # Running beside a paused mpv: the title open in it first, then the usual order.
        animes.sort(key=lambda anime: int(anime.media_id) != open_media)
    wanted = [Path(path) for path in prefer_paths]
    if playing is not None:
        wanted = [playing]
    else:
        wanted += _wanted_paths(cache, stamp)
    preferred_media: set[int] = set()
    for path in dict.fromkeys(wanted):
        media = _title_of(db, path)
        if media is not None:
            preferred_media.add(media)
    if playing is not None:
        animes = [anime for anime in animes if int(anime.media_id) in preferred_media]
    elif preferred_media:
        # New episodes first: a lone new file of an analysed season needs one fingerprint.
        animes.sort(key=lambda anime: int(anime.media_id) not in preferred_media)
    prefer_ids = {identity_of(path) for path in wanted if Path(path).expanduser().is_file()}
    prefer_ids.discard("")
    migration: dict[str, Any] = {}
    memo = _load_no_progress(cache)
    memo_changed = False
    for entry in memo.values():
        # A clock set back must not leave a title alone for longer than a day.
        if float(entry.get("until") or 0) > stamp + NO_PROGRESS_RECHECK_SECONDS:
            entry["until"] = stamp + NO_PROGRESS_RECHECK_SECONDS
            memo_changed = True
    lease: Any = None
    outcome: AnalysisReport | None = None
    no_progress: list[int] = []

    def should_stop() -> str | None:
        if callable(cancel_check) and cancel_check():
            return "cancelled"
        # Called every 50 ms while FFmpeg decodes: resumed playback kills it within ~2 s
        # (one mpv answer is reused for 1.5 s); finished files keep their results.
        wait = admission()
        if wait is not None:
            return str(wait)
        if scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND):
            return "higher_priority"
        return None

    try:
        for anime in animes:
            media_id = int(anime.media_id)
            files = _ready_files(db, media_id, incomplete)
            library = TitleLibrary(cache.root, media_id) if episodes_expected(anime) else None
            if library is not None and not library.migrated:
                try:
                    _migrate_title(db, media_id, library, cache, files, migration, settings, log)
                except Exception as exc:  # noqa: BLE001 - migration is best effort
                    log.warning(
                        "FAIL step=intro_skipper.library_migrate media_id=%s error=%r",
                        anime.media_id,
                        str(exc)[:200],
                    )
            if not files or not title_needs_analysis(files, cache, settings, stamp, library):
                continue
            before = _title_signature(media_id, files, cache)
            known = memo.get(str(media_id))
            if known and known.get("signature") == before and stamp < float(known.get("until") or 0):
                continue  # nothing changed since a pass that did nothing for it
            if lease is None:
                lease = scheduler.acquire_heavy(
                    "intro-skipper-playback" if playing is not None else "intro-skipper",
                    blocking=False,
                    # The playback step did its own admission (its video is the foreground).
                    foreground_sensitive=playing is None,
                    priority=WorkPriority.BACKGROUND,
                    resource="cpu",
                    **({"foreground_paused_ok": True} if paused_ok else {}),
                )
                if lease is None:
                    _log_deferral(log, "heavy_busy")
                    return {"status": "deferred", "reason": "heavy_busy"}
            try:
                report = analyze_title(
                    files,
                    cache=cache,
                    ffmpeg=_resolve(ffmpeg),
                    ffprobe=_resolve(ffprobe),
                    settings=settings,
                    should_stop=should_stop,
                    max_new_fingerprints=(
                        min(max_new_fingerprints, PLAYBACK_MAX_NEW_FINGERPRINTS)
                        if playing is not None
                        else max_new_fingerprints
                    ),
                    logger=log,
                    library=library,
                    prefer=prefer_ids,
                )
            except Exception as exc:  # noqa: BLE001 - one title (a damaged library file) never ends every pass
                # It came first again on every pass, and no title after it was ever analysed.
                log.warning("FAIL step=intro_skipper.title media_id=%s error=%r", media_id, str(exc)[:200])
                memo[str(media_id)] = {"signature": before, "until": stamp + NO_PROGRESS_RECHECK_SECONDS}
                memo_changed = True
                continue
            report.media_id = media_id
            outcome = report
            log.info(
                "DONE step=intro_skipper.pass media_id=%s files=%s fingerprinted=%s analysed=%s found=%s empty=%s errors=%s stopped=%s",
                media_id,
                len(files),
                report.fingerprinted,
                report.analysed,
                report.found,
                report.empty,
                report.errors,
                report.stopped or "-",
            )
            if report.stopped or report.progressed:
                if memo.pop(str(media_id), None) is not None:
                    memo_changed = True
                break
            no_progress.append(media_id)
            if _title_signature(media_id, files, cache) == before:
                # Analysing it again would do the same nothing: leave it until something changes.
                memo[str(media_id)] = {"signature": before, "until": _recheck_at(files, cache, stamp)}
                memo_changed = True
                log.info(
                    "SKIP step=intro_skipper.title media_id=%s reason=no_progress files=%s until_change=1",
                    media_id,
                    len(files),
                )
    finally:
        if lease is not None:
            lease.release()
        if memo_changed:
            _save_no_progress(cache, memo, stamp)
        _forget_wanted(cache, stamp)
    if outcome is None:
        return {"status": "idle"}
    try:
        # A player open on a file that had no segments shows the button from now on.
        refresh_playback_sessions(cache_dir, log)
    except Exception as exc:  # noqa: BLE001 - never breaks the pass
        log.warning("FAIL step=intro_skipper.session_refresh error=%r", str(exc)[:200])
    _register_and_enforce(db, cache_dir, cache.written, log)
    result = {"status": "stopped" if outcome.stopped else "ran", **asdict(outcome)}
    if no_progress:
        result["no_progress"] = no_progress
    return result


def _results_index(cache: IntroSkipperCache) -> dict[str, dict[str, Any]]:
    """Analysed results by file path (``path``/``episode`` are kept in every found/empty result)."""
    index: dict[str, dict[str, Any]] = {}
    directory = cache.root / "results"
    if not directory.is_dir():
        return index
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("status") not in {"found", "empty"}:
            continue
        if not data.get("path") or not data.get("identity") or data.get("episode") is None:
            continue
        key = str(Path(str(data["path"])).expanduser())
        if key not in index or float(data.get("checked_at") or 0) > float(index[key].get("checked_at") or 0):
            index[key] = data
    return index


def _migrate_title(
    db: Any,
    media_id: int,
    library: TitleLibrary,
    cache: IntroSkipperCache,
    files: Sequence[EpisodeFile],
    shared: dict[str, Any],
    settings: DetectorSettings,
    log: logging.Logger,
) -> None:
    """One-time import of cached fingerprints of this title's already deleted episodes."""
    rows = list(db.episodes(media_id))
    anchors = {Path(row.video_path).expanduser().parent for row in rows if getattr(row, "video_path", None)}
    anchors |= {item.path.parent for item in files}
    if not anchors:
        return  # nothing of this title was ever on disk
    if "owners" not in shared:
        owners: dict[Path, set[int]] = {}
        for row in db.episodes():
            if getattr(row, "video_path", None) and getattr(row, "media_id", None) is not None:
                owners.setdefault(Path(row.video_path).expanduser().parent, set()).add(int(row.media_id))
        shared["owners"] = owners
        shared["results"] = _results_index(cache)
    anchors = {folder for folder in anchors if shared["owners"].get(folder, set()) <= {media_id}}
    on_disk = {item.episode for item in files}
    best: dict[int, dict[str, Any]] = {}
    for key, data in shared["results"].items():
        path = Path(key)
        try:
            episode = int(data["episode"])
        except (TypeError, ValueError):
            continue
        if path.parent not in anchors or episode in on_disk or path.exists():
            continue
        if episode not in best or float(data.get("checked_at") or 0) > float(
            best[episode].get("checked_at") or 0
        ):
            best[episode] = data
    imported = 0
    for episode in sorted(best):
        data = best[episode]
        if str(episode) in library.data["episodes"]:
            continue
        fps = cached_fingerprints(cache, str(data["identity"]))
        if fps is None:
            continue
        library.add_episode(episode, str(data["identity"]), fps)
        library.observe(episode, str(data["identity"]), fps, _result_segments(data), settings)
        imported += 1
    library.mark_migrated()
    library.reconcile(settings, log)
    library.save()
    log.info("MIGRATE step=intro_skipper.library media_id=%s imported_episodes=%s", media_id, imported)


def _register_and_enforce(db: Any, cache_dir: Path, written: Sequence[Path], log: logging.Logger) -> None:
    try:
        from .cache_registry import CachePolicy, CacheRegistry

        registry = CacheRegistry(db, Path(cache_dir))
        for path in dict.fromkeys(written):
            if Path(path).exists():
                registry.register(CACHE_CATEGORY, Path(path), metadata={"algorithm": ALGORITHM_VERSION})
        registry.enforce(
            {CACHE_CATEGORY: CachePolicy(max_bytes=CACHE_MAX_BYTES, max_age_seconds=CACHE_MAX_AGE_SECONDS)}
        )
    except Exception as exc:  # noqa: BLE001 - cache bookkeeping never breaks the agent
        log.warning("FAIL step=intro_skipper.cache_registry error=%r", str(exc)[:200])


# ------------------------------------------------------ playback pickup ---


class PlaybackDetection:
    """Detection for a video that started playing without segments (a fresh download).

    Runs ``run_pass`` (one ``run_background_pass(playback_video=...)``, normally in the
    low-RAM worker) on a daemon thread until the video has a result, then writes the
    ready segments into the pending mpv session (``refresh_playback_session``); the skip
    script re-reads it and shows the button from then on. A deferred step (heavy work
    busy, energy saving) is retried every ``retry_seconds`` until ``max_seconds``. When
    it ends without segments the session stops being pending. Never raises: playback
    does not depend on it.
    """

    RETRY_SECONDS = 20.0
    MAX_SECONDS = 30 * 60.0
    FINAL_STATUSES = frozenset({"disabled", "tool_missing", "idle", "error"})

    def __init__(
        self,
        *,
        cache_dir: Path,
        video: Path,
        session: Path,
        run_pass: Callable[[], Any],
        logger: logging.Logger | None = None,
        retry_seconds: float | None = None,
        max_seconds: float | None = None,
    ) -> None:
        import threading

        self.cache_dir = Path(cache_dir)
        self.video = Path(video)
        self.session = Path(session)
        self.run_pass = run_pass
        self.logger = logger or logging.getLogger(__name__)
        self.retry_seconds = self.RETRY_SECONDS if retry_seconds is None else float(retry_seconds)
        self.max_seconds = self.MAX_SECONDS if max_seconds is None else float(max_seconds)
        self._stop = threading.Event()
        self._thread: Any = None
        self.outcome = ""

    def start(self) -> "PlaybackDetection":
        import threading

        self._thread = threading.Thread(target=self.run, name="pudge-intro-playback", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> str:
        started = time.monotonic()
        attempts = 0
        outcome = "timeout"
        self.logger.info("START step=intro_skipper.playback video=%s", self.video.name)
        try:
            while not self._stop.is_set():
                if refresh_playback_session(self.cache_dir, self.session):
                    outcome = "segments"
                    break
                if analysed(self.cache_dir, self.video):
                    outcome = "no_segments"
                    break
                attempts += 1
                try:
                    result = self.run_pass()
                except Exception as exc:  # noqa: BLE001 - playback never depends on detection
                    self.logger.warning("FAIL step=intro_skipper.playback error=%r", str(exc)[:200])
                    result = {"status": "error"}
                status = str((result or {}).get("status") or "error") if isinstance(result, dict) else "error"
                if refresh_playback_session(self.cache_dir, self.session):
                    outcome = "segments"
                    break
                if analysed(self.cache_dir, self.video):
                    outcome = "no_segments"
                    break
                if status in self.FINAL_STATUSES:
                    outcome = status
                    break
                if time.monotonic() - started >= self.max_seconds:
                    break
                # "ran" on other files of the title (fingerprint budget): go on at once.
                if self._stop.wait(min(1.0, self.retry_seconds) if status == "ran" else self.retry_seconds):
                    break
            else:
                outcome = "stopped"
            if self._stop.is_set() and outcome == "timeout":
                outcome = "stopped"
        finally:
            count = None
            try:
                count = refresh_playback_session(self.cache_dir, self.session, final=True)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("FAIL step=intro_skipper.playback_session error=%r", str(exc)[:200])
            self.outcome = outcome
            self.logger.info(
                "DONE step=intro_skipper.playback video=%s outcome=%s attempts=%s segments=%s",
                self.video.name,
                outcome,
                attempts,
                count if count is not None else "-",
            )
        return outcome
