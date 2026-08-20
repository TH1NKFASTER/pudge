from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_companion_assets_are_in_wheel_package_data() -> None:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = payload["tool"]["setuptools"]["package-data"]["pudge"]
    assert "web/companion/*" in package_data

    companion = ROOT / "pudge" / "web" / "companion"
    for name in (
        "index.html",
        "app.js",
        "styles.css",
        "manifest.webmanifest",
        "sw.js",
        "icon.svg",
    ):
        assert (companion / name).is_file()
