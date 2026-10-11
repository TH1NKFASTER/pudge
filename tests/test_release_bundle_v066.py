"""build_release.sh: the release ZIP keeps sources and tests, and the suite gates it (behaviour;
audit 71Б rewrite). The script runs in a small project copy; `python` runs the real release
helpers but records pytest and builds a stand-in wheel, `uv` writes the requirements file."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
ZIP = shutil.which("zip")

PYTHON = f"""#!/bin/sh
echo "python $*" >> "$BUILD_LOG"
if [ "$1 $2" = "-m pytest" ]; then exit "${{PYTEST_STATUS:-0}}"; fi
if [ "$1 $2 $3" = "-m pip wheel" ]; then
  mkdir -p dist
  exec {sys.executable} -c 'import zipfile; zipfile.ZipFile("dist/pudge-9.8.7-py3-none-any.whl", "w").writestr("pudge/__init__.py", "")'
fi
exec env -u PYTHONPATH {sys.executable} "$@"
"""
UV = """#!/bin/sh
echo "uv $*" >> "$BUILD_LOG"
for last; do :; done
if [ "$1" = export ]; then echo "requests==2.0 --hash=sha256:00" > "$last"; fi
"""


def _project(folder: Path, slug: str = "pudge") -> Path:
    project = folder / "project"
    (project / "pudge").mkdir(parents=True)
    for name in ("brand.env", "install_checks.py", "legacy_install.py"):
        shutil.copy2(ROOT / "pudge" / name, project / "pudge" / name)
    brand = (
        (project / "pudge/brand.env")
        .read_text(encoding="utf-8")
        .replace('APP_SLUG="pudge"', f'APP_SLUG="{slug}"')
    )
    (project / "pudge/brand.env").write_text(brand, encoding="utf-8")
    (project / "pudge/__init__.py").write_text('__version__ = "9.8.7"\n')
    (project / "pudge/assets").mkdir()
    (project / "pudge/__pycache__").mkdir()
    (project / "pudge/__pycache__/old.cpython.pyc").write_bytes(b"cache")
    (project / "tests").mkdir()
    (project / "tests/test_example.py").write_text("def test_example():\n    pass\n")
    (project / ".github/release").mkdir(parents=True)
    for name in ("write_build_info.py", "check_release_bundle.py", "finish_release_archives.py"):
        shutil.copy2(ROOT / ".github/release" / name, project / ".github/release" / name)
    (project / ".github/workflows").mkdir()
    (project / ".github/workflows/release.yml").write_text("name: Release\n")
    for name in (
        "install.sh",
        "README.md",
        "config.example.toml",
        "pyproject.toml",
        "Makefile",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "uv.lock",
    ):
        (project / name).write_text(f"{name}\n")
    shutil.copy2(ROOT / "build_release.sh", project / "build_release.sh")
    (folder / "bin").mkdir()
    for name, text in (("python", PYTHON), ("uv", UV)):
        (folder / "bin" / name).write_text(text)
        (folder / "bin" / name).chmod(0o755)
    return project


def _build(folder: Path, project: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    if BASH is None or ZIP is None:
        pytest.skip("needs bash and zip")
    log = folder / "build.log"
    environment = {
        "PATH": f"{folder / 'bin'}:/usr/bin:/bin",
        "HOME": str(folder),
        "BUILD_LOG": str(log),
        **env,
    }
    done = subprocess.run(
        [BASH, str(project / "build_release.sh")],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    return done, log.read_text().splitlines() if log.exists() else []


def test_release_zip_ships_sources_tests_and_the_tested_wheel(tmp_path: Path) -> None:
    project = _project(tmp_path)
    done, calls = _build(tmp_path, project, PUDGE_TRIAL_JIMAKU_API_KEY="trial-key")
    assert done.returncode == 0, done.stderr
    archive = project / "dist/pudge-macos-v9.8.7.zip"
    assert done.stdout.strip().splitlines()[-1] == str(archive)
    # The full suite runs before the wheel is built.
    assert calls.index("python -m pytest -q") < calls.index(
        "python -m pip wheel . --no-deps --no-build-isolation -w dist"
    )
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        executable = (bundle.getinfo("pudge/install.sh").external_attr >> 16) & 0o111
    for name in (
        "pudge/pudge/__init__.py",
        "pudge/tests/test_example.py",
        "pudge/install.sh",
        "pudge/build_release.sh",
        "pudge/pudge-9.8.7-py3-none-any.whl",
        "pudge/release-requirements.txt",
        "pudge/uv.lock",
        "pudge/.github/release/write_build_info.py",
        "pudge/.github/workflows/release.yml",
        "pudge/pudge/build-info.json",
        "pudge/pudge/assets/jimaku-trial-key",
    ):
        assert name in names
    assert not [name for name in names if "__pycache__" in name]
    assert executable
    # The trial key goes into the archive only; the archive is the one v0.7.29 published
    # (pudge-macos-vX.Y.Z.zip with a portable checksum), with no X.Y.Z.zip copy.
    assert not (project / "pudge/assets/jimaku-trial-key").exists()
    assert (project / "dist/pudge-macos-v9.8.7.zip.sha256").read_text().endswith("  pudge-macos-v9.8.7.zip\n")
    assert sorted(path.name for path in (project / "dist").glob("*.zip*")) == [
        "pudge-macos-v9.8.7.zip",
        "pudge-macos-v9.8.7.zip.sha256",
    ]


def test_failing_suite_stops_the_release_and_brand_names_the_archive(tmp_path: Path) -> None:
    project = _project(tmp_path / "failing")
    done, calls = _build(tmp_path / "failing", project, PYTEST_STATUS="1")
    assert done.returncode != 0
    assert not [call for call in calls if "pip wheel" in call] and not (project / "dist").exists()
    # SKIP_TESTS (CI ran the suite already); the bundle folder follows the brand's slug.
    project = _project(tmp_path / "brand", slug="kappa")
    done, calls = _build(tmp_path / "brand", project, SKIP_TESTS="1")
    assert done.returncode == 0, done.stderr
    assert "python -m pytest -q" not in calls
    with zipfile.ZipFile(project / "dist/kappa-macos-v9.8.7.zip") as bundle:
        names = bundle.namelist()
    assert names and all(name.startswith("kappa/") for name in names)
    assert (project / "dist/kappa-macos-v9.8.7.zip.sha256").is_file()
    assert not (project / "dist/9.8.7.zip").exists()


def test_restored_suite_has_expected_breadth() -> None:
    tests = list(Path("tests").glob("test_*.py"))
    assert len(tests) >= 60
