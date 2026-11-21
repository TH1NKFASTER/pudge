from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import AppConfig
from .fsutil import atomic_write_json

_CACHE_SCHEMA = "final-pipeline-v13-ass-geometry-speech-recovery"
# No age limit: a manifest stays valid while the video/dependency snapshots,
# the prepared file and the alignment guard still hold (the library scan always
# trusted it that way). An age limit only re-ran the whole pipeline for an
# episode opened a week after it was prepared.
_DEFAULT_TTL_SECONDS = 0


# A raw subtitle this small is identified by its content too: an edit that kept the size
# and the time (a sync tool restoring mtime) is still seen, a touch alone is not a change.
_RAW_SOURCE_HASH_LIMIT = 4 * 1024 * 1024


def pipeline_steps_version() -> str:
    """The versions of the steps after the alignment, as one string: a manifest written by
    other versions of them describes another result."""
    from .subtitles import candidate_choice, clock_blocks, cold_open, onset_lead, scale_check

    return "|".join(
        (
            clock_blocks.VERSION,
            scale_check.VERSION,
            cold_open.VERSION,
            onset_lead.VERSION,
            candidate_choice.VERSION,
        )
    )


def tool_identity(command: str) -> dict[str, object] | None:
    """Path, size and modification time of the executable ``command`` resolves to (None when
    it does not resolve): an upgraded or replaced binary is another tool."""
    command = str(command or "").strip()
    if not command:
        return None
    resolved = command if "/" in command else shutil.which(command)
    if not resolved:
        return None
    try:
        path = Path(resolved).resolve()
        stat = path.stat()
    except OSError:
        return None
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def tool_identity_key(command: str) -> str:
    identity = tool_identity(command)
    if identity is None:
        return "missing"
    return f"{identity['path']}:{identity['size']}:{identity['mtime_ns']}"


def _alass_identity(config: AppConfig) -> dict[str, object] | None:
    command = str(getattr(getattr(config, "tools", None), "alass", "") or "alass")
    identity = tool_identity(command)
    if identity is None and command in {"alass", "alass-cli"}:
        identity = tool_identity("alass-cli" if command == "alass" else "alass")
    return identity


def _file_digest(path: Path) -> str | None:
    try:
        if path.stat().st_size > _RAW_SOURCE_HASH_LIMIT:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _raw_source_snapshot(path: Path | None) -> dict[str, object] | None:
    snapshot = _path_snapshot(path)
    if snapshot is None or path is None:
        return snapshot
    digest = _file_digest(path)
    if digest is not None:
        snapshot["sha256"] = digest
    return snapshot


def _raw_source_unchanged(snapshot: object) -> bool:
    """The raw subtitle the result was made from is still the same file. A raw file that is
    gone (a pruned download cache) does not make the prepared result wrong."""
    if not isinstance(snapshot, dict):
        return True
    try:
        path = Path(str(snapshot["path"]))
        if not path.exists():
            return True
        stat = path.stat()
        if stat.st_size == int(snapshot["size"]) and stat.st_mtime_ns == int(snapshot["mtime_ns"]):
            return True
    except (OSError, KeyError, TypeError, ValueError):
        return False
    stored = snapshot.get("sha256")
    return isinstance(stored, str) and _file_digest(path) == stored


def _inputs_snapshot(config: AppConfig, raw_source: Path | None) -> dict[str, object]:
    return {
        "raw_source_snapshot": _raw_source_snapshot(raw_source),
        "pipeline_steps": pipeline_steps_version(),
        "alass_identity": _alass_identity(config),
    }


def _inputs_changed(payload: dict[str, object], config: AppConfig) -> bool:
    """A manifest made from another raw file, by other step versions or another ALASS.

    Each field is checked only when the manifest has it: one written before the field was
    recorded stays valid (it is completed with the current state on load), so a release
    does not prepare the whole library again at once."""
    if "raw_source_snapshot" in payload and not _raw_source_unchanged(payload.get("raw_source_snapshot")):
        return True
    if "pipeline_steps" in payload and payload.get("pipeline_steps") != pipeline_steps_version():
        return True
    if "alass_identity" in payload and payload.get("alass_identity") != _alass_identity(config):
        return True
    return False


