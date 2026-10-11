"""An update with no old-brand folders to migrate prints nothing (behaviour).

install.sh runs pudge/legacy_install.py twice; each printed an all-empty JSON report."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pudge" / "legacy_install.py"


def _migrate(home: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "paths", "--home", str(home)]
        + ["--app-name", "pudge", "--app-slug", "pudge", "--legacy-slugs", "anime-mpv"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_nothing_to_migrate_prints_nothing_and_a_migration_is_reported(tmp_path):
    assert _migrate(tmp_path) == ""

    old = tmp_path / ".config" / "anime-mpv" / "config.toml"
    old.parent.mkdir(parents=True)
    old.write_text("x = 1\n", encoding="utf-8")
    report = json.loads(_migrate(tmp_path))

    assert report["moved"] == [str(old)]
    assert (tmp_path / ".config" / "pudge" / "config.toml").read_text(encoding="utf-8") == "x = 1\n"
