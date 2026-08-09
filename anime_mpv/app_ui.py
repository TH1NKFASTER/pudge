from __future__ import annotations

from pathlib import Path

from .web_app import launch_web_app


def launch_app(config_path: Path) -> int:
    return launch_web_app(config_path)
