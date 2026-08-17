from __future__ import annotations

from pathlib import Path
from typing import Any


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        if hasattr(obj, "keys") and name in obj.keys():
            return obj[name]
    except Exception:
        pass
    return getattr(obj, name, default)


def _raw(download: Any) -> dict[str, Any]:
    value = _value(download, "raw", {})
    return value if isinstance(value, dict) else {}


def _local_file_exists(local: Any) -> bool:
    raw = _value(local, "video_path")
    if not raw:
        return False
    try:
        return Path(str(raw)).expanduser().is_file()
    except OSError:
        return False


def download_complete(download: Any) -> bool:
    if download is None:
        return False
    state = str(_value(download, "state", "") or "").casefold()
    if state in {"complete", "completed", "seeding", "uploading", "stalledup"}:
        return True
    try:
        progress = float(_value(download, "progress", 0.0) or 0.0)
    except (TypeError, ValueError):
        progress = 0.0
    raw = _raw(download)
    try:
        total = int(raw.get("total_size") or raw.get("totalLength") or 0)
        downloaded = int(raw.get("downloaded") or raw.get("completedLength") or 0)
    except (TypeError, ValueError):
        total = downloaded = 0
    verifying = bool(raw.get("verifying"))
    return bool(
        not verifying
        and (
            progress >= 0.999
            or (total > 0 and downloaded >= total)
        )
    )


def _download_progress(download: Any) -> tuple[int, int | None]:
    try:
        progress = max(0.0, min(1.0, float(_value(download, "progress", 0.0) or 0.0)))
    except (TypeError, ValueError):
        progress = 0.0
    raw = _raw(download)
    try:
        speed = max(
            0,
            int(
                raw.get("download_speed")
                or raw.get("dlspeed")
                or _value(download, "download_speed", 0)
                or 0
            ),
        )
        total = max(0, int(raw.get("total_size") or raw.get("totalLength") or 0))
        downloaded = max(
            0,
            int(
                raw.get("downloaded")
                or raw.get("completedLength")
                or round(total * progress)
                or 0
            ),
        )
    except (TypeError, ValueError):
        speed = total = downloaded = 0
    eta = None
    if speed > 0 and total > downloaded:
        eta = max(1, int((total - downloaded) / speed))
    return int(round(progress * 100)), eta


def derive_episode_presentation(
    *,
    local: Any = None,
    download: Any = None,
    action_job: Any = None,
) -> dict[str, Any]:
    """Return one canonical user-facing episode status.

    Durable episode state remains authoritative for local media. Download/job
    state only fills the gap before the local episode is ready, so stale torrent
    metadata cannot demote an existing file back to "Downloading".
    """

    local_state = str(_value(local, "state", "") or "").casefold()
    local_exists = _local_file_exists(local)
    action_code = str(_value(action_job, "action_code", "") or "")
    action_state = str(_value(action_job, "state", "") or "").casefold()

    if local_exists and local_state == "watched":
        return {"status": "watched", "ready": True, "action_code": ""}
    if local_exists and local_state == "ready":
        return {"status": "ready", "ready": True, "action_code": ""}
    if action_job is not None and (action_state == "needs_action" or action_code):
        return {
            "status": "needs_action",
            "ready": False,
            "action_code": action_code,
        }
    if local_exists and local_state == "waiting_text_subtitles":
        return {
            "status": "waiting_text_subtitles",
            "ready": False,
            "action_code": action_code,
        }
    if local_exists:
        return {
            "status": "waiting_subtitles",
            "ready": False,
            "action_code": "",
        }

    if download is not None:
        if download_complete(download):
            return {
                "status": "waiting_preparation",
                "ready": False,
                "progress_percent": 100,
                "eta_seconds": None,
                "action_code": "",
            }
        progress, eta = _download_progress(download)
        state = str(_value(download, "state", "") or "").casefold()
        raw = _raw(download)
        error = bool(
            state in {"error", "missingfiles", "unknown"}
            or raw.get("error_code")
            or raw.get("error_message")
        )
        if error:
            status = "download_error"
        elif state in {"paused", "stopped", "waiting", "queued"} and progress <= 0:
            status = "waiting_download"
        else:
            status = "downloading"
        return {
            "status": status,
            "ready": False,
            "progress_percent": progress,
            "eta_seconds": eta,
            "action_code": "",
        }

    return {"status": "missing", "ready": False, "action_code": ""}
