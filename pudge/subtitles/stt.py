from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .. import audio_timeline
from ..fsutil import atomic_write_text
from ..subtitle_formats import write_srt


# Same MLX limits as audiobook STT: a whole episode otherwise peaks at 0.5-0.7 GB.
STT_MLX_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
STT_MLX_MEMORY_LIMIT_BYTES = 6 * 1024 * 1024 * 1024


def _stt_worker_environment(ffmpeg_path: str = "") -> dict[str, str]:
    environment = dict(os.environ)
    environment.setdefault("PUDGE_MLX_CACHE_LIMIT_BYTES", str(STT_MLX_CACHE_LIMIT_BYTES))
    environment.setdefault("PUDGE_MLX_MEMORY_LIMIT_BYTES", str(STT_MLX_MEMORY_LIMIT_BYTES))
    # MLX Whisper reads the audio by running "ffmpeg" from PATH. Pudge opened from the Dock
    # has no Homebrew folder on PATH (only the background agent has one), so the worker gets
    # the folder of the FFmpeg Pudge itself uses, as the audiobook worker does.
    found = ffmpeg_path if os.sep in ffmpeg_path else shutil.which(ffmpeg_path) if ffmpeg_path else None
    if found:
        folder = str(Path(found).resolve().parent)
        parts = [part for part in environment.get("PATH", "").split(os.pathsep) if part]
        if folder not in parts:
            environment["PATH"] = os.pathsep.join([folder, *parts])
    return environment


_WORKER_AVAILABILITY: dict[str, dict[str, Any]] = {}


def _worker_availability(python: str) -> dict[str, Any]:
    """Whether the MLX worker can run; checked once per process (it imports MLX)."""
    cached = _WORKER_AVAILABILITY.get(python)
    if cached is not None:
        return dict(cached)
    result = _check_worker_availability(python)
    if result.get("reason") != "stt_timeout":  # a slow first import may succeed later
        _WORKER_AVAILABILITY[python] = dict(result)
    return result


