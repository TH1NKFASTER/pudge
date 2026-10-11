"""CI, development installs and the optional Irodori server install fixed versions (behaviour).

CI and the release checks install with `uv sync --locked`; `make install-dev` and install.sh
from a checkout use uv.lock too; a fresh install of the managed Irodori server can be pinned to
one commit and always logs the commit it installed. Workflow steps, make and install.sh run with stand-ins
(tests/t71g01_support.py)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from source_policy import source
from t71g01_support import MacSim, Recorder, load_workflow, run_step, stand_in, workflow_step
from test_v089_irodori_managed_install import _api

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run  # the Irodori tests replace subprocess.run with the install's stand-in
# Like uv, it also echoes the exported requirements (with their hashes) to the terminal.
UV_STANDIN = (
    'record uv "$@"\nprev=""\n'
    'for arg in "$@"; do [ "$prev" = "--output-file" ] && printf \'httpx==0.28.1 --hash=sha256:00\\n\' > "$arg"; prev="$arg"; done\n'
    "printf 'httpx==0.28.1 --hash=sha256:00\\n'\n"
)
# (workflow, job, step, extras): every job that installs Pudge's dependencies. CI's lint
# installs nothing of the project (tests/test_fix_lean_ci.py).
INSTALL_STEPS = [
    ("ci.yml", "tests", "Install test dependencies", ["dev", "sync"]),
    ("ci.yml", "macos-smoke", "Install core test dependencies", ["dev"]),
    ("release.yml", "quality", "Install dependencies", ["dev"]),
    ("release.yml", "verify-and-test", "Install dependencies", ["dev", "sync"]),
]


@pytest.mark.parametrize("workflow_name,job,step_name,extras", INSTALL_STEPS)
def test_ci_installs_exactly_the_locked_versions(tmp_path, workflow_name, job, step_name, extras):
    # policy: the workflow file is configuration for GitHub; its steps are run here.
    workflow = load_workflow(source(f".github/workflows/{workflow_name}"))
    calls = Recorder(tmp_path / "tools")
    python = calls.tool("python")
    calls.tool("uv")
    github_path = tmp_path / "github-path"
    project = tmp_path / "project"
    project.mkdir()

    done = run_step(
        workflow_step(workflow, job, step_name),
        project,
        tools=calls.bin,
        env={**calls.env, "GITHUB_PATH": str(github_path)},
    )

    assert done.returncode == 0, done.stderr
    assert calls.calls("python") == [["-m", "pip", "install", "--upgrade", "uv"]]
    expected = ["sync", "--locked"] + [arg for extra in extras for arg in ("--extra", extra)]
    assert calls.calls("uv") == [[*expected, "--python", str(python)]]
    # The later steps' `python` is the locked environment.
    assert github_path.read_text().splitlines() == [f"{project}/.venv/bin"]
    # uv.lock resolves for macOS only, so the job runs on a Mac.
    uv = tomllib.loads(source("pyproject.toml"))["tool"]["uv"]
    assert uv["environments"] == ["sys_platform == 'darwin'"]
    assert workflow["jobs"][job]["runs-on"].startswith("macos")


def test_make_install_dev_syncs_the_lock_into_the_test_environment(tmp_path):
    calls = Recorder(tmp_path / "tools")
    calls.tool("uv", 'record uv-env "$UV_PROJECT_ENVIRONMENT"\n')
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(ROOT / "Makefile", project / "Makefile")
    done = subprocess.run(
        ["make", "install-dev"],
        cwd=project,
        capture_output=True,
        text=True,
        env={"PATH": f"{calls.bin}:/usr/bin:/bin", **calls.env},
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert calls.calls("uv") == [
        ["sync", "--locked", "--python", "3.12", "--extra", "dev", "--extra", "sync"]
    ]
    assert calls.calls("uv-env") == [[".venv-test"]]


def _checkout_with_lock(sim: MacSim) -> Path:
    checkout = sim.project("checkout", version="9.8.7", kind="git")
    (checkout / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    # This Homebrew has every formula but uv; `brew install uv` puts uv in its prefix.
    uv = stand_in(sim.base / "uv-standin", UV_STANDIN)
    stand_in(
        sim.bin / "brew",
        'record brew "$@"\n'
        'case "$1" in\n'
        '  list) [ "$2" = "--versions" ] && { [ "$3" != uv ] || [ -x "$MACSIM_PREFIX/uv/bin/uv" ]; } ;;\n'
        f'  install) if [ "$2" = uv ]; then mkdir -p "$MACSIM_PREFIX/uv/bin" && cp {uv} "$MACSIM_PREFIX/uv/bin/uv"; fi ;;\n'
        "  --prefix) printf '%s\\n' \"$MACSIM_PREFIX/$2\" ;;\n"
        "esac\n",
    )
    return checkout


def test_install_from_a_checkout_uses_the_lockfile_with_hashes(tmp_path):
    sim = MacSim(tmp_path / "sim")
    checkout = _checkout_with_lock(sim)

    first = sim.install(checkout)

    assert first.returncode == 0, first.output
    assert ["install", "uv"] in first.calls("brew")
    [export] = first.calls("uv")
    requirements = export[export.index("--output-file") + 1]
    assert export == [
        "export",
        "--project",
        str(checkout),
        "--python",
        str(sim.venv / "bin/python"),
        "--locked",
        "--extra",
        "sync",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements.txt",
        "--output-file",
        requirements,
        "--quiet",
    ]
    pip = first.calls("pip")
    assert ["install", "--quiet", "--upgrade", "--require-hashes", "-r", requirements] in pip
    # The terminal gets no exported requirements, i.e. no screens of package hashes.
    assert "sha256" not in first.output
    # Nothing is resolved from the wheel's own (unpinned) requirements.
    assert not [call for call in pip if call[0] == "install" and any(arg.endswith("[sync]") for arg in call)]
    assert not Path(requirements).exists()
    assert sim.runtime(), "the checkout's package is installed"

    # uv is there now: the next install does not install it again.
    second = sim.install(checkout)
    assert second.returncode == 0, second.output
    assert ["install", "uv"] not in second.calls("brew")
    assert len(second.calls("uv")) == 1


def test_a_lock_uv_refuses_stops_the_install_before_anything_is_replaced(tmp_path):
    sim = MacSim(tmp_path / "sim")
    checkout = _checkout_with_lock(sim)
    stand_in(
        sim.base / "uv-standin",
        'record uv "$@"\necho "error: The lockfile needs to be updated" >&2\nexit 2\n',
    )

    failed = sim.install(checkout)

    assert failed.returncode != 0
    assert "The lockfile needs to be updated" in failed.output
    assert not [call for call in failed.calls("pip") if call[:1] == ["install"] and "-r" in call]
    assert not sim.app.exists()


# ---------------------------------------------------------------------------------------
# The managed Irodori server.
def _real_clone(repo: Path) -> str:
    """What `git clone` leaves: a repository with one commit; returns its revision."""
    repo.mkdir(parents=True)
    (repo / ".env.example").write_text("# env\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid"]
    REAL_RUN(["git", "init", "-q", str(repo)], check=True)
    REAL_RUN([*git, "add", "-A"], check=True)
    REAL_RUN([*git, "commit", "-qm", "upstream"], check=True)
    return REAL_RUN(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _install(api, monkeypatch: pytest.MonkeyPatch) -> tuple[list[list[str]], dict[str, str]]:
    fake_uv = api._irodori_install_root() / "fake-uv"
    fake_uv.parent.mkdir(parents=True, exist_ok=True)
    fake_uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: fake_uv)
    monkeypatch.setattr(
        web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )
    calls: list[list[str]] = []
    cloned: dict[str, str] = {}

    def fake_run(command, **kwargs):
        command = [str(part) for part in command]
        if command[:1] == ["git"] and "rev-parse" in command:  # reading the installed revision
            return REAL_RUN(command, **kwargs)
        calls.append(command)
        if command[1:2] == ["clone"]:
            cloned["revision"] = _real_clone(Path(command[-1]))
        if command[0] == str(fake_uv):
            python = api._irodori_managed_python()
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("python", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_app_module.subprocess, "run", fake_run)
    # WebAppApi configured the app log, which detaches it from caplog again (see conftest).
    logging.getLogger("pudge").propagate = True
    api._run_irodori_install()
    return calls, cloned


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_irodori_install_logs_the_commit_it_installed(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(web_app_module, "IRODORI_TTS_COMMIT", "")  # no pin: main as cloned
    api = _api(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)

    calls, cloned = _install(api, monkeypatch)

    assert api.irodori_install_status()["state"] == "ready"
    revision = cloned["revision"]
    assert f"RESULT step=irodori.install revision={revision} pinned=no" in caplog.text
    assert f"Installed Irodori-TTS-Server revision {revision}" in api._irodori_install_log_path().read_text()
    assert not any("reset" in command for command in calls)


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_a_pinned_irodori_commit_is_what_a_fresh_install_puts_on_the_mac(tmp_path, monkeypatch, caplog):
    pin = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(web_app_module, "IRODORI_TTS_COMMIT", pin)
    api = _api(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)
    repo = str(api._irodori_repo_dir())

    calls, _cloned = _install(api, monkeypatch)

    fetch = ["/usr/bin/git", "-C", repo, "fetch", "--depth", "1", "origin", pin]
    reset = ["/usr/bin/git", "-C", repo, "reset", "--hard", pin]
    assert (
        calls.index(fetch) < calls.index(reset) < next(i for i, c in enumerate(calls) if c[1:2] == ["sync"])
    )
    assert not any(
        "origin/main" in command or command[-1] == "main" and "fetch" in command for command in calls
    )
    assert f"pinned={pin}" in caplog.text

    # The pin chooses fresh installs only: Update Irodori follows main, so Settings offer it
    # when main has moved on.
    main = "f" * 40
    monkeypatch.setattr(api, "_irodori_remote_revision", lambda **_kwargs: main)
    status = api.irodori_install_status()
    assert (status["remote_revision"], status["update_available"]) == (main[:8], True)

    # Repairing the install puts its last good commit back (the stand-in's reset did not move
    # HEAD, so that is the commit the clone left), not the pin or main.
    last_good = api._irodori_last_good()["revision"]
    calls.clear()
    api._run_irodori_install()
    assert ["/usr/bin/git", "-C", repo, "fetch", "--depth", "1", "origin", last_good] in calls
    assert ["/usr/bin/git", "-C", repo, "reset", "--hard", last_good] in calls
    assert fetch not in calls and not any("origin/main" in command for command in calls)
    assert not any(command[1:2] == ["clone"] for command in calls)
