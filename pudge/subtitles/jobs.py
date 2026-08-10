from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import SubtitleJobStage


class SubtitleJobReporter:
    """Atomic cross-process progress for the background subtitle worker."""

    def __init__(self, path: Path | None, trace_path: Path | None = None) -> None:
        self.path = path
        self.trace_path = trace_path

    @classmethod
    def from_environment(cls) -> "SubtitleJobReporter":
        raw = os.getenv("PUDGE_SUBTITLE_JOB_STATUS", "").strip()
        trace_raw = os.getenv("PUDGE_SUBTITLE_JOB_TRACE", "").strip()
        return cls(
            Path(raw) if raw else None,
            Path(trace_raw) if trace_raw else None,
        )

    def update(self, stage: SubtitleJobStage, **details: Any) -> None:
        payload = {"stage": stage.value, "updated_at": time.time(), "details": details}
        if self.trace_path is not None:
            try:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                with self.trace_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"kind": "worker_stage", **payload},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            except OSError:
                pass
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                temporary = Path(handle.name)
            temporary.replace(self.path)
        except OSError:
            return


def read_job_report(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
