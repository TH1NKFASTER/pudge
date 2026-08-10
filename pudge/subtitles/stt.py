from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..subtitle_formats import write_srt


def _cache_key(video: Path, model: str) -> str:
    stat = video.stat()
    raw = f"stt-v1:{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{model}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def prepare_japanese_stt_reference(
    video: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str,
    model: str,
    timeout_seconds: float,
    force: bool = False,
) -> tuple[Path | None, dict[str, Any]]:
    """Create a cached, low-cost Japanese speech clock as a last resort.

    MLX Whisper is imported in a disposable worker only after every ordinary
    alignment path fails. The model and resulting SRT are cached, so subsequent
    candidates for the same video do not repeat transcription.
    """
    key = _cache_key(video, model)
    root = cache_dir / "stt" / key
    reference = root / "reference.ja.srt"
    metadata = root / "metadata.json"
    if not force and reference.is_file() and metadata.is_file():
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        return reference, {"available": True, "cache": "hit", **payload}

    root.mkdir(parents=True, exist_ok=True)
    python = os.getenv("PUDGE_PYTHON", "").strip() or sys.executable
    availability = subprocess.run(
        [python, "-m", "pudge.subtitles.stt_worker", "--check"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if availability.returncode != 0:
        return None, {
            "available": False,
            "reason": "stt_unavailable",
            "error": (availability.stderr or availability.stdout).strip()[-1000:],
        }
    audio = root / "audio.flac"
    result_path = root / "transcription.json"
    extract = subprocess.run(
        [
            ffmpeg_path,
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-compression_level",
            "8",
            str(audio),
        ],
        text=True,
        capture_output=True,
        timeout=min(max(60.0, timeout_seconds), 15 * 60),
    )
    if extract.returncode != 0 or not audio.is_file():
        audio.unlink(missing_ok=True)
        return None, {
            "available": False,
            "reason": "audio_extract_failed",
            "error": extract.stderr.strip()[-1000:],
        }

    try:
        completed = subprocess.run(
            [
                python,
                "-m",
                "pudge.subtitles.stt_worker",
                str(audio),
                str(result_path),
                model,
            ],
            text=True,
            capture_output=True,
            timeout=max(60.0, float(timeout_seconds)),
        )
    except subprocess.TimeoutExpired:
        audio.unlink(missing_ok=True)
        return None, {"available": False, "reason": "stt_timeout"}
    finally:
        audio.unlink(missing_ok=True)

    if completed.returncode != 0 or not result_path.is_file():
        return None, {
            "available": False,
            "reason": "stt_unavailable",
            "error": (completed.stderr or completed.stdout).strip()[-1000:],
        }
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
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
        text = str(segment.get("text") or "").strip()
        if text:
            cues.append((start, end, text))
    if len(cues) < 8:
        return None, {"available": False, "reason": "stt_too_few_segments", "segments": len(cues)}
    write_srt(cues, reference)
    info = {"model": model, "segments": len(cues), "language": "ja"}
    metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path.unlink(missing_ok=True)
    return reference, {"available": True, "cache": "miss", **info}