def _complete_legacy_manifest(manifest: Path, payload: dict[str, object], config: AppConfig) -> None:
    missing = [
        key for key in ("raw_source_snapshot", "pipeline_steps", "alass_identity") if key not in payload
    ]
    if not missing:
        return
    raw = str(payload.get("raw_source") or "").strip()
    current = _inputs_snapshot(config, Path(raw) if raw else None)
    for key in missing:
        payload[key] = current[key]
    try:
        atomic_write_json(manifest, payload, ensure_ascii=False, indent=2)
    except OSError:
        pass


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


_TIMELINE_VERSION_RE = re.compile(r"\btimeline-v(\d+)\.(\d+)")


def _timeline_version(value: object) -> tuple[int, int] | None:
    match = _TIMELINE_VERSION_RE.match(str(value or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _current_timeline_version() -> tuple[int, int] | None:
    from .subtitles.timeline_alignment import _ALGORITHM_VERSION

    return _timeline_version(_ALGORITHM_VERSION)


def _clock_outdated(payload: dict[str, object]) -> bool:
    """A result made by an older timeline (or saved before the fields were recorded).

    Saved results name the timeline version and the chosen file's language purity; one
    without them was made before precise clocks and Japanese-only preference, and is
    prepared again once. A result of an older timeline version is prepared again too.
    """
    if "timeline_algorithm" not in payload or "language_purity" not in payload:
        return True
    if "timeline" not in str(payload.get("engine") or ""):
        return False
    version, current = _timeline_version(payload.get("timeline_algorithm")), _current_timeline_version()
    return current is not None and (version is None or version < current)


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
    if str(payload.get("source") or "") == "external" and _clock_outdated(payload):
        invalidate_final_pipeline_result(video, config)
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
    if subtitle_id is not None:
        # The caller takes int() of it: a damaged manifest is a miss, not a crash.
        try:
            int(subtitle_id)
        except (TypeError, ValueError):
            return None
    if subtitle:
        subtitle_path = Path(subtitle)
        if not subtitle_path.is_file() or subtitle_path.stat().st_size <= 0:
            return None
        dependency = payload.get("dependency")
        if dependency is not None and not _snapshot_valid(dependency):
            return None
        if not _guard_still_valid(payload, subtitle_path):
            invalidate_final_pipeline_result(video, config)
            return None
    elif subtitle_id is None:
        return None
    if _inputs_changed(payload, config):
        invalidate_final_pipeline_result(video, config)
        return None
    _complete_legacy_manifest(manifest, payload, config)
    return payload


def final_pipeline_manifest(video: Path, config: AppConfig) -> dict[str, object] | None:
    """The stored manifest as written (no validity checks, nothing invalidated): what the
    prepared subtitle was made from, for callers that only describe it."""
    try:
        payload = json.loads(_manifest_path(video, config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _guard_still_valid(payload: dict[str, object], subtitle_path: Path) -> bool:
    """A cached result written before the current alignment guard is re-checked."""
    from .alignment_guard import GUARD_VERSION, validate_alignment_output, validate_final_standalone

    if payload.get("alignment_guard_version") == GUARD_VERSION:
        return True
    if subtitle_path.suffix.casefold() != ".srt" or str(payload.get("source") or "") == "embedded":
        return True
    raw = str(payload.get("raw_source") or "").strip()
    if raw and Path(raw).is_file() and Path(raw).resolve() != subtitle_path.resolve():
        verdict = validate_alignment_output(Path(raw), subtitle_path)
    else:
        verdict = validate_final_standalone(subtitle_path)
    return verdict.get("status") != "rejected"


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
    raw_source: Path | None = None,
    alignment: dict[str, object] | None = None,
    candidate_details: dict[str, object] | None = None,
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
        "raw_source": str(raw_source) if raw_source is not None else "",
        **_inputs_snapshot(config, raw_source),
        "alignment_guard_version": _guard_version(),
        "engine": str((alignment or {}).get("engine") or ""),
        "timeline_algorithm": str((alignment or {}).get("timeline_algorithm") or ""),
        "language_purity": str((candidate_details or {}).get("language_purity") or ""),
    }
    atomic_write_json(manifest, payload, ensure_ascii=False, indent=2)
    return manifest


def _guard_version() -> str:
    from .alignment_guard import GUARD_VERSION

    return GUARD_VERSION


def invalidate_final_pipeline_result(video: Path, config: AppConfig) -> None:
    try:
        _manifest_path(video, config).unlink(missing_ok=True)
    except OSError:
        pass
