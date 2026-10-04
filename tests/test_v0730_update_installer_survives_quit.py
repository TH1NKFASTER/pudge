import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

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


def test_source_install_refreshes_build_identity_before_building_the_wheel():
    from pathlib import Path
    installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
    refresh = installer.index('.github/release/write_build_info.py" "$PROJECT_DIR/pudge"')
    assert refresh < installer.index('-m pip wheel "$PROJECT_DIR"')
    assert 'SOURCE_REVISION="$SOURCE_REVISION-dirty"' in installer
