#!/usr/bin/env python3
"""Run a bounded offline preflight, or an explicitly requested developer suite."""

from __future__ import annotations

import argparse
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


def run_smoke_check(project: Path, python: Path, *, timeout: float = 30) -> int:
    probe = """
import ast, pathlib, sys, tomllib, zipfile
root = pathlib.Path(sys.argv[1])
package = root / "pudge"
data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
tree = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
versions = [ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
if versions != [data["project"]["version"]]:
    raise RuntimeError("Source version declarations disagree")
for path in package.rglob("*.py"):
    compile(path.read_bytes(), str(path), "exec")
for wheel in root.glob("pudge-*.whl"):
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Release wheel is corrupt")
print("Installation smoke check passed.")
"""
    try:
        subprocess.run([str(python), "-I", "-c", probe, str(project)], check=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Installation smoke check timed out after {timeout:g} seconds.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Installation smoke check failed; installation stopped.") from exc
    return 0


def _requirements_ready(python: Path, project: Path) -> bool:
    probe = """
import importlib.metadata as md, sys, tomllib
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
data = tomllib.loads(open(sys.argv[1], encoding="utf-8").read())["project"]
requirements = data["dependencies"] + sum(
    [data["optional-dependencies"][extra] for extra in ("dev", "sync")], [])
for text in requirements:
    requirement = Requirement(text)
    if requirement.marker and not requirement.marker.evaluate():
        continue
    try:
        version = md.version(requirement.name)
    except md.PackageNotFoundError:
        sys.exit(1)
    if version not in requirement.specifier:
        sys.exit(1)
"""
    return (
        subprocess.run(
            [str(python), "-c", probe, str(project / "pyproject.toml")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _test_python(project: Path) -> Path:
    supplied = os.environ.get("PUDGE_INSTALL_TEST_PYTHON", "").strip()
    python = (
        Path(supplied).expanduser().absolute() if supplied else project / ".venv-install-tests/bin/python"
    )
    if supplied and not python.is_file():
        raise RuntimeError(f"Test Python does not exist: {python}")
    if not python.is_file():
        print("Preparing a separate installation test environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    if not _requirements_ready(python, project):
        if subprocess.run(
            [str(python), "-c", "import pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode:
            subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--editable",
                str(project) + "[dev,sync]",
            ],
            check=True,
        )
        if not _requirements_ready(python, project):
            raise RuntimeError("Installation test dependencies are incomplete.")
    return python


def run_suite(project: Path, python: Path, logs: Path) -> int:
    policy = Path(__file__).with_name("ocr_fixture_policy.py")
    violations = runpy.run_path(str(policy))["check"](project)
    if violations:
        raise RuntimeError("OCR fixture policy failed; installation stopped:\n" + "\n".join(violations))
    logs.mkdir(parents=True, exist_ok=True)
    print(f"Running ALL tests before installation. Logs: {logs}", flush=True)
    # Ignore caller selection flags and real Pudge state, including an inherited
    # PUDGE_HOME from the application updater. No real config or library is used.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PUDGE_") and key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
    }
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(project)
    env["PYTHONUNBUFFERED"] = "1"
    with tempfile.TemporaryDirectory(prefix="pudge-install-tests-") as temporary:
        isolated = Path(temporary)
        for name in ("home", "tmp", "gui-stubs"):
            (isolated / name).mkdir()
        for name in ("open", "xdg-open", "osascript"):
            stub = isolated / "gui-stubs" / name
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
        env.update(
            HOME=str(isolated / "home"),
            TMPDIR=str(isolated / "tmp"),
            TEMP=str(isolated / "tmp"),
            TMP=str(isolated / "tmp"),
            PATH=str(isolated / "gui-stubs") + os.pathsep + env.get("PATH", ""),
            PUDGE_HOME=str(isolated / "pudge-home"),
            PUDGE_RUNTIME_LOG_PATH=str(Path(temporary) / "runtime.log"),
            PUDGE_ANIME_MAPPINGS="0",
            PUDGE_SEADEX="0",
            PUDGE_INTRO_DETECTION="0",
        )
        command = [
            str(python),
            "-m",
            "pytest",
            "-v",
            "-ra",
            "-o",
            "addopts=",
            "-o",
            "faulthandler_timeout=60",
            "--junitxml=" + str(logs / "results.xml"),
            "tests",
        ]
        # A worker can inherit pytest's stdout. Poll the pytest process itself
        # rather than waiting for pipe EOF from every surviving descendant.
        with (logs / "pytest.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with (logs / "pytest.log").open(encoding="utf-8", errors="replace") as reader:
                    while process.poll() is None:
                        print(reader.read(), end="", flush=True)
                        time.sleep(0.1)
                    print(reader.read(), end="", flush=True)
                status = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
    if status != 0:
        print(
            f"Tests failed. Installation stopped before changing the app. Log: {logs / 'pytest.log'}",
            file=sys.stderr,
        )
        return status if status > 0 else 1
    try:
        report = ET.parse(logs / "results.xml").getroot()
        suites = list(report.iter("testsuite"))
        tests = sum(int(suite.get("tests", "0")) for suite in suites)
        skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
        failures = sum(int(suite.get("failures", "0")) + int(suite.get("errors", "0")) for suite in suites)
    except (OSError, ET.ParseError, ValueError) as exc:
        raise RuntimeError("No valid complete test report; installation stopped.") from exc
    for case in report.iter("testcase"):
        reason = case.find("skipped")
        if reason is not None and any(word in case.get("classname", "").lower() for word in ("manga", "ocr")):
            message = (reason.get("message", "") + (reason.text or "")).lower()
            if any(word in message for word in ("fixture", "corpus", "page image", "png/json")):
                raise RuntimeError(
                    "OCR test skipped for missing materials; installation stopped: " + case.get("name", "")
                )
    if tests <= skipped or failures:
        raise RuntimeError("No passing tests or an inconsistent report; installation stopped.")
    print(f"Installation test gate passed: {tests - skipped} passed, {skipped} skipped.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-suite", action="store_true", help="Prepare dev tools and run all source tests")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if not args.full_suite:
        return run_smoke_check(project, Path(sys.executable))
    if not (project / "tests").is_dir():
        raise RuntimeError("Source test suite is missing; installation stopped.")
    missing = [tool for tool in ("node", "ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise RuntimeError("Required test tools are missing: " + ", ".join(missing))
    python = _test_python(project)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())
    return run_suite(project, python, project / ".install-test-logs" / stamp)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installation stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
