from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import AppConfig
from ..language import JAPANESE_LANGUAGE_CODES, has_japanese_marker, normalize_language_code
from ..media import TEXT_CODECS, find_embedded_japanese_subtitles, probe_media
from ..models import SubtitleCandidate, VideoIdentity
from ..ocr import image_subtitle_to_srt
from ..providers.jimaku import JimakuClient, JimakuError, materialize_jimaku_files
from ..subtitle_formats import convert_to_plain_srt
from ..syncing import extract_embedded_timing_reference, optimize_candidates, subtitle_quality_accepted
from .benchmark import BENCHMARK_SCHEMA, evaluate_subtitle_files, report_case_id, write_json


CORPUS_CASE_SCHEMA = "pudge-subtitle-benchmark-case-v1"
DIAGNOSTIC_CASE_SCHEMA = "pudge-subtitle-benchmark-diagnostic-v1"
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".webm",
    ".ts",
    ".m2ts",
    ".mts",
    ".wmv",
    ".flv",
    ".ogv",
    ".mpeg",
    ".mpg",
}


class SubtitleBenchmarkError(RuntimeError):
    pass


def _safe_component(value: str, *, limit: int = 120) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value or "")).strip(" .")
    return (value[:limit] or "case").strip()


def _run(command: list[str], *, timeout: float, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubtitleBenchmarkError(f"{label} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stdout[-2500:].strip()
        raise SubtitleBenchmarkError(f"{label} failed with code {completed.returncode}: {detail}")
    return completed


def select_benchmark_audio_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
    streams = [
        stream
        for stream in probe.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not streams:
        return None

    def score(stream: dict[str, Any]) -> tuple[float, int]:
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        language = normalize_language_code(str(tags.get("language", "")))
        title = str(tags.get("title", ""))
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        value = 0.0
        if language in JAPANESE_LANGUAGE_CODES:
            value += 100.0
        if has_japanese_marker(title):
            value += 40.0
        if bool(disposition.get("default")):
            value += 5.0
        channels = int(stream.get("channels") or 0)
        if channels in {1, 2}:
            value += 1.0
        return value, -int(stream.get("index") or 0)

    return max(streams, key=score)


def extract_embedded_japanese_oracle(
    video: Path,
    destination: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> dict[str, object]:
    video = Path(video)
    destination = Path(destination)
    candidates = [
        candidate
        for candidate in find_embedded_japanese_subtitles(video, ffprobe_path, ffmpeg_path)
        if candidate.codec in TEXT_CODECS
    ]
    if not candidates:
        raise SubtitleBenchmarkError("video has no embedded Japanese text subtitle")
    selected = candidates[0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp.srt")
    temporary.unlink(missing_ok=True)
    _run(
        [
            ffmpeg_path,
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            f"0:{selected.stream_index}",
            "-c:s",
            "srt",
            str(temporary),
        ],
        timeout=120,
        label="oracle subtitle extraction",
    )
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise SubtitleBenchmarkError("embedded Japanese subtitle extracted to an empty file")
    temporary.replace(destination)
    return {
        "stream_index": selected.stream_index,
        "subtitle_id": selected.subtitle_id,
        "codec": selected.codec,
        "language": selected.language,
        "title": selected.title,
        "score": selected.score,
        "detected_from_text": selected.detected_from_text,
        "path": str(destination),
        "bytes": destination.stat().st_size,
    }


def build_compact_benchmark_media(
    video: Path,
    destination: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    audio_bitrate: str = "24k",
) -> dict[str, object]:
    """Create a tiny replayable media file without leaking the Japanese oracle.

    The proxy contains only one Japanese/most-likely-Japanese audio stream and,
    when available, the non-Japanese embedded timing reference that production
    Pudge itself is allowed to use.  No Japanese subtitle stream is copied.
    """

    video = Path(video)
    destination = Path(destination)
    probe = probe_media(video, ffprobe_path)
    audio = select_benchmark_audio_stream(probe)
    if audio is None:
        raise SubtitleBenchmarkError("video has no audio stream")
    try:
        audio_index = int(audio["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SubtitleBenchmarkError("selected audio stream has no valid index") from exc

    timing_reference, timing_result = extract_embedded_timing_reference(
        video,
        Path(cache_dir),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=False,
        verbose=False,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp.mkv")
    temporary.unlink(missing_ok=True)

    command = [ffmpeg_path, "-y", "-v", "error", "-i", str(video)]
    if timing_reference is not None:
        command.extend(["-i", str(timing_reference)])
    command.extend(
        [
            "-map",
            f"0:{audio_index}",
            "-map_chapters",
            "0",
            "-map_metadata",
            "0",
            "-vn",
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-b:a",
            str(audio_bitrate),
            "-ac",
            "1",
            "-ar",
            "16000",
        ]
    )
    if timing_reference is not None:
        command.extend(
            [
                "-map",
                "1:0",
                "-c:s",
                "srt",
                "-metadata:s:s:0",
                "language=eng",
                "-metadata:s:s:0",
                "title=Pudge benchmark timing reference",
            ]
        )
    command.append(str(temporary))
    _run(command, timeout=900, label="compact benchmark media creation")
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise SubtitleBenchmarkError("compact benchmark media is empty")
    temporary.replace(destination)

    tags = audio.get("tags") if isinstance(audio.get("tags"), dict) else {}
    source_size = video.stat().st_size
    target_size = destination.stat().st_size
    return {
        "path": str(destination),
        "source_path": str(video),
        "source_bytes": source_size,
        "compact_bytes": target_size,
        "compression_ratio": round(target_size / max(1, source_size), 5),
        "audio_stream_index": audio_index,
        "audio_language": str(tags.get("language", "")),
        "audio_title": str(tags.get("title", "")),
        "audio_codec": str(audio.get("codec_name", "")),
        "audio_bitrate": str(audio_bitrate),
        "timing_reference": dict(timing_result),
        "timing_reference_included": timing_reference is not None,
    }


def discover_jimaku_benchmark_candidates(
    *,
    client: JimakuClient,
    video: Path,
    cache_dir: Path,
    media_id: int | None,
    title: str,
    episode: int | None,
    prefer_srt: bool = True,
    max_entries: int = 4,
) -> tuple[list[SubtitleCandidate], dict[str, object]]:
    """Materialize broad Jimaku coverage for one benchmark case.

    This intentionally evaluates more than production's final score threshold:
    the corpus needs both plausible candidates and hard negatives in order to
    measure candidate-selection errors rather than only alignment quality.
    """

    identity = VideoIdentity(title=title, episode=episode, raw_name=Path(video).name)
    entries = []
    if media_id is not None:
        try:
            entries.extend(client.search_entries(anilist_id=int(media_id)))
        except JimakuError:
            pass
    if title:
        try:
            entries.extend(client.search_entries(query=title))
        except JimakuError:
            if not entries:
                raise
    deduplicated = {entry.id: entry for entry in entries}
    ranked_entries = client.rank_entries(list(deduplicated.values()), identity, media_id)

    candidates: list[SubtitleCandidate] = []
    seen_urls: set[str] = set()
    entry_payloads: list[dict[str, object]] = []
    for entry in ranked_entries[: max(1, int(max_entries))]:
        files = client.files_for_episode(entry.id, episode)
        ranked_files = client.rank_files(files, identity, Path(video), prefer_srt=prefer_srt)
        file_payloads: list[dict[str, object]] = []
        for item in ranked_files:
            file_payloads.append(
                {
                    "name": item.name,
                    "url": item.url,
                    "score": item.score,
                    "details": dict(item.details),
                }
            )
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            item.details.update(
                {
                    "entry_id": entry.id,
                    "entry_name": entry.name,
                    "entry_anilist_id": entry.anilist_id,
                    "requested_anilist_id": media_id,
                }
            )
            candidates.extend(
                materialize_jimaku_files(
                    client,
                    item,
                    identity,
                    Path(video),
                    Path(cache_dir),
                    prefer_srt=prefer_srt,
                )
            )
        entry_payloads.append(
            {
                "id": entry.id,
                "name": entry.name,
                "english_name": entry.english_name,
                "japanese_name": entry.japanese_name,
                "anilist_id": entry.anilist_id,
                "files": file_payloads,
            }
        )
    return candidates, {
        "entries": entry_payloads,
        "materialized_candidates": len(candidates),
        "unique_urls": len(seen_urls),
    }


def _candidate_plain_srt(
    candidate: SubtitleCandidate,
    case_cache: Path,
    config: AppConfig,
    *,
    media: Path | None = None,
) -> tuple[Path | None, dict[str, object]]:
    suffix = candidate.path.suffix.casefold()
    if suffix == ".srt":
        return candidate.path, {"reason": "native_text"}
    if suffix in {".ass", ".ssa"}:
        converted, result = convert_to_plain_srt(
            candidate.path,
            case_cache,
            ffmpeg_path=config.tools.ffmpeg,
            force=False,
            verbose=False,
        )
        if converted.suffix.casefold() == ".srt" and converted.is_file():
            return converted, {"reason": "converted_text", "result": result}
        return None, {"reason": "text_conversion_failed", "result": result}
    if suffix in {".sup", ".pgs"}:
        if media is None:
            return None, {"reason": "bitmap_ocr_needs_media_context"}
        try:
            converted, result = image_subtitle_to_srt(
                Path(media),
                case_cache,
                subtitle_path=candidate.path,
                ffmpeg_path=config.tools.ffmpeg,
                force=False,
            )
        except Exception as exc:  # benchmark must keep the rest of the corpus usable
            return None, {"reason": "bitmap_ocr_failed", "error": f"{type(exc).__name__}: {exc}"}
        if converted is not None and converted.is_file():
            return converted, {"reason": "bitmap_ocr", "result": result}
        return None, {"reason": "bitmap_ocr_unavailable", "result": result}
    return None, {"reason": "unsupported_candidate_format", "format": suffix}


def evaluate_materialized_candidates(
    candidates: Iterable[SubtitleCandidate],
    oracle: Path,
    case_cache: Path,
    config: AppConfig,
    *,
    media: Path | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates):
        plain, preprocessing = _candidate_plain_srt(candidate, case_cache, config, media=media)
        report = None
        if plain is not None and plain.is_file():
            report = evaluate_subtitle_files(plain, oracle).as_dict()
        rows.append(
            {
                "index": index,
                "name": candidate.name,
                "source": candidate.source,
                "score": candidate.score,
                "path": str(candidate.path),
                "plain_srt": str(plain) if plain is not None else None,
                "preprocessing": preprocessing,
                "episode": candidate.episode,
                "verified_japanese": candidate.verified_japanese,
                "details": dict(candidate.details),
                "oracle": report,
            }
        )
    return rows


def run_current_pudge_benchmark(
    *,
    media: Path,
    candidates: list[SubtitleCandidate],
    oracle: Path,
    cache_dir: Path,
    config: AppConfig,
    force: bool = False,
) -> dict[str, object]:
    started = time.monotonic()
    selected, final_path, result = optimize_candidates(
        Path(media),
        candidates,
        Path(cache_dir),
        config.sync,
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
        alass_path=config.tools.alass,
        force=force,
        verbose=False,
        prefer_srt=config.matching.prefer_srt,
        srt_tolerance_ratio=config.matching.srt_alignment_tolerance_ratio,
        srt_tolerance_absolute=config.matching.srt_alignment_tolerance_absolute,
    )
    accepted, quality_reason = subtitle_quality_accepted(result)
    oracle_report = None
    if final_path is not None and Path(final_path).suffix.casefold() == ".srt" and Path(final_path).is_file():
        oracle_report = evaluate_subtitle_files(Path(final_path), Path(oracle)).as_dict()
    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "selected": {
            "name": selected.name,
            "source": selected.source,
            "score": selected.score,
            "path": str(selected.path),
            "details": dict(selected.details),
        }
        if selected is not None
        else None,
        "final_path": str(final_path) if final_path is not None else None,
        "accepted": bool(accepted),
        "quality_reason": quality_reason,
        "production_result": result,
        "oracle": oracle_report,
        "false_accept": bool(
            accepted
            and (
                oracle_report is None
                or not bool(oracle_report.get("same_episode"))
                or not bool(oracle_report.get("good_alignment"))
            )
        ),
        "false_reject": bool(
            not accepted
            and oracle_report is not None
            and bool(oracle_report.get("same_episode"))
            and bool(oracle_report.get("good_alignment"))
        ),
    }


def create_case_from_video(
    *,
    video: Path,
    corpus_dir: Path,
    config: AppConfig,
    media_id: int | None,
    title: str,
    episode: int | None,
    fetch_jimaku: bool = True,
    run_pudge: bool = True,
) -> Path:
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise SubtitleBenchmarkError(f"video not found: {video}")
    case_id = report_case_id(media_id=media_id, episode=episode, release_name=video.name)
    case_dir = Path(corpus_dir).expanduser() / "cases" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = case_dir / "cache"
    oracle = case_dir / "oracle.srt"

    try:
        oracle_meta = extract_embedded_japanese_oracle(
            video,
            oracle,
            ffmpeg_path=config.tools.ffmpeg,
            ffprobe_path=config.tools.ffprobe,
        )
    except SubtitleBenchmarkError:
        # A no-oracle source is immediately re-materialized under diagnostics/.
        # Do not leave thousands of empty cases/<id>/ directories behind.
        if not (case_dir / "case.json").exists():
            shutil.rmtree(case_dir, ignore_errors=True)
        raise
    compact_name = _safe_component(video.stem, limit=180) + ".mkv"
    compact = case_dir / compact_name
    media_meta = build_compact_benchmark_media(
        video,
        compact,
        cache_dir,
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
    )

    candidates: list[SubtitleCandidate] = []
    jimaku_meta: dict[str, object] = {"skipped": True}
    if fetch_jimaku:
        client = JimakuClient(config.jimaku.base_url, config.jimaku.api_key, cache_dir=config.paths.cache_dir)
        try:
            candidates, jimaku_meta = discover_jimaku_benchmark_candidates(
                client=client,
                video=compact,
                cache_dir=case_dir / "jimaku-cache",
                media_id=media_id,
                title=title,
                episode=episode,
                prefer_srt=config.matching.prefer_srt,
            )
        finally:
            client.close()

    candidate_rows = evaluate_materialized_candidates(candidates, oracle, cache_dir, config, media=compact)
    replay = None
    if run_pudge and candidates:
        replay = run_current_pudge_benchmark(
            media=compact,
            candidates=candidates,
            oracle=oracle,
            cache_dir=case_dir / "pudge-cache",
            config=config,
        )
        if isinstance(replay, dict) and isinstance(replay.get("selected"), dict):
            selected_path = str(replay["selected"].get("path") or "")
            selected_name = str(replay["selected"].get("name") or "")
            selected_row = next(
                (
                    row
                    for row in candidate_rows
                    if str(row.get("path") or "") == selected_path
                    or (selected_name and str(row.get("name") or "") == selected_name)
                ),
                None,
            )
            replay["selected_candidate_oracle"] = (
                selected_row.get("oracle") if isinstance(selected_row, dict) else None
            )
            any_same_episode = any(
                isinstance(row.get("oracle"), dict) and bool(row["oracle"].get("same_episode"))
                for row in candidate_rows
            )
            selected_same_episode = bool(
                isinstance(replay.get("selected_candidate_oracle"), dict)
                and replay["selected_candidate_oracle"].get("same_episode")
            )
            final_oracle = replay.get("oracle") if isinstance(replay.get("oracle"), dict) else {}
            replay["candidate_miss"] = not any_same_episode
            replay["selection_failure"] = bool(any_same_episode and not selected_same_episode)
            replay["alignment_failure"] = bool(
                selected_same_episode
                and isinstance(final_oracle, dict)
                and final_oracle.get("same_episode")
                and not final_oracle.get("good_alignment")
            )

    payload = {
        "schema": CORPUS_CASE_SCHEMA,
        "benchmark_schema": BENCHMARK_SCHEMA,
        "case_id": case_id,
        "created_at": time.time(),
        "media_id": media_id,
        "title": title,
        "episode": episode,
        "source_release_name": video.name,
        "source_extension": video.suffix.casefold(),
        "oracle": oracle_meta,
        "compact_media": media_meta,
        "jimaku": jimaku_meta,
        "candidates": candidate_rows,
        "current_pudge": replay,
    }
    write_json(case_dir / "case.json", payload)
    return case_dir



def _extract_embedded_text_tracks(
    video: Path,
    destination: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    """Preserve every embedded text subtitle for no-oracle forensic cases."""
    probe = probe_media(Path(video), ffprobe_path)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for stream in probe.get("streams", []):
        if not isinstance(stream, dict) or stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").casefold()
        if codec not in TEXT_CODECS:
            continue
        try:
            index = int(stream.get("index"))
        except (TypeError, ValueError):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        language = str(tags.get("language") or "und")
        title = str(tags.get("title") or "")
        target = destination / f"stream-{index:02d}-{_safe_component(language, limit=16)}.srt"
        result: dict[str, object] = {
            "stream_index": index,
            "codec": codec,
            "language": language,
            "title": title,
            "path": str(target),
        }
        try:
            _run(
                [
                    ffmpeg_path,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(video),
                    "-map",
                    f"0:{index}",
                    "-c:s",
                    "srt",
                    str(target),
                ],
                timeout=120,
                label=f"embedded text subtitle extraction stream {index}",
            )
            result["bytes"] = target.stat().st_size if target.is_file() else 0
        except SubtitleBenchmarkError as exc:
            target.unlink(missing_ok=True)
            result["path"] = None
            result["error"] = str(exc)
        rows.append(result)
    return probe, rows


def create_diagnostic_case_from_video(
    *,
    video: Path,
    corpus_dir: Path,
    config: AppConfig,
    media_id: int | None,
    title: str,
    episode: int | None,
    reason: str,
    source_release: dict[str, object] | None = None,
    fetch_jimaku: bool = True,
    run_pudge: bool = True,
) -> Path:
    """Preserve a useful no-oracle case instead of throwing the download away.

    The original video can be deleted after this returns.  We keep compact
    audio/timing media, all embedded text tracks, Jimaku materializations and
    the production alignment diagnostics.  This is intentionally not counted
    as GOLD because there is no embedded Japanese text oracle.
    """
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise SubtitleBenchmarkError(f"video not found: {video}")
    case_id = report_case_id(media_id=media_id, episode=episode, release_name=video.name)
    case_dir = Path(corpus_dir).expanduser() / "diagnostics" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    payload_path = case_dir / "diagnostic.json"
    try:
        existing = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = None
    if isinstance(existing, dict) and existing.get("schema") == DIAGNOSTIC_CASE_SCHEMA:
        return case_dir

    cache_dir = case_dir / "cache"
    probe, embedded_tracks = _extract_embedded_text_tracks(
        video,
        case_dir / "embedded-text",
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
    )
    write_json(case_dir / "ffprobe.json", probe)

    compact_name = _safe_component(video.stem, limit=180) + ".mkv"
    compact = case_dir / compact_name
    media_meta = build_compact_benchmark_media(
        video,
        compact,
        cache_dir,
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
    )

    candidates: list[SubtitleCandidate] = []
    jimaku_meta: dict[str, object] = {"skipped": True}
    if fetch_jimaku:
        client = JimakuClient(config.jimaku.base_url, config.jimaku.api_key, cache_dir=config.paths.cache_dir)
        try:
            candidates, jimaku_meta = discover_jimaku_benchmark_candidates(
                client=client,
                video=compact,
                cache_dir=case_dir / "jimaku-cache",
                media_id=media_id,
                title=title,
                episode=episode,
                prefer_srt=config.matching.prefer_srt,
            )
        finally:
            client.close()

    candidate_rows: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates):
        plain, preprocessing = _candidate_plain_srt(candidate, cache_dir, config, media=compact)
        candidate_rows.append(
            {
                "index": index,
                "name": candidate.name,
                "source": candidate.source,
                "score": candidate.score,
                "path": str(candidate.path),
                "plain_srt": str(plain) if plain is not None else None,
                "preprocessing": preprocessing,
                "episode": candidate.episode,
                "verified_japanese": candidate.verified_japanese,
                "details": dict(candidate.details),
            }
        )

    replay: dict[str, object] | None = None
    if run_pudge and candidates:
        started = time.monotonic()
        try:
            selected, final_path, result = optimize_candidates(
                compact,
                candidates,
                case_dir / "pudge-cache",
                config.sync,
                ffmpeg_path=config.tools.ffmpeg,
                ffprobe_path=config.tools.ffprobe,
                alass_path=config.tools.alass,
                force=False,
                verbose=False,
                prefer_srt=config.matching.prefer_srt,
                srt_tolerance_ratio=config.matching.srt_alignment_tolerance_ratio,
                srt_tolerance_absolute=config.matching.srt_alignment_tolerance_absolute,
            )
            accepted, quality_reason = subtitle_quality_accepted(result)
            replay = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "selected": {
                    "name": selected.name,
                    "source": selected.source,
                    "score": selected.score,
                    "path": str(selected.path),
                    "details": dict(selected.details),
                } if selected is not None else None,
                "final_path": str(final_path) if final_path is not None else None,
                "accepted": bool(accepted),
                "quality_reason": quality_reason,
                "production_result": result,
            }
        except Exception as exc:
            replay = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }

    payload = {
        "schema": DIAGNOSTIC_CASE_SCHEMA,
        "tier": "diagnostic",
        "created_at": time.time(),
        "media_id": media_id,
        "title": title,
        "episode": episode,
        "reason": str(reason),
        "source_release_name": video.name,
        "source_release": dict(source_release or {}),
        "source_extension": video.suffix.casefold(),
        "embedded_text_tracks": embedded_tracks,
        "compact_media": media_meta,
        "jimaku": jimaku_meta,
        "candidates": candidate_rows,
        "current_pudge": replay,
    }
    write_json(payload_path, payload)
    return case_dir




def _stored_subtitle_candidate(row: dict[str, Any]) -> SubtitleCandidate | None:
    path_value = str(row.get("path") or "").strip()
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_file():
        return None
    episode_value = row.get("episode")
    try:
        episode = int(episode_value) if episode_value is not None else None
    except (TypeError, ValueError):
        episode = None
    try:
        score = float(row.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    return SubtitleCandidate(
        path=path,
        source=str(row.get("source") or "stored"),
        score=score,
        name=str(row.get("name") or path.name),
        episode=episode,
        verified_japanese=bool(row.get("verified_japanese")),
        details=dict(details),
    )


def _stored_selected_candidate_oracle(
    replay: dict[str, object], candidate_rows: list[dict[str, Any]]
) -> None:
    selected = replay.get("selected") if isinstance(replay.get("selected"), dict) else None
    selected_path = str(selected.get("path") or "") if selected else ""
    selected_name = str(selected.get("name") or "") if selected else ""
    selected_row = next(
        (
            row
            for row in candidate_rows
            if str(row.get("path") or "") == selected_path
            or (selected_name and str(row.get("name") or "") == selected_name)
        ),
        None,
    )
    replay["selected_candidate_oracle"] = (
        selected_row.get("oracle") if isinstance(selected_row, dict) else None
    )
    any_same_episode = any(
        isinstance(row.get("oracle"), dict) and bool(row["oracle"].get("same_episode"))
        for row in candidate_rows
    )
    selected_same_episode = bool(
        isinstance(replay.get("selected_candidate_oracle"), dict)
        and replay["selected_candidate_oracle"].get("same_episode")
    )
    final_oracle = replay.get("oracle") if isinstance(replay.get("oracle"), dict) else {}
    replay["candidate_miss"] = not any_same_episode
    replay["selection_failure"] = bool(any_same_episode and not selected_same_episode)
    replay["alignment_failure"] = bool(
        selected_same_episode
        and isinstance(final_oracle, dict)
        and final_oracle.get("same_episode")
        and not final_oracle.get("good_alignment")
    )


def _stored_replay_media_path(
    report_path: Path,
    payload: dict[str, object],
    media: Path,
) -> tuple[Path, bool]:
    """Preserve the retained source basename while replaying compact media.

    Stored benchmark media is intentionally compacted under a synthetic filename.
    Production matching/timing guards may depend on release identity tokens in the
    original basename (for example a bracketed release CRC).  Replay therefore
    exposes the compact bytes through a lightweight alias carrying the original
    ``source_release_name``.  No video is downloaded or copied.
    """
    raw_name = str(payload.get("source_release_name") or "").strip()
    if not raw_name:
        return media, False
    source_name = _safe_component(Path(raw_name).name, limit=220)
    if not source_name or source_name == media.name:
        return media, False

    alias_dir = report_path.parent / "replay-media"
    alias = alias_dir / source_name
    try:
        alias_dir.mkdir(parents=True, exist_ok=True)
        if alias.is_symlink() or alias.exists():
            try:
                if alias.resolve() == media.resolve():
                    return alias, True
            except OSError:
                pass
            alias.unlink()
        alias.symlink_to(media.resolve())
        return alias, True
    except OSError:
        # Symlinks can be unavailable on some filesystems/platforms. A hardlink
        # preserves the basename without duplicating compact benchmark media.
        try:
            if alias.is_symlink() or alias.exists():
                alias.unlink()
            os.link(media, alias)
            return alias, True
        except OSError:
            return media, False


def replay_stored_benchmark_case(
    report_path: Path,
    *,
    config: AppConfig,
    force: bool = True,
) -> dict[str, object]:
    """Replay production Pudge on an already-materialized benchmark report.

    This intentionally reuses compact media and Jimaku candidate files already in
    the corpus.  It performs no AniList/Jimaku/Nyaa network access and never
    touches the retained source video, which makes before/after alignment changes
    directly comparable.
    """
    report_path = Path(report_path).expanduser().resolve()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SubtitleBenchmarkError(f"invalid stored benchmark report: {report_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SubtitleBenchmarkError(f"stored benchmark report is not an object: {report_path}")
    schema = str(payload.get("schema") or "")
    if schema not in {CORPUS_CASE_SCHEMA, DIAGNOSTIC_CASE_SCHEMA}:
        raise SubtitleBenchmarkError(f"unsupported stored benchmark schema: {schema or '<missing>'}")

    compact_meta = payload.get("compact_media") if isinstance(payload.get("compact_media"), dict) else {}
    media = Path(str(compact_meta.get("path") or "")).expanduser()
    if not media.is_file():
        raise SubtitleBenchmarkError(f"stored compact media not found: {media}")
    replay_media, release_name_preserved = _stored_replay_media_path(report_path, payload, media)
    payload["replay_media_path"] = str(replay_media)
    payload["replay_media_source_name_preserved"] = bool(release_name_preserved)
    candidate_rows = [row for row in payload.get("candidates") or [] if isinstance(row, dict)]
    candidates = [candidate for row in candidate_rows if (candidate := _stored_subtitle_candidate(row)) is not None]
    payload["replayed_at"] = time.time()
    payload["replay_generation"] = "v109-source-basename-preserved"
    if not candidates:
        payload["current_pudge"] = None
        write_json(report_path, payload)
        return {
            "report_path": str(report_path),
            "status": "no_candidates",
            "accepted": None,
            "candidate_count": 0,
        }

    cache_dir = report_path.parent / "pudge-cache"
    if schema == CORPUS_CASE_SCHEMA:
        oracle = report_path.parent / "oracle.srt"
        if not oracle.is_file():
            raise SubtitleBenchmarkError(f"stored oracle not found: {oracle}")
        replay = run_current_pudge_benchmark(
            media=replay_media,
            candidates=candidates,
            oracle=oracle,
            cache_dir=cache_dir,
            config=config,
            force=force,
        )
        _stored_selected_candidate_oracle(replay, candidate_rows)
    else:
        started = time.monotonic()
        try:
            selected, final_path, result = optimize_candidates(
                replay_media,
                candidates,
                cache_dir,
                config.sync,
                ffmpeg_path=config.tools.ffmpeg,
                ffprobe_path=config.tools.ffprobe,
                alass_path=config.tools.alass,
                force=force,
                verbose=False,
                prefer_srt=config.matching.prefer_srt,
                srt_tolerance_ratio=config.matching.srt_alignment_tolerance_ratio,
                srt_tolerance_absolute=config.matching.srt_alignment_tolerance_absolute,
            )
            accepted, quality_reason = subtitle_quality_accepted(result)
            replay = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "selected": {
                    "name": selected.name,
                    "source": selected.source,
                    "score": selected.score,
                    "path": str(selected.path),
                    "details": dict(selected.details),
                } if selected is not None else None,
                "final_path": str(final_path) if final_path is not None else None,
                "accepted": bool(accepted),
                "quality_reason": quality_reason,
                "production_result": result,
            }
        except Exception as exc:
            replay = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }

    payload["current_pudge"] = replay
    write_json(report_path, payload)
    accepted_value = replay.get("accepted") if isinstance(replay, dict) else None
    status = "error" if isinstance(replay, dict) and replay.get("error") else "replayed"
    return {
        "report_path": str(report_path),
        "status": status,
        "accepted": accepted_value,
        "candidate_count": len(candidates),
        "error": replay.get("error") if isinstance(replay, dict) else None,
    }

def collect_diagnostic_reports(corpus_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((Path(corpus_dir) / "diagnostics").glob("*/diagnostic.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == DIAGNOSTIC_CASE_SCHEMA:
            rows.append(payload)
    return rows

def collect_case_reports(corpus_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((Path(corpus_dir) / "cases").glob("*/case.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def case_is_complete(corpus_dir: Path, case_id: str) -> bool:
    path = Path(corpus_dir) / "cases" / str(case_id) / "case.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and str(payload.get("schema")) == CORPUS_CASE_SCHEMA
