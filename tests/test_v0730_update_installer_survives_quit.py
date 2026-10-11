"""The update installer survives Pudge's quit, update preflight, build identity of source
and release installs, GitHub release titles (behaviour; audit 71Б rewrite)."""

import json
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from source_policy import source
from t71g01_support import Recorder, build_release, load_workflow, run_step, source_history, workflow_step

from pudge.build_identity import read_build_identity
from pudge.process_cleanup import OwnedProcessTree
from pudge.web_app import WebAppApi


def test_quit_cleanup_never_kills_the_launched_update_installer():
    installer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        api = WebAppApi.__new__(WebAppApi)
        api._play_processes = {}
        api._play_registry = {}
        api.app_updater = SimpleNamespace(installer_pid=installer.pid)
        tree = OwnedProcessTree()
        api._preserve_playback_processes(tree)
        tree.stop(grace=0.2)
        assert installer.poll() is None, "installer was killed by quit cleanup"
        assert leftover.wait(timeout=5) is not None
    finally:
        for process in (installer, leftover):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_update_preflight_cancels_jiten_prefetch_and_names_real_blockers():
    api = WebAppApi.__new__(WebAppApi)
    cancel = threading.Event()
    prefetch = threading.Thread(target=lambda: cancel.wait(10), name="jiten-prefetch")
    prefetch.start()
    stop = threading.Event()
    startup = threading.Thread(target=lambda: stop.wait(10), name="pudge-startup-maintenance")
    startup.start()
    api._jiten_state_prefetch_cancel_event = cancel
    api._jiten_state_prefetch_thread = prefetch
    api._startup_maintenance_thread = startup
    api._irodori_tts_lock = threading.Lock()
    api._irodori_tts_threads = {}
    try:
        with pytest.raises(RuntimeError, match=r"pudge-startup-maintenance"):
            api._prepare_update_shutdown()
        assert cancel.is_set() and not prefetch.is_alive()
    finally:
        stop.set()
        startup.join(timeout=5)


def test_build_info_ignores_finder_metadata(tmp_path):
    import json

    root = tmp_path / "pudge"
    (root / "web").mkdir(parents=True)
    (root / "__init__.py").write_text("x = 1\n")
    (root / ".DS_Store").write_bytes(b"finder")
    (root / "web" / ".DS_Store").write_bytes(b"finder")
    script = __import__("pathlib").Path(__file__).resolve().parents[1] / ".github/release/write_build_info.py"
    subprocess.run([sys.executable, str(script), str(root), "0.0.0", "--revision", "abc"], check=True)
    files = json.loads((root / "build-info.json").read_text())["files"]
    assert list(files) == ["__init__.py"]


def _settings_version_line(page, build: dict, version: str) -> tuple[str, str]:
    result = page.run(
        "setPage('settings'); await __tick(100); return [__text('.settings-header .subtle'), t('settings.version')];",
        state={"settings": {"version": version, "build": build}},
    )
    return tuple(result.value)


def test_source_install_shows_the_checkout_revision_it_was_built_from(page, tmp_path: Path) -> None:
    history = source_history()
    checkout, install = history.projects["checkout"], history.steps["install"]
    short = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # The checkout had local changes under pudge/: the identity says so, and it was written
    # into the tree before the wheel was built from it.
    [seen] = install.result.calls("pip-wheel-saw")
    assert json.loads(seen[0])["build_info"]["id"] == f"9.8.7-{short}-dirty"
    assert f"Installed build: 9.8.7-{short}-dirty" in install.result.output
    for name, data in install.runtime_after.items():  # the runtime this install left behind
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    identity = read_build_identity(tmp_path / "pudge")
    assert (identity["id"], identity["release"], identity["verified"]) == (
        f"9.8.7-{short}-dirty",
        False,
        True,
    )
    line, label = _settings_version_line(page, identity, "9.8.7")
    assert line.startswith(f"{label}: 9.8.7 · 9.8.7-{short}-dirty · ")


def test_public_release_build_id_is_just_the_version(tmp_path: Path, page) -> None:
    build = build_release(tmp_path / "release")
    assert build.result.returncode == 0, build.result.output
    with zipfile.ZipFile(build.project / "dist/pudge-9.8.7-py3-none-any.whl") as wheel:
        wheel.extractall(tmp_path / "installed")
    identity = read_build_identity(tmp_path / "installed/pudge")
    assert (identity["id"], identity["display"], identity["release"], identity["verified"]) == (
        "9.8.7",
        "9.8.7",
        True,
        True,
    )
    # Settings shows just the version for a public release ...
    line, label = _settings_version_line(page, identity, "9.8.7")
    assert line == f"{label}: 9.8.7"
    # ... and says so when its files were changed afterwards.
    (tmp_path / "installed/pudge/__init__.py").write_text("patched = True\n", encoding="utf-8")
    modified = read_build_identity(tmp_path / "installed/pudge")
    result = page.run(
        "setPage('settings'); await __tick(100); return [__text('.settings-header .subtle'), t('uiB.settings.buildModified')];",
        state={"settings": {"version": "9.8.7", "build": modified}},
    )
    assert result.value[0] == f"{label}: 9.8.7 ({result.value[1]})"


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("tag", ["v9.8.7", "9.8.7"])
def test_github_release_title_keeps_the_pudge_v_prefix(tmp_path: Path, tag: str, exists: bool) -> None:
    # policy: the workflow file is configuration for GitHub; its publish step is run here.
    publish = workflow_step(
        load_workflow(source(".github/workflows/release.yml")), "build-release", "Publish GitHub Release"
    )
    calls = Recorder(tmp_path / "gh")
    calls.tool("gh", '[ "$2" != view ] || [ -n "$EXISTS" ]\n')
    values = {
        "github.token": "token",
        "github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name": tag,
        **{f"steps.archive.outputs.{name}": name for name in ("path", "checksum")},
    }
    completed = run_step(
        publish,
        tmp_path,
        tools=calls.bin,
        expressions=values,
        env={**calls.env, **({"EXISTS": "1"} if exists else {})},
    )
    assert completed.returncode == 0, completed.stderr
    titled = [call for call in calls.calls("gh") if "--title" in call]
    assert [call[1] for call in titled] == ["edit" if exists else "create"]
    assert titled[0][titled[0].index("--title") + 1] == "Pudge v9.8.7"