def _check_worker_availability(python: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [python, "-m", "pudge.subtitles.stt_worker", "--check"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "reason": "stt_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "stt_unavailable",
            "error": str(exc),
        }
    if completed.returncode:
        return {
            "available": False,
            "reason": "stt_unavailable" if completed.returncode == 3 else "stt_worker_failed",
            "error": (completed.stderr or completed.stdout).strip()[-1000:],
        }
    return {"available": True}


def japanese_stt_available(*, enabled: bool = True) -> bool:
    """Only invalidate prepared subtitles when the configured worker can run."""
    if not enabled:
        return False
    python = os.getenv("PUDGE_PYTHON", "").strip() or sys.executable
    return bool(_worker_availability(python)["available"])


def _sampled_content_fingerprint(path: Path, *, chunk_size: int = 128 * 1024) -> str:
    """Path-independent fingerprint sampled from the beginning/middle/end."""
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        for offset in sorted({0, max(0, size // 2 - chunk_size // 2), max(0, size - chunk_size)}):
            handle.seek(offset)
            digest.update(offset.to_bytes(8, "big", signed=False))
            digest.update(handle.read(chunk_size))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_japanese_audio_stream(video: Path, ffprobe_path: str | None) -> tuple[int | None, dict[str, Any]]:
    if not str(ffprobe_path or "").strip():
        return None, {"available": False, "selection_reason": "ffprobe_not_provided"}
    try:
        # Also the start times (audio_timeline): the same probe tells where the
        # selected track starts on the playback timeline.
        completed = subprocess.run(
            audio_timeline.probe_command(str(ffprobe_path), video),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {"available": False, "selection_reason": "ffprobe_failed", "error": str(exc)}
    if completed.returncode != 0:
        return None, {
            "available": False,
            "selection_reason": "ffprobe_failed",
            "error": (completed.stderr or completed.stdout).strip()[-1000:],
        }
    try:
        payload = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, {"available": False, "selection_reason": "ffprobe_invalid_json", "error": str(exc)}
    if not isinstance(payload, dict):
        return None, {
            "available": False,
            "selection_reason": "ffprobe_invalid_json",
            "error": "not an object",
        }
    timeline = audio_timeline.remember(video, payload)
    streams = [
        row
        for row in (payload.get("streams") or [])
        if isinstance(row, dict) and audio_timeline.is_audio_row(row)
    ]
    if not streams:
        return None, {"available": False, "selection_reason": "no_audio_streams"}

    def details(row: dict[str, Any], reason: str) -> tuple[int | None, dict[str, Any]]:
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            index = None
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        disposition = row.get("disposition") if isinstance(row.get("disposition"), dict) else {}
        position = streams.index(row)
        return index, {
            "available": index is not None,
            "index": index,
            # Position among the audio streams: ffmpeg's ``0:a:N`` specifier.
            "audio_position": position,
            # Leading silence before this track on the playback timeline (audio_timeline).
            "start_offset_seconds": timeline.delay(position=position) if timeline is not None else 0.0,
            "codec": str(row.get("codec_name") or ""),
            "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""),
            "default": bool(disposition.get("default")),
            "selection_reason": reason,
            "stream_count": len(streams),
        }

    japanese_languages = {"ja", "jpn", "japanese", "jp"}
    for row in streams:
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        language = str(tags.get("language") or "").strip().casefold()
        if language in japanese_languages or language.startswith("ja-"):
            return details(row, "japanese_language_tag")
    for row in streams:
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        title = str(tags.get("title") or "").casefold()
        if re.search(r"(?:japanese|日本語|日本|\bjpn\b|\bjp\b)", title):
            return details(row, "japanese_title_tag")
    if len(streams) == 1:
        return details(streams[0], "single_audio_stream_without_language_tag")
    return None, {
        "available": False,
        "selection_reason": "ambiguous_multiple_audio_streams",
        "stream_count": len(streams),
        "streams": [
            {
                "index": row.get("index"),
                "codec": str(row.get("codec_name") or ""),
                "language": str((row.get("tags") or {}).get("language") or "")
                if isinstance(row.get("tags"), dict)
                else "",
                "title": str((row.get("tags") or {}).get("title") or "")
                if isinstance(row.get("tags"), dict)
                else "",
                "default": bool((row.get("disposition") or {}).get("default"))
                if isinstance(row.get("disposition"), dict)
                else False,
            }
            for row in streams
        ],
    }


def _cache_key(
    content_fingerprint: str,
    model: str,
    audio_stream_index: int | None,
    start_offset: float = 0.0,
    gaps: tuple[tuple[float, float], ...] | None = None,
) -> str:
    raw = f"stt-v4-content-audio-text-clock:{content_fingerprint}:{model}:{audio_stream_index}"
    if start_offset:
        # Transcripts of a late-starting track made before it was padded are early.
        # (v1 is the version that padded them: those transcripts stay valid.)
        raw += f":playback-timeline-v1={start_offset:.3f}"
    # Transcripts of a track with gaps inside made before they were filled are early
    # after each gap.
    raw += audio_timeline.gaps_marker(gaps)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def prepare_japanese_stt_reference(
    video: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str,
    ffprobe_path: str | None = None,
    model: str,
    timeout_seconds: float,
    force: bool = False,
) -> tuple[Path | None, dict[str, Any]]:
    """Create a cached, low-cost Japanese speech clock as a last resort.

    MLX Whisper is imported in a disposable worker only after every ordinary
    alignment path fails. The model and resulting SRT are cached, so subsequent
    candidates for the same video do not repeat transcription.
    """
    audio_stream_index, audio_stream = _select_japanese_audio_stream(video, ffprobe_path)
    content_fingerprint = _sampled_content_fingerprint(video)
    if str(ffprobe_path or "").strip() and audio_stream_index is None:
        return None, {
            "available": False,
            "reason": "japanese_audio_stream_unavailable",
            "video_content_fingerprint": content_fingerprint,
            "audio_stream": audio_stream,
        }
    # Whisper's times count from the first sample of the FLAC: a track that starts
    # after the file start gets that much leading silence (audio_timeline).
    start_offset = (
        float(audio_stream.get("start_offset_seconds") or 0.0) if audio_stream_index is not None else 0.0
    )
    # A gap inside the track (a remux seam) would collapse in the FLAC: it is filled
    # (``delay_filter``), and transcripts made before that are not reused.
    gaps = (
        audio_timeline.inner_gaps(video, ffprobe_path, index=audio_stream_index)
        if audio_stream_index is not None
        else None
    )
    key = _cache_key(content_fingerprint, model, audio_stream_index, start_offset, gaps)
    root = cache_dir / "stt" / key
    reference = root / "reference.ja.srt"
    metadata = root / "metadata.json"
    if not force and reference.is_file() and metadata.is_file():
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        info = {
            "available": True,
            "cache": "hit",
            "video_content_fingerprint": content_fingerprint,
            "audio_stream": audio_stream,
            **(payload if isinstance(payload, dict) else {}),
        }
        # The files of this cache, not the paths recorded where the transcript was made (a
        # copied or moved cache): a step that opened the recorded word transcript found
        # nothing, and a cached run then took other steps than the run that made it.
        info["reference_path"] = str(reference)
        transcript = root / "transcription.json"
        if transcript.is_file():
            info["transcript_path"] = str(transcript)
        else:
            info.pop("transcript_path", None)
        return reference, info

    root.mkdir(parents=True, exist_ok=True)
    python = os.getenv("PUDGE_PYTHON", "").strip() or sys.executable
    availability = _worker_availability(python)
    if not availability["available"]:
        return None, availability
    # Names of this run only: the window and the agent can transcribe the same audio at once
    # (``ffmpeg -y`` rewrote the other's audio while its worker read it). The result takes
    # the shared name only when complete.
    run = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    audio = root / f"audio.{run}.flac"
    result_path = root / "transcription.json"
    run_result = root / f"transcription.{run}.json"
    try:
        extract = subprocess.run(
            [
                ffmpeg_path,
                "-v",
                "error",
                "-y",
                "-i",
                str(video),
                *(["-map", f"0:{audio_stream_index}"] if audio_stream_index is not None else []),
                "-vn",
                # Always on the playback timeline: a file without gaps or delay
                # decodes to the same samples.
                "-af",
                audio_timeline.delay_filter(start_offset),
                "-ac",
                "1",
                "-ar",
                "16000",
                # The FLAC is deleted right after transcription: no heavy compression.
                "-compression_level",
                "0",
                str(audio),
            ],
            text=True,
            capture_output=True,
            timeout=min(max(60.0, timeout_seconds), 15 * 60),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        audio.unlink(missing_ok=True)
        return None, {"available": False, "reason": "audio_extract_failed", "error": str(exc)}
    if extract.returncode != 0 or not audio.is_file():
        audio.unlink(missing_ok=True)
        return None, {
            "available": False,
            "reason": "audio_extract_failed",
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "error": extract.stderr.strip()[-1000:],
        }
    try:
        audio_sha256 = _file_sha256(audio)
        audio_bytes = audio.stat().st_size
    except OSError as exc:
        # The extracted FLAC is never left behind in the cache folder.
        audio.unlink(missing_ok=True)
        return None, {"available": False, "reason": "audio_extract_failed", "error": str(exc)}

    try:
        completed = subprocess.run(
            [
                python,
                "-m",
                "pudge.subtitles.stt_worker",
                "--words",
                str(audio),
                str(run_result),
                model,
            ],
            text=True,
            capture_output=True,
            timeout=max(60.0, float(timeout_seconds)),
            env=_stt_worker_environment(ffmpeg_path),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        audio.unlink(missing_ok=True)
        run_result.unlink(missing_ok=True)
        return None, {
            "available": False,
            "reason": "stt_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "stt_worker_failed",
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
        }
    finally:
        audio.unlink(missing_ok=True)

    if completed.returncode != 0 or not run_result.is_file():
        run_result.unlink(missing_ok=True)
        return None, {
            "available": False,
            "reason": "stt_unavailable" if completed.returncode == 3 else "stt_worker_failed",
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
            "error": (completed.stderr or completed.stdout).strip()[-1000:],
        }
    try:
        # Read before it takes the shared name: an unreadable result must not replace the
        # word transcript an earlier (valid) cache entry of this folder points at.
        payload = json.loads(run_result.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        run_result.unlink(missing_ok=True)
        return None, {"available": False, "reason": "stt_invalid_result", "error": str(exc)}
    segments = payload.get("segments") if isinstance(payload, dict) else None
    cues: list[tuple[float, float, str]] = []
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        try:
            start = max(0.0, float(segment.get("start") or 0.0))
            end = max(start + 0.05, float(segment.get("end") or start + 0.05))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            continue  # an infinite time cannot be written as an SRT timestamp
        text = str(segment.get("text") or "").strip()
        if text:
            cues.append((start, end, text))
    if len(cues) < 8:
        run_result.unlink(missing_ok=True)
        return None, {
            "available": False,
            "reason": "stt_too_few_segments",
            "segments": len(cues),
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
        }
    try:
        os.replace(run_result, result_path)
    except OSError as exc:
        run_result.unlink(missing_ok=True)
        return None, {"available": False, "reason": "stt_invalid_result", "error": str(exc)}
    write_srt(cues, reference)
    reference_sha256 = _file_sha256(reference)
    info = {
        "model": model,
        "segments": len(cues),
        "language": "ja",
        "word_timestamps": True,
        "transcript_path": str(result_path),
        "reference_path": str(reference),
        "reference_sha256": reference_sha256,
        "video_content_fingerprint": content_fingerprint,
        "audio_stream": audio_stream,
        "audio_sha256": audio_sha256,
        "audio_bytes": audio_bytes,
        "audio_start_offset_seconds": start_offset,
    }
    # Atomic: a cut-off write left a reference whose cache hit had lost its metadata.
    atomic_write_text(metadata, json.dumps(info, ensure_ascii=False, indent=2))
    return reference, {"available": True, "cache": "miss", **info}
