from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import AppConfig

_CACHE_SCHEMA = "final-pipeline-v9"
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def pipeline_cache_key(video: Path, config: AppConfig) -> str:
    video = video.expanduser().resolve()
    stat = video.stat()
    payload = {
        "schema": _CACHE_SCHEMA,
        "video": str(video),
        "video_size": stat.st_size,
        "video_mtime_ns": stat.st_mtime_ns,
        "matching": asdict(config.matching),
        "sync": asdict(config.sync),
        "llm": {
            key: value
            for key, value in asdict(config.llm).items()
            if key not in {"api_key", "base_url", "timeout_seconds", "keep_alive"}
        },
        "tools": {
            "ffmpeg": config.tools.ffmpeg,
            "ffprobe": config.tools.ffprobe,
            "alass": config.tools.alass,
        },
    }
    raw = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _manifest_path(video: Path, config: AppConfig) -> Path:
    return config.paths.cache_dir / "final-pipeline" / f"{pipeline_cache_key(video, config)}.json"


def _path_snapshot(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _snapshot_valid(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        path = Path(str(payload["path"]))
        stat = path.stat()
        return stat.st_size == int(payload["size"]) and stat.st_mtime_ns == int(payload["mtime_ns"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


def load_final_pipeline_result(
    video: Path,
    config: AppConfig,
    *,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
) -> dict[str, object] | None:
    manifest = _manifest_path(video, config)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA:
        return None
    if str(payload.get("source") or "").casefold() == "ocr" and not config.matching.ocr_image_subtitles:
        return None
    try:
        created_at = float(payload.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if ttl_seconds > 0 and time.time() - created_at > ttl_seconds:
        return None

    subtitle = str(payload.get("subtitle") or "").strip()
    subtitle_id = payload.get("subtitle_id")
    if subtitle:
        subtitle_path = Path(subtitle)
        if not subtitle_path.is_file() or subtitle_path.stat().st_size <= 0:
            return None
        dependency = payload.get("dependency")
        if dependency is not None and not _snapshot_valid(dependency):
            return None
    elif subtitle_id is None:
        return None
    return payload


def final_pipeline_cache_available(video: Path, config: AppConfig) -> bool:
    try:
        return load_final_pipeline_result(video, config) is not None
    except OSError:
        return False


def save_final_pipeline_result(
    video: Path,
    config: AppConfig,
    *,
    subtitle: Path | None,
    subtitle_id: int | None,
    dependency: Path | None = None,
    source: str = "",
) -> Path:
    manifest = _manifest_path(video, config)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _CACHE_SCHEMA,
        "created_at": time.time(),
        "video": str(video.resolve()),
        "subtitle": str(subtitle.resolve()) if subtitle is not None else "",
        "subtitle_id": subtitle_id,
        "dependency": _path_snapshot(dependency),
        "source": source,
    }
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(manifest)
    return manifest


def invalidate_final_pipeline_result(video: Path, config: AppConfig) -> None:
    try:
        _manifest_path(video, config).unlink(missing_ok=True)
    except OSError:
        pass
