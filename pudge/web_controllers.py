from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .diagnostics import DebugBundleBuilder


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
