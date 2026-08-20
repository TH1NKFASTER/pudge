from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .diagnostics import DebugBundleBuilder


class CompanionController:
    """Use cases shared by the desktop bridge and future native frontends."""

    def __init__(self, mobile_sync: Any, streaming: Any) -> None:
        self.mobile_sync = mobile_sync
        self.streaming = streaming

    def status(self, config: Any, base_url: str) -> dict[str, Any]:
        bind_host = str(config.bind_host)
        return {
            "enabled": bool(config.enabled),
            "base_url": str(base_url),
            "bind_host": bind_host,
            "port": int(config.port),
            "protocol": self.mobile_sync.protocol_info(),
            "devices": self.mobile_sync.devices(),
            "trusted_lan_required": bind_host in {"0.0.0.0", "::"},
        }

    def revoke(self, device_id: str) -> dict[str, Any]:
        normalized = str(device_id or "")
        self.streaming.revoke_device(normalized)
        return {
            "ok": self.mobile_sync.revoke_device(normalized),
            "devices": self.mobile_sync.devices(),
        }

    def conflicts(self, limit: int = 100) -> dict[str, Any]:
        rows = self.mobile_sync.conflicts()
        return {"ok": True, "conflicts": rows[: max(1, min(500, int(limit)))]}

    def resolve_conflict(self, conflict_id: int, resolution: str) -> dict[str, Any]:
        return self.mobile_sync.resolve_conflict(
            int(conflict_id),
            accept_incoming=str(resolution).casefold() in {"incoming", "remote"},
        )


class DiagnosticsController:
    def __init__(
        self,
        builder: DebugBundleBuilder,
        snapshot: Callable[[int, int | None], dict[str, Any]],
    ) -> None:
        self.builder = builder
        self.snapshot = snapshot

    def export(
        self,
        output_dir: Path,
        *,
        version: str,
        frontend: dict[str, Any],
        logs: dict[str, Path],
    ) -> Path:
        snapshots: list[dict[str, Any]] = []
        media_id = frontend.get("media_id")
        if media_id is not None:
            try:
                episode = frontend.get("episode")
                snapshots.append(
                    self.snapshot(
                        int(media_id),
                        None if episode is None else int(episode),
                    )
                )
            except (OSError, TypeError, ValueError):
                pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = int(time.time_ns() % 1_000_000)
        target = Path(output_dir) / f"pudge-runtime-debug-{stamp}-{suffix:06d}.zip"
        return self.builder.build(
            target,
            version=version,
            frontend=frontend,
            snapshots=snapshots,
            logs=logs,
        )
