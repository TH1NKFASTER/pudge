"""Local OP/ED detector (PUDGE_IMPLEMENTATION task 6).

Idea: the opening (and ending) of one title is the same audio in several
episodes, at a different time.  For every finished local episode of one
AniList entry we fingerprint the first 6 and the last 7 minutes of the
Japanese audio track (``audio_fingerprints``), align them against up to four
neighbouring episodes and keep a stretch of 30–180 s (configurable heuristic)
that is shared.  Each file gets its *own* boundaries (the median of the
partners that agree on them); a second agreeing partner raises confidence.

Only files already on disk are used; nothing is downloaded for analysis.
A single episode, silence, a short logo (< min length), a recap or a changed
OP that no other file shares produce no segment.

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

DETECTOR_VERSION = "intro-1"
ALGORITHM_VERSION = f"{ENGINE_VERSION}+{DETECTOR_VERSION}"
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
    stopped: str = ""
    notes: list[str] = field(default_factory=list)


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
        fd, tmp = tempfile.mkstemp(prefix=".result.", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({**payload, "identity": identity}, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        self.written.append(path)


def segments_for_video(cache_dir: Path, video: Path) -> list[MediaSegment]:
    """Ready segments of one file (cache only, cheap; for playback). Never analyses."""
    identity = identity_of(Path(video))
    data = IntroSkipperCache(cache_dir).read_result(identity)
    if not data or data.get("status") != "found" or data.get("algorithm") != ALGORITHM_VERSION:
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


def write_playback_session(cache_dir: Path, video: Path) -> Path | None:
    """Session JSON for the mpv skip script, from *ready* segments only.

    Never analyses or waits.  Returns None when the file has no segments.
    The caller deletes the file when mpv exits.
    """
    import secrets

    video = Path(video)
    segments = segments_for_video(cache_dir, video)
    if not segments:
        return None
    session_id = secrets.token_hex(8)
    payload = {
        "version": SESSION_VERSION,
        "session_id": session_id,
        "video": str(video),
        "video_absolute": str(video.expanduser().resolve()),
        "fingerprint": segments[0].media_fingerprint,
        "segments": [
            {
                "kind": segment.kind,
                "start": segment.start_seconds,
                "end": segment.end_seconds,
                "confidence": segment.confidence,
                "source": segment.source,
            }
            for segment in segments
        ],
    }
    directory = Path(cache_dir).expanduser() / "intro-skipper" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


# ---------------------------------------------------------------- probing ---

def probe_audio(video: Path, ffprobe: str, *, timeout: float = 30.0) -> tuple[float, int | None]:
    """(duration seconds, Japanese audio stream index or None for the first track)."""
    from .subtitles.stt import _select_japanese_audio_stream

    try:
        completed = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
            text=True, capture_output=True, timeout=timeout, check=False,
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
    meta_path = cache.root / "fingerprints" / f"{hashlib.sha256(item.identity.encode()).hexdigest()[:32]}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stream = meta.get("stream")
        duration = float(meta["duration"])
        if meta.get("engine") == ENGINE_VERSION:
            head = load_fingerprint(cache.fingerprint_path(item.identity, stream, "head"))
            tail = load_fingerprint(cache.fingerprint_path(item.identity, stream, "tail"))
            if head is not None and tail is not None:
                return head[0], tail[0], duration, False
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not allow_compute:
        return None
    duration, stream = probe_audio(item.path, ffprobe)
    if duration < MIN_FILE_SECONDS:
        raise FingerprintError(f"too short for OP/ED ({duration:.0f} s)")
    head_len = min(HEAD_SECONDS, duration)
    tail_start = max(0.0, duration - TAIL_SECONDS)
    head = fingerprint_samples(
        decode_region(item.path, ffmpeg=ffmpeg, start_seconds=0.0, duration_seconds=head_len,
                      stream_index=stream, cancel_check=cancel_check),
        start_seconds=0.0,
    )
    tail = fingerprint_samples(
        decode_region(item.path, ffmpeg=ffmpeg, start_seconds=tail_start, duration_seconds=duration - tail_start,
                      stream_index=stream, cancel_check=cancel_check),
        start_seconds=tail_start,
    )
    info = {"identity": item.identity, "stream": stream}
    head_path = cache.fingerprint_path(item.identity, stream, "head")
    tail_path = cache.fingerprint_path(item.identity, stream, "tail")
    save_fingerprint(head_path, head, meta={**info, "region": "head"})
    save_fingerprint(tail_path, tail, meta={**info, "region": "tail"})
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"engine": ENGINE_VERSION, "stream": stream, "duration": duration, "identity": item.identity}),
        encoding="utf-8",
    )
    cache.written.extend([head_path, tail_path, meta_path])
    return head, tail, duration, True


# --------------------------------------------------------------- matching ---

def _consensus(candidates: list[tuple[float, float, float]], agree: float) -> tuple[float, float, int, float] | None:
    """(start, end, supporters, mean BER) of the largest group of agreeing candidates."""
    best: tuple[int, float, float, list[tuple[float, float, float]]] | None = None
    for start, end, _ber in candidates:
        group = [c for c in candidates if abs(c[0] - start) <= agree and abs(c[1] - end) <= agree]
        key = (len(group), statistics.median(c[1] - c[0] for c in group), -statistics.mean(c[2] for c in group))
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


def _confidence(supporters: int, ber: float) -> float:
    base = 0.6 if supporters <= 1 else 0.8 if supporters == 2 else 0.9
    return round(max(0.3, base - max(0.0, ber - 0.2)), 3)


def detect_segments(
    target: EpisodeFile,
    target_fp: tuple[AudioFingerprint, AudioFingerprint, float],
    partners: Sequence[tuple[EpisodeFile, tuple[AudioFingerprint, AudioFingerprint, float]]],
    settings: DetectorSettings,
) -> list[MediaSegment]:
    """Segments of ``target`` from its shared audio with ``partners`` (pure)."""
    head, tail, duration = target_fp
    segments: list[MediaSegment] = []
    for kind, mine, index in (("intro", head, 0), ("outro", tail, 1)):
        candidates: list[tuple[float, float, float]] = []
        for _partner, fps in partners:
            run = best_shared_run(
                mine, fps[index], min_seconds=settings.min_seconds,
                max_seconds=settings.max_seconds, max_bit_error=settings.max_bit_error,
            )
            if run is not None:
                candidates.append((run.a_start, run.a_end, run.bit_error_rate))
        agreed = _consensus(candidates, settings.agree_seconds)
        if agreed is None:
            continue
        start, end, supporters, ber = agreed
        start, end = max(0.0, start), min(duration, end)
        if not (settings.min_seconds <= end - start <= settings.max_seconds):
            continue
        if any(s.start_seconds < end and start < s.end_seconds for s in segments):
            continue  # short file: the tail window saw the OP again
        segments.append(
            MediaSegment(
                kind=kind, start_seconds=round(start, 2), end_seconds=round(end, 2), source=SOURCE,
                confidence=_confidence(supporters, ber), media_fingerprint=target.identity,
            )
        )
    return segments


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
        confidence = min((float(s.get("confidence") or 0) for s in result.get("segments") or []), default=0.0)
        return new_partners and confidence < 0.9
    return True


def title_needs_analysis(
    files: Sequence[EpisodeFile], cache: IntroSkipperCache, settings: DetectorSettings, now: float
) -> bool:
    """Same partner choice as ``analyze_title``; files in an error back-off are left out."""
    results = {item.identity: cache.read_result(item.identity) for item in files}
    usable = [
        item for item in files
        if not (
            results[item.identity]
            and results[item.identity].get("status") == "error"
            and results[item.identity].get("algorithm") == ALGORITHM_VERSION
            and now < float(results[item.identity].get("retry_after") or 0)
        )
    ]
    if len(usable) < 2:
        return False
    return any(
        _needs_analysis(results[item.identity], {p.identity for p in _nearest(usable, item, settings.max_partners)}, now)
        for item in usable
    )


def _nearest(items: Sequence[EpisodeFile], target: EpisodeFile, limit: int) -> list[EpisodeFile]:
    others = [item for item in items if item.identity != target.identity]
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
) -> AnalysisReport:
    log = logger or logging.getLogger(__name__)
    report = AnalysisReport()
    ordered = sorted(files, key=lambda item: (item.episode, str(item.path)))
    fingerprints: dict[str, tuple[AudioFingerprint, AudioFingerprint, float]] = {}
    budget = max(0, int(max_new_fingerprints))
    stamp = now()

    def cancel() -> bool:
        return should_stop() is not None

    for item in ordered:
        reason = should_stop()
        if reason:
            report.stopped = reason
            return report
        existing = cache.read_result(item.identity)
        if existing and existing.get("status") == "error" and stamp < float(existing.get("retry_after") or 0):
            continue
        try:
            fps = _fingerprints_for(item, cache, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                    allow_compute=budget > 0, cancel_check=cancel)
        except FingerprintError as exc:
            if str(exc) == "cancelled":
                report.stopped = should_stop() or "cancelled"
                return report
            report.errors += 1
            log.info("FAIL step=intro_skipper.fingerprint path=%s error=%r", item.path.name, str(exc)[:200])
            cache.write_result(item.identity, {
                "algorithm": ALGORITHM_VERSION, "status": "error", "error": str(exc)[:300],
                "checked_at": stamp, "retry_after": stamp + ERROR_RETRY_SECONDS, "segments": [],
                "partners": [], "path": str(item.path),
            })
            continue
        if fps is None:
            continue
        if fps[3]:
            budget -= 1
            report.fingerprinted += 1
        fingerprints[item.identity] = fps[:3]

    ready = [item for item in ordered if item.identity in fingerprints]
    for item in ready:
        reason = should_stop()
        if reason:
            report.stopped = reason
            return report
        partners = _nearest(ready, item, settings.max_partners)
        if not partners:
            continue  # one episode alone proves nothing
        partner_ids = {p.identity for p in partners}
        if not _needs_analysis(cache.read_result(item.identity), partner_ids, stamp):
            continue
        segments = detect_segments(
            item, fingerprints[item.identity], [(p, fingerprints[p.identity]) for p in partners], settings,
        )
        report.analysed += 1
        status = "found" if segments else "empty"
        if segments:
            report.found += 1
        else:
            report.empty += 1
        cache.write_result(item.identity, {
            "algorithm": ALGORITHM_VERSION,
            "status": status,
            "segments": [asdict(segment) for segment in segments],
            "partners": sorted(partner_ids),
            "checked_at": stamp,
            "retry_after": stamp + EMPTY_RETRY_SECONDS if not segments else 0,
            "path": str(item.path),
            "episode": item.episode,
            "settings": asdict(settings),
        })
        log.info(
            "RESULT step=intro_skipper.detect episode=%s status=%s partners=%s segments=%s",
            item.episode, status, len(partners),
            ",".join(f"{s.kind}:{s.start_seconds:.1f}-{s.end_seconds:.1f}@{s.confidence}" for s in segments) or "-",
        )
    return report


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
) -> dict[str, Any]:
    """One low-priority step: pick one title that needs analysis and work on it."""
    from .work_scheduler import WorkPriority

    log = logger or logging.getLogger(__name__)
    if not enabled or os.environ.get("PUDGE_INTRO_DETECTION", "1").strip() == "0":
        return {"status": "disabled"}
    missing = tools_missing(ffmpeg, ffprobe)
    if missing:
        log.info("SKIP step=intro_skipper.pass reason=tool_missing tools=%s", ",".join(missing))
        return {"status": "tool_missing", "missing": missing}
    reason = scheduler.background_wait_reason()
    if reason is not None:
        return {"status": "deferred", "reason": reason}

    cache = IntroSkipperCache(cache_dir)
    incomplete = list(incomplete_paths)
    stamp = time.time()
    chosen: tuple[int, list[EpisodeFile]] | None = None
    animes = sorted(db.anime_list(), key=lambda anime: (anime.status != "CURRENT", anime.title.casefold()))
    for anime in animes:
        files = _ready_files(db, anime.media_id, incomplete)
        if len(files) < 2:
            continue
        if title_needs_analysis(files, cache, settings, stamp):
            chosen = (int(anime.media_id), files)
            break
    if chosen is None:
        return {"status": "idle"}

    lease = scheduler.acquire_heavy(
        "intro-skipper", blocking=False, foreground_sensitive=True,
        priority=WorkPriority.BACKGROUND, resource="cpu",
    )
    if lease is None:
        return {"status": "deferred", "reason": "heavy_busy"}

    def should_stop() -> str | None:
        if callable(cancel_check) and cancel_check():
            return "cancelled"
        wait = scheduler.background_wait_reason()
        if wait is not None:
            return str(wait)
        if scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND):
            return "higher_priority"
        return None

    media_id, files = chosen
    try:
        report = analyze_title(
            files, cache=cache, ffmpeg=_resolve(ffmpeg), ffprobe=_resolve(ffprobe), settings=settings,
            should_stop=should_stop, max_new_fingerprints=max_new_fingerprints, logger=log,
        )
    finally:
        lease.release()
    report.media_id = media_id
    _register_and_enforce(db, cache_dir, cache.written, log)
    log.info(
        "DONE step=intro_skipper.pass media_id=%s files=%s fingerprinted=%s analysed=%s found=%s empty=%s errors=%s stopped=%s",
        media_id, len(files), report.fingerprinted, report.analysed, report.found, report.empty,
        report.errors, report.stopped or "-",
    )
    return {"status": "stopped" if report.stopped else "ran", **asdict(report)}


def _register_and_enforce(db: Any, cache_dir: Path, written: Sequence[Path], log: logging.Logger) -> None:
    try:
        from .cache_registry import CachePolicy, CacheRegistry

        registry = CacheRegistry(db, Path(cache_dir))
        for path in dict.fromkeys(written):
            if Path(path).exists():
                registry.register(CACHE_CATEGORY, Path(path), metadata={"algorithm": ALGORITHM_VERSION})
        registry.enforce({CACHE_CATEGORY: CachePolicy(max_bytes=CACHE_MAX_BYTES, max_age_seconds=CACHE_MAX_AGE_SECONDS)})
    except Exception as exc:  # noqa: BLE001 - cache bookkeeping never breaks the agent
        log.warning("FAIL step=intro_skipper.cache_registry error=%r", str(exc)[:200])
