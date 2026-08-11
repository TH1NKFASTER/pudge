from __future__ import annotations

import json
import os
import time
from importlib import resources
from pathlib import Path
from typing import Any


TRIAL_SECONDS = 48 * 60 * 60
_ASSET_NAME = "jimaku-trial-key"


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
