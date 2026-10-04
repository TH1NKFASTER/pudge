import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh"])
def test_installer_selects_zsh_before_shell_specific_syntax(tmp_path, shell) -> None:
    if shutil.which(shell) is None or not Path("/bin/zsh").is_file():
        pytest.skip("zsh installer entrypoint requires zsh")
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project with spaces"
    (project / "pudge").mkdir(parents=True)
    shutil.copy2(root / "install.sh", project / "install.sh")
    shutil.copy2(root / "pudge/brand.env", project / "pudge/brand.env")
    result = subprocess.run(
        [shell, str(project / "install.sh"), "--update", "--invalid"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 2
    assert "Unknown installer argument: --invalid" in result.stderr
    assert "unbound variable" not in result.stderr


def test_app_bundle_uses_native_launcher_and_managed_venv() -> None:
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "/usr/bin/clang" in text
    assert "Py_InitializeFromConfig(&config)" in text
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in text
    assert "return Py_RunMain();" in text
    assert "execv(python" not in text
    assert "PyInstaller" not in text
    assert "--collect-all pudge" not in text


def test_native_launcher_keeps_pudge_notification_identity() -> None:
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "--pudge-native-notification" in text
    assert "UNUserNotificationCenter" in text
    assert "PudgeNotificationDelegate" in text


def test_installer_gracefully_quits_and_guards_sessions_before_replacement() -> None:
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    assert 'app.terminate()' in text
    assert 'pkill' not in text
    assert 'rm -f "$DATA_DIR/app-session.json"' not in text
    assert text.index('PUDGE_SESSION_GUARD') < text.index('UPDATE_PACKAGE_BACKUP=')


def test_installer_session_guard_keeps_live_marker_and_refuses_update(tmp_path) -> None:
    text = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
    guard = text.split("<<'PUDGE_SESSION_GUARD'\n", 1)[1].split('\nPUDGE_SESSION_GUARD', 1)[0]
    marker = tmp_path / 'app-session.json'
    content = json.dumps({'pid': os.getpid()})
    marker.write_text(content)
    result = subprocess.run([sys.executable, '-c', guard, str(marker)], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert 'still active' in result.stderr
    assert marker.read_text() == content
    marker.write_text(json.dumps({'pid': 0}))
    assert subprocess.run([sys.executable, '-c', guard, str(marker)], capture_output=True, check=False).returncode == 0


def test_install_preflight_precedes_migration_and_runtime_changes() -> None:
    text = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
    gate = text.index('python3.12 "$PROJECT_DIR/pudge/install_checks.py"')
    assert gate < text.index('legacy_install.py')
    assert gate < text.index('mkdir -p "$DATA_DIR"')
    assert gate < text.index("<<'PUDGE_GRACEFUL_QUIT'")
