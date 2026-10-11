from __future__ import annotations

import hashlib
import json
import os
import time
from importlib import resources
from pathlib import Path
from typing import Any

from .branding import APP_SLUG


TRIAL_SECONDS = 48 * 60 * 60
_ASSET_NAME = "jimaku-trial-key"
# Statuses Jimaku answers when it does not accept an API key (invalid, revoked).
KEY_REJECTED_STATUSES = frozenset({401, 403})


def bundled_jimaku_api_key() -> str:
    """Return the release-bundled trial key without persisting it to user config."""

    environment = os.getenv("PUDGE_BUNDLED_JIMAKU_API_KEY", "").strip()
    if environment:
        return environment
    try:
        asset = resources.files("pudge").joinpath("assets", _ASSET_NAME)
        return asset.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, TypeError):
        return ""


def _trial_marker(cache_dir: Path) -> Path:
    return Path(cache_dir).expanduser() / "jimaku-trial.json"


def apply_jimaku_trial(config: Any, *, now: float | None = None) -> None:
    """Use the bundled Jimaku key for 48 hours when no personal key is configured."""

    current_time = float(time.time() if now is None else now)
    jimaku = config.jimaku
    was_trial_active = bool(getattr(jimaku, "trial_active", False))
    personal = str(getattr(jimaku, "personal_api_key", "") or "").strip()
    if not personal and not was_trial_active:
        # Keep AppConfig instances constructed directly by callers compatible:
        # before the trial fields existed, api_key was the only personal-key field.
        personal = str(getattr(jimaku, "api_key", "") or "").strip()
    jimaku.personal_api_key = personal
    jimaku.trial_active = False
    jimaku.trial_expires_at = 0.0
    if personal:
        jimaku.api_key = personal
        return

    jimaku.api_key = ""
    bundled = bundled_jimaku_api_key()
    if not bundled:
        return

    marker = _trial_marker(config.paths.cache_dir)
    started_at = current_time
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        started_at = float(payload.get("started_at") or current_time)
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"started_at": current_time}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    expires_at = started_at + TRIAL_SECONDS
    jimaku.trial_expires_at = expires_at
    if current_time < expires_at:
        jimaku.api_key = bundled
        jimaku.trial_active = True


def persisted_jimaku_api_key(jimaku: Any) -> str:
    personal = str(getattr(jimaku, "personal_api_key", "") or "").strip()
    if personal:
        return personal
    if not bool(getattr(jimaku, "trial_active", False)):
        return str(getattr(jimaku, "api_key", "") or "").strip()
    return ""


def jimaku_key_fingerprint(api_key: str) -> str:
    """A short, non-reversible id of a key (logs and markers never hold the key itself)."""

    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:12]


def jimaku_rejects_key(status_code: int, content_type: str = "") -> bool:
    """Whether a Jimaku answer means "this API key is not accepted".

    401/403 from the API itself; an HTML 403 is a proxy or bot-protection page
    in front of Jimaku and says nothing about the key."""

    return int(status_code) in KEY_REJECTED_STATUSES and "text/html" not in str(content_type).casefold()


def _rejection_marker(cache_dir: Path) -> Path:
    return Path(cache_dir).expanduser() / "jimaku-api" / "key-rejected.json"


def _notice_marker(cache_dir: Path) -> Path:
    return Path(cache_dir).expanduser() / "jimaku-key-notice.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def record_jimaku_key_rejection(
    cache_dir: Path, key_hash: str, status_code: int, *, now: float | None = None
) -> None:
    """Remember (across processes) that Jimaku refused the key ``key_hash`` identifies."""

    _write_json(
        _rejection_marker(cache_dir),
        {
            "key_hash": str(key_hash),
            "status": int(status_code),
            "at": float(time.time() if now is None else now),
        },
    )


def clear_jimaku_key_rejection(cache_dir: Path, key_hash: str) -> None:
    """The key was accepted again: forget an earlier refusal of that same key."""

    marker = _rejection_marker(cache_dir)
    if not marker.is_file():
        return
    if str(_read_json(marker).get("key_hash") or "") != str(key_hash):
        return
    try:
        marker.unlink()
    except OSError:
        pass


def jimaku_key_problem(config: Any) -> dict[str, Any]:
    """Why Jimaku search does not work with the configured key, if it does not.

    ``kind`` is ``personal_rejected`` (Jimaku refuses the user's own key),
    ``trial_rejected`` (Jimaku refuses the bundled key) or ``trial_expired`` (the
    48 hours are over); ``{}`` when there is no problem. ``id`` is the same for one
    refused personal key (by its fingerprint) or one trial, so the window shows its
    notice once for each; ``seen`` says it was shown already."""

    jimaku = config.jimaku
    cache_dir = config.paths.cache_dir
    rejection = _read_json(_rejection_marker(cache_dir))
    rejected_hash = str(rejection.get("key_hash") or "")
    personal = persisted_jimaku_api_key(jimaku)
    if personal:
        fingerprint = jimaku_key_fingerprint(personal)
        if rejected_hash != fingerprint:
            return {}
        problem: dict[str, Any] = {
            "kind": "personal_rejected",
            "status": int(rejection.get("status") or 0),
            "id": f"personal-{fingerprint}",
        }
    else:
        bundled = bundled_jimaku_api_key()
        if not bundled:
            return {}
        expires_at = float(getattr(jimaku, "trial_expires_at", 0.0) or 0.0)
        if rejected_hash == jimaku_key_fingerprint(bundled):
            problem = {"kind": "trial_rejected", "status": int(rejection.get("status") or 0)}
        elif expires_at > 0 and not bool(getattr(jimaku, "trial_active", False)):
            problem = {"kind": "trial_expired", "status": 0}
        else:
            return {}
        problem["id"] = f"trial-{int(expires_at)}"
    problem["seen"] = str(_read_json(_notice_marker(cache_dir)).get("seen") or "") == problem["id"]
    return problem


def mark_jimaku_key_notice_seen(cache_dir: Path, notice_id: str) -> None:
    _write_json(_notice_marker(cache_dir), {"seen": str(notice_id or ""), "at": time.time()})


def check_jimaku_api_key(base_url: str, api_key: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Ask Jimaku once whether it accepts ``api_key`` (one small search request).

    ``status``: ``ok`` / ``empty`` (accepted), ``invalid`` (refused),
    ``rate_limited``, ``unreachable`` or ``error`` (could not be checked)."""

    # pudge.config imports this module, and install.sh reads the config with a bare Python.
    import httpx

    key = str(api_key or "").strip()
    if not key:
        return {"service": "Jimaku", "status": "missing"}
    try:
        response = httpx.get(
            f"{str(base_url or '').rstrip('/')}/api/entries/search",
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "User-Agent": APP_SLUG,
            },
            params={"anime": "true", "query": "Frieren"},
            timeout=timeout,
            follow_redirects=True,
        )
        status_code = int(response.status_code)
        content_type = (getattr(response, "headers", None) or {}).get("content-type", "")
        if jimaku_rejects_key(status_code, content_type):
            return {"service": "Jimaku", "status": "invalid", "http_status": status_code}
        if status_code == 429:
            return {"service": "Jimaku", "status": "rate_limited"}
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        return {"service": "Jimaku", "status": "unreachable", "detail": str(exc)}
    except ValueError as exc:
        return {"service": "Jimaku", "status": "error", "detail": str(exc)}
    count = len(payload) if isinstance(payload, list) else 0
    return {"service": "Jimaku", "status": "ok" if count > 0 else "empty", "count": count}
