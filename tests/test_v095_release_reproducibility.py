"""Release reproducibility: waiting cards, lint gates over all Python and JavaScript, locked
and hashed release dependencies (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

from source_policy import source
from t71g01_support import (
    ROOT,
    Recorder,
    build_release,
    load_workflow,
    release_history,
    run_step,
    source_history,
    workflow_step,
)


def test_waiting_cards_show_the_episode_status_without_a_final_suffix(page) -> None:
    final = {
        "media_id": 1,
        "title": "Final",
        "next_episode": 12,
        "is_final_episode": True,
        "local": {"state": "waiting_subtitles", "episode": 12},
    }
    downloading = {
        "media_id": 2,
        "title": "Downloading",
        "next_episode": 3,
        "download": {"state": "downloading", "progress": 0.42},
    }
    downloaded = {
        "media_id": 3,
        "title": "Downloaded",
        "next_episode": 4,
        "download": {"state": "completed", "progress": 1},
    }
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('current'); renderCurrent(); await __tick(50);
            const state = id => __text(`.waiting-card[data-media-id="${id}"] .airing-state`);
            return {states: [state(1), state(2), state(3)], waiting: t('label.waitingSubs'), final: t('label.final')};
            """,
            lang=lang,
            state={"home": {"waiting": [final, downloading, downloaded]}},
        )
        out = result.value
        # v0.7.29 appended " • Final" to the status of a final episode still being prepared.
        assert out["states"][0] == out["waiting"] and out["final"] not in out["states"][0]
        assert out["states"][1].endswith("42%")
        assert out["states"][2] == out["waiting"]


def _lint_tools(base: Path) -> Recorder:
    """Stand-ins for python and node, and a project with nested JavaScript modules."""
    calls = Recorder(base)
    calls.tool("python")
    calls.tool("node")
    for name in (
        "pudge/web/app.js",
        "pudge/web/vn/capture.js",
        "pudge/web/style.css",
        "pudge/cli.py",
        ".github/release/release.py",
    ):
        (base / "project" / name).parent.mkdir(parents=True, exist_ok=True)
        (base / "project" / name).write_text("", encoding="utf-8")
    return calls


def test_local_and_ci_lint_check_all_python_and_every_javascript_module(tmp_path: Path) -> None:
    ruff = ["-m", "ruff", "check", "--select", "E9,F", "pudge", ".github/release"]
    formatted = ["-m", "ruff", "format", "--check", "pudge", "tests", "scripts", ".github"]
    every_js = [["--check", "pudge/web/app.js"], ["--check", "pudge/web/vn/capture.js"]]
    # make lint (the local release gate runs it through make quality).
    calls = _lint_tools(tmp_path / "make")
    shutil.copy2(ROOT / "Makefile", tmp_path / "make/project/Makefile")
    completed = subprocess.run(
        ["make", "lint", "PYTHON=python"],
        cwd=tmp_path / "make/project",
        capture_output=True,
        text=True,
        env={"PATH": f"{calls.bin}:/usr/bin:/bin", **calls.env},
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert ruff in calls.calls("python") and formatted in calls.calls("python")
    assert sorted(calls.calls("node")) == every_js
    # CI's lint job: the same checks with the pinned ruff (uvx, tests/test_fix_lean_ci.py).
    # policy: the workflow file is configuration for GitHub; its steps are run here.
    workflow = load_workflow(source(".github/workflows/ci.yml"))
    calls = _lint_tools(tmp_path / "ci")
    calls.tool("uvx")
    job_env = workflow["jobs"]["lint"].get("env") or {}
    for name in ("Check Python", "Check JavaScript modules"):
        done = run_step(
            workflow_step(workflow, "lint", name),
            tmp_path / "ci/project",
            tools=calls.bin,
            env={**job_env, **calls.env},
        )
        assert done.returncode == 0, done.stderr
    uvx = calls.calls("uvx")
    assert [job_env["RUFF"], *ruff[2:]] in uvx and [job_env["RUFF"], *formatted[2:]] in uvx
    assert sorted(calls.calls("node")) == every_js


def test_release_uses_locked_hashed_dependencies(tmp_path: Path) -> None:
    # policy: uv's lock covers both kinds of Mac (a project setting, nothing to run).
    uv = tomllib.loads(source("pyproject.toml"))["tool"]["uv"]
    assert set(uv["required-environments"]) == {
        "sys_platform == 'darwin' and platform_machine == 'arm64'",
        "sys_platform == 'darwin' and platform_machine == 'x86_64'",
    }
    # The release build checks the lock and ships hashed requirements exported from it.
    build = build_release(tmp_path / "build")
    assert build.result.returncode == 0, build.result.output
    assert build.result.calls("uv") == [
        ["lock", "--check"],
        [
            "export",
            "--locked",
            "--extra",
            "sync",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
            "dist/release-requirements.txt",
        ],
    ]
    assert "release-requirements.txt" in build.shipped()
    # A release install takes its dependencies from them, hash-checked; a checkout without uv.lock
    # resolves them (with uv.lock it exports them: test_fix_locked_dev_installs.py).
    release = release_history()
    requirements = str(release.projects["9.8.6"] / "release-requirements.txt")
    assert ["install", "--quiet", "--upgrade", "--require-hashes", "-r", requirements] in release.steps[
        "install"
    ].result.calls("pip")
    development = source_history().steps["install"].result.calls("pip")
    assert not [call for call in development if "-r" in call]
    assert [call for call in development if call[-1].endswith(".whl[sync]")]
    # The release job installs uv before building.
    calls = Recorder(tmp_path / "tooling")
    calls.tool("python")
    step = workflow_step(
        load_workflow(source(".github/workflows/release.yml")), "build-release", "Install build tooling"
    )
    assert run_step(step, tmp_path, tools=calls.bin, env=calls.env).returncode == 0
    [install] = calls.calls("python")
    assert install[:4] == ["-m", "pip", "install", "--upgrade"] and "uv" in install
