from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .branding import APP_SLUG
from typing import Any

from .language import (
    JAPANESE_LANGUAGE_CODES,
    has_japanese_marker,
    has_negative_language_marker,
    japanese_text_metrics,
    normalize_language_code,
)
from .models import EmbeddedSubtitle


TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}


class MediaProbeError(RuntimeError):
    pass


def probe_media(video: Path, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(video),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(completed.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        raise MediaProbeError(f"Не удалось прочитать контейнер через ffprobe: {exc}") from exc


def _extract_text_sample(video: Path, stream_index: int, ffmpeg: str) -> str:
    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-") as temp_dir:
        output = Path(temp_dir) / "sample.srt"
        command = [
            ffmpeg,
            "-v", "error",
            "-y",
            "-nostdin",
            "-probesize", "10M",
            "-analyzeduration", "10000000",
            "-i", str(video),
            "-map", f"0:{stream_index}",
            "-t", "600",
            "-c:s", "srt",
            str(output),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=45)
            return output.read_text(encoding="utf-8", errors="replace")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return ""


def find_embedded_japanese_subtitles(
    video: Path,
    ffprobe: str,
    ffmpeg: str,
    verbose: bool = False,
) -> list[EmbeddedSubtitle]:
    info = probe_media(video, ffprobe)
    subtitle_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    candidates: list[EmbeddedSubtitle] = []

    for subtitle_id, stream in enumerate(subtitle_streams, start=1):
        tags = stream.get("tags") or {}
        language = normalize_language_code(str(tags.get("language", "")))
        title = str(tags.get("title", ""))
        codec = str(stream.get("codec_name", "")).casefold()
        score = 0.0
        detected_from_text = False

        if language in JAPANESE_LANGUAGE_CODES:
            score += 120
        if has_japanese_marker(title):
            score += 70
        if has_negative_language_marker(title) and not has_japanese_marker(title):
            score -= 100

        lowered_title = title.casefold()
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        # Subtitle-track ranking adapted from SubPlz's pragmatic stream scorer.
        # For Pudge a signs/forced-only Japanese track is worse than no full
        # Japanese track: accepting it would make an episode look playback-ready.
        if any(word in lowered_title for word in ("commentary", "comment", "comms")) or int(disposition.get("comment", 0) or 0):
            score -= 200
        if any(word in lowered_title for word in ("sign", "song", "karaoke", "forced")) or int(disposition.get("forced", 0) or 0):
            score -= 110
        if any(word in lowered_title for word in ("dialog", "dialogue", "full")):
            score += 20
        if int(disposition.get("default", 0) or 0):
            score += 10

        # For unlabelled text streams, inspect a sample. This is deliberately skipped
        # for bitmap subtitles because OCR would be required.
        if score < 60 and codec in TEXT_CODECS:
            sample = _extract_text_sample(video, int(stream["index"]), ffmpeg)
            metrics = japanese_text_metrics(sample)
            if metrics["detected"]:
                score += 90
                detected_from_text = True

        if verbose:
            print(
                f"  embedded sid={subtitle_id} codec={codec} lang={language or '-'} "
                f"title={title or '-'} score={score:.1f}"
            )

        if score >= 60:
            candidates.append(
                EmbeddedSubtitle(
                    stream_index=int(stream["index"]),
                    subtitle_id=subtitle_id,
                    codec=codec,
                    language=language,
                    title=title,
                    score=score,
                    detected_from_text=detected_from_text,
                )
            )

    return sorted(
        candidates,
        key=lambda item: (item.score, item.codec in TEXT_CODECS),
        reverse=True,
    )


def find_embedded_japanese_subtitle(
    video: Path,
    ffprobe: str,
    ffmpeg: str,
    verbose: bool = False,
    text_only: bool = False,
) -> EmbeddedSubtitle | None:
    candidates = find_embedded_japanese_subtitles(
        video,
        ffprobe,
        ffmpeg,
        verbose=verbose,
    )
    if text_only:
        candidates = [candidate for candidate in candidates if candidate.codec in TEXT_CODECS]
    return candidates[0] if candidates else None
