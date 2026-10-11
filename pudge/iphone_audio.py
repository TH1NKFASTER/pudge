"""Prepare immutable, native-compatible audio copies; originals remain untouched."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from .iphone_service import digest


def probe(path: Path, *, ffprobe: str = "ffprobe") -> dict:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    value = json.loads(result.stdout)
    audio = next((s for s in value.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("No audio stream")
    duration = float(value.get("format", {}).get("duration") or audio.get("duration") or 0)
    if not 0 < duration < 365 * 86400:
        raise ValueError("Invalid audio duration")
    return {"durationMs": round(duration * 1000), "codec": audio["codec_name"]}


def prepare_audio(
    source: Path,
    root: Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    original_sha: str | None = None,
) -> dict:
    """Return the native-playable resource for one source file.

    A natively playable original is served in place (no second copy of the
    library on disk); its hash is bound to the manifest, so a later change of
    the file yields a new edition instead of mixed bytes. Only incompatible
    codecs get an immutable AAC variant under ``root``.
    """
    root.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    original_sha = original_sha or digest(source)
    info = probe(source, ffprobe=ffprobe)
    direct = (
        (source.suffix.lower() in {".mp3"} and info["codec"] == "mp3")
        or (source.suffix.lower() in {".m4a", ".m4b", ".aac"} and info["codec"] in {"aac", "alac"})
        or (source.suffix.lower() == ".wav" and info["codec"].startswith("pcm_"))
    )
    if direct:
        return {
            "path": source,
            "originalSHA256": original_sha,
            "sha256": original_sha,
            "canonicalDurationMs": info["durationMs"],
            "playbackDurationMs": info["durationMs"],
            "sizeBytes": before.st_size,
        }
    cache = root / (original_sha + "-aac-v1.m4a")
    if not cache.exists():
        temporary = root / (str(uuid.uuid4()) + ".m4a")
        try:
            subprocess.run(
                [
                    str(ffmpeg),
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "0",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                timeout=12 * 3600,
                check=True,
                capture_output=True,
            )
            after = source.stat()
            if after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size:
                raise ValueError("Source changed during preparation")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
    playback = probe(cache, ffprobe=ffprobe)
    if abs(playback["durationMs"] - info["durationMs"]) > 250:
        raise ValueError("Prepared audio timeline is incompatible")
    return {
        "path": cache,
        "originalSHA256": original_sha,
        "sha256": digest(cache),
        "canonicalDurationMs": info["durationMs"],
        "playbackDurationMs": playback["durationMs"],
        "sizeBytes": cache.stat().st_size,
    }
