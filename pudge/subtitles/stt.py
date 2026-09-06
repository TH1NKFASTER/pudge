from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..subtitle_formats import write_srt


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


def _select_japanese_audio_stream(
    video: Path, ffprobe_path: str | None
) -> tuple[int | None, dict[str, Any]]:
    if not str(ffprobe_path or "").strip():
        return None, {"available": False, "selection_reason": "ffprobe_not_provided"}
    try:
        completed = subprocess.run(
            [
                str(ffprobe_path), "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=default",
                "-of", "json", str(video),
            ],
            text=True, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, {"available": False, "selection_reason": "ffprobe_failed", "error": str(exc)}
    if completed.returncode != 0:
        return None, {
            "available": False, "selection_reason": "ffprobe_failed",
            "error": (completed.stderr or completed.stdout).strip()[-1000:],
        }
    try:
        payload = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, {"available": False, "selection_reason": "ffprobe_invalid_json", "error": str(exc)}
    streams = [row for row in (payload.get("streams") or []) if isinstance(row, dict)]
    if not streams:
        return None, {"available": False, "selection_reason": "no_audio_streams"}

    def details(row: dict[str, Any], reason: str) -> tuple[int | None, dict[str, Any]]:
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            index = None
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        disposition = row.get("disposition") if isinstance(row.get("disposition"), dict) else {}
        return index, {
            "available": index is not None,
            "index": index,
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
                if isinstance(row.get("tags"), dict) else "",
                "title": str((row.get("tags") or {}).get("title") or "")
                if isinstance(row.get("tags"), dict) else "",
                "default": bool((row.get("disposition") or {}).get("default"))
                if isinstance(row.get("disposition"), dict) else False,
            }
            for row in streams
        ],
    }


def _cache_key(content_fingerprint: str, model: str, audio_stream_index: int | None) -> str:
    raw = (
        f"stt-v4-content-audio-text-clock:{content_fingerprint}:"
        f"{model}:{audio_stream_index}"
    )
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
    key = _cache_key(content_fingerprint, model, audio_stream_index)
    root = cache_dir / "stt" / key
    reference = root / "reference.ja.srt"
    metadata = root / "metadata.json"
    if not force and reference.is_file() and metadata.is_file():
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        return reference, {
            "available": True, "cache": "hit",
            "video_content_fingerprint": content_fingerprint,
            "audio_stream": audio_stream,
            **payload,
        }

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
            *(["-map", f"0:{audio_stream_index}"] if audio_stream_index is not None else []),
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
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "error": extract.stderr.strip()[-1000:],
        }
    audio_sha256 = _file_sha256(audio)
    audio_bytes = audio.stat().st_size

    try:
        completed = subprocess.run(
            [
                python,
                "-m",
                "pudge.subtitles.stt_worker",
                "--words",
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
        return None, {
            "available": False, "reason": "stt_timeout",
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
        }
    finally:
        audio.unlink(missing_ok=True)

    if completed.returncode != 0 or not result_path.is_file():
        return None, {
            "available": False,
            "reason": "stt_unavailable",
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
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
        return None, {
            "available": False, "reason": "stt_too_few_segments", "segments": len(cues),
            "audio_stream": audio_stream,
            "video_content_fingerprint": content_fingerprint,
            "audio_sha256": audio_sha256,
            "audio_bytes": audio_bytes,
        }
    write_srt(cues, reference)
    reference_sha256 = _file_sha256(reference)
    info = {
        "model": model, "segments": len(cues), "language": "ja", "word_timestamps": True,
        "transcript_path": str(result_path), "reference_path": str(reference),
        "reference_sha256": reference_sha256,
        "video_content_fingerprint": content_fingerprint,
        "audio_stream": audio_stream,
        "audio_sha256": audio_sha256,
        "audio_bytes": audio_bytes,
    }
    metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return reference, {"available": True, "cache": "miss", **info}
