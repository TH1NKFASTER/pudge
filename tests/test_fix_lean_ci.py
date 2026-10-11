"""CI is lean: the tests run once per batch on Python 3.12 (the Python the app's installer sets
up), lint runs on Linux with ruff and mypy at the versions uv.lock pins instead of installing the
project on a Mac, and the release checks use the same Python and batches (behaviour of the
workflows: the jobs GitHub starts, their steps run with stand-ins, tests/t71g01_support.py)."""

from __future__ import annotations

import re
import tomllib

from source_policy import source
from t71g01_support import Recorder, load_workflow, run_step, workflow_step

BATCHES = ["1", "2", "3", "4"]  # as GitHub expands the matrix


def _workflow(name: str) -> dict:
    # policy: the workflow file is configuration for GitHub; its jobs are expanded and its steps run here.
    return load_workflow(source(f".github/workflows/{name}"))


def _locked_version(package: str) -> str:
    # policy: uv.lock is the record of the pinned versions (data, nothing to run).
    lock = tomllib.loads(source("uv.lock"))
    return next(entry["version"] for entry in lock["package"] if entry["name"] == package)


def _expand(value: object, matrix: dict) -> str:
    return re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda match: str(matrix[match.group(1)]), str(value))


def _started_jobs(workflow: dict) -> list[dict]:
    """One entry per job GitHub starts (each matrix combination is a job of its own)."""
    started = []
    for job_id, job in workflow["jobs"].items():
        combinations: list[dict] = [{}]
        for key, values in ((job.get("strategy") or {}).get("matrix") or {}).items():
            combinations = [
                {**combination, key: str(value)} for combination in combinations for value in values
            ]
        for matrix in combinations:
            started.append(
                {
                    "job": job_id,
                    "matrix": matrix,
                    "runner": job["runs-on"],
                    "pythons": [
                        _expand(step["with"]["python-version"], matrix)
                        for step in job["steps"]
                        if str(step.get("uses", "")).startswith("actions/setup-python")
                    ],
                }
            )
    return started


def _batch_run(tmp_path, workflow: dict, job: str, matrix: dict) -> list[str]:
    """What the job's "Run test batch" step runs for this matrix entry."""
    calls = Recorder(tmp_path / f"{job}-{matrix['batch']}")
    calls.tool("python")
    step = workflow_step(workflow, job, "Run test batch")
    project = calls.base / "project"
    project.mkdir()
    done = run_step({**step, "run": _expand(step["run"], matrix)}, project, tools=calls.bin, env=calls.env)
    assert done.returncode == 0, done.stderr
    [call] = calls.calls("python")
    return call


def test_ci_runs_each_test_batch_once_on_python_3_12(tmp_path):
    workflow = _workflow("ci.yml")
    started = _started_jobs(workflow)

    tests = [job for job in started if job["job"] == "tests"]
    assert [job["matrix"] for job in tests] == [{"batch": batch} for batch in BATCHES]
    assert all(job["runner"].startswith("macos") and job["pythons"] == ["3.12"] for job in tests)
    # Every batch of the suite, each once.
    runs = [_batch_run(tmp_path, workflow, "tests", job["matrix"]) for job in tests]
    assert sorted(run[run.index("--batch") + 1] for run in runs) == ["0", "1", "2", "3"]
    assert {run[run.index("--batches") + 1] for run in runs} == {"4"}
    uploads = {
        _expand(step["with"]["name"], job["matrix"])
        for job in tests
        for step in workflow["jobs"]["tests"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    }
    assert len(uploads) == len(BATCHES)  # artifact names must not clash

    # The Mac runners: the 4 test batches and the smoke job; every job uses Python 3.12.
    assert sorted(job["job"] for job in started if job["runner"].startswith("macos")) == [
        "macos-smoke",
        *["tests"] * len(BATCHES),
    ]
    assert {python for job in started for python in job["pythons"]} == {"3.12"}


def test_lint_runs_on_linux_with_the_ruff_and_mypy_versions_of_the_lock(tmp_path):
    workflow = _workflow("ci.yml")
    lint = workflow["jobs"]["lint"]
    assert lint["runs-on"].startswith("ubuntu")
    calls = Recorder(tmp_path / "lint")
    for tool in ("python", "uv", "uvx", "node"):
        calls.tool(tool)
    project = calls.base / "project"
    (project / "pudge/web").mkdir(parents=True)
    (project / "pudge/web/app.js").write_text("", encoding="utf-8")

    for step in lint["steps"]:
        if "run" in step:
            done = run_step(step, project, tools=calls.bin, env={**(lint.get("env") or {}), **calls.env})
            assert done.returncode == 0, (step.get("name"), done.stderr)

    # Nothing of the project is installed: no `uv sync`, only uv itself (for uvx).
    assert calls.calls("uv") == []
    assert ["-m", "pip", "install", "--upgrade", "uv"] in calls.calls("python")
    ruff, mypy = f"ruff@{_locked_version('ruff')}", f"mypy@{_locked_version('mypy')}"
    uvx = calls.calls("uvx")
    assert {call[0] for call in uvx} == {ruff, mypy}
    assert [ruff, "check", "--select", "E9,F", "pudge", ".github/release"] in uvx
    assert [ruff, "check", "--select", "E9,F63,F7", "tests"] in uvx
    assert [ruff, "format", "--check", "pudge", "tests", "scripts", ".github"] in uvx
    [type_check] = [call for call in uvx if call[0] == mypy]
    # Checked as on the Mac the app runs on.
    assert type_check[1:4] == ["--platform", "darwin", "--follow-imports=skip"]
    assert [".github/release/sync_config_example.py", "--check"] in calls.calls("python")
    assert [".github/release/check_release_metadata.py"] in calls.calls("python")
    assert calls.calls("node") == [["--check", "pudge/web/app.js"]]


def test_the_release_checks_use_the_same_python_and_batches_as_ci(tmp_path):
    release, ci = _workflow("release.yml"), _workflow("ci.yml")
    started = _started_jobs(release)

    assert {python for job in started for python in job["pythons"]} == {"3.12"}
    tests = [job for job in started if job["job"] == "verify-and-test"]
    assert [job["matrix"] for job in tests] == [{"batch": batch} for batch in BATCHES]
    strip = lambda run: run[: run.index("--results-dir")] if "--results-dir" in run else run  # noqa: E731
    assert [strip(_batch_run(tmp_path, release, "verify-and-test", job["matrix"])) for job in tests] == [
        strip(_batch_run(tmp_path / "ci", ci, "tests", {"batch": batch})) for batch in BATCHES
    ]
