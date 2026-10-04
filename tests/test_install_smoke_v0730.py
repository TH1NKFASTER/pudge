from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_checks():
    spec = importlib.util.spec_from_file_location('install_checks_smoke', ROOT / 'pudge/install_checks.py')
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    return checks


def source_project(root):
    (root / 'pudge').mkdir(parents=True)
    (root / 'pudge/__init__.py').write_text('__version__ = "0.7.30"\n')
    (root / 'pyproject.toml').write_text('[project]\nversion = "0.7.30"\n')
    return root


def test_default_install_gate_is_offline_and_needs_no_dev_tools(tmp_path, monkeypatch):
    checks = load_checks()
    project = source_project(tmp_path / 'project')
    monkeypatch.setattr(checks, '__file__', str(project / 'pudge/install_checks.py'))
    monkeypatch.setattr(sys, 'argv', ['install_checks'])
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(checks, '_test_python', lambda _: pytest.fail('default install prepared dev dependencies'))
    assert checks.main() == 0
    assert not (project / '.venv-install-tests').exists()


def test_smoke_gate_reports_invalid_source(tmp_path):
    checks = load_checks()
    project = source_project(tmp_path)
    (project / 'pudge/broken.py').write_text('def broken(\n')
    smoke = getattr(checks, 'run_smoke_check', None)
    assert smoke is not None, 'installer has no bounded smoke check'
    with pytest.raises(RuntimeError, match='smoke check failed'):
        smoke(project, Path(sys.executable))


def test_installer_preflight_script_entrypoint_works_without_pytest():
    result = subprocess.run([sys.executable, str(ROOT / 'pudge/install_checks.py')],
                            capture_output=True, text=True, check=False, timeout=40)
    assert result.returncode == 0, result.stderr
    assert 'Installation smoke check passed' in result.stdout


def test_alias_refresh_failure_preserves_a_successful_install(tmp_path):
    import shutil
    shell = shutil.which('zsh')
    if not shell:
        pytest.skip('macOS installer shell unavailable')
    source = (ROOT / 'install.sh').read_text()
    start = source.index('"$VENV_DIR/bin/python" "$PROJECT_DIR/pudge/mac_app_aliases.py"')
    end = source.index('killall Dock', start)
    venv = tmp_path / 'venv'; (venv / 'bin').mkdir(parents=True)
    python = venv / 'bin/python'; python.write_text('#!/bin/sh\nexit 73\n'); python.chmod(0o700)
    script = tmp_path / 'alias.zsh'
    script.write_text('set -euo pipefail\nVENV_DIR=' + repr(str(venv)) + '\nPROJECT_DIR=/project\n'
                      'APP_PATH=/app\nAPP_BUNDLE_ID=test\nLEGACY_NAMES=(Old)\n' + source[start:end] + '\necho install-kept\n')
    result = subprocess.run([shell, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'install-kept' in result.stdout


def test_smoke_gate_terminates_a_hung_child(tmp_path):
    checks = load_checks()
    source_project(tmp_path)
    slow = tmp_path / 'slow-python'
    slow.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
    slow.chmod(0o700)
    smoke = getattr(checks, 'run_smoke_check', None)
    assert smoke is not None, 'installer has no bounded smoke check'
    with pytest.raises(RuntimeError, match='timed out'):
        smoke(tmp_path, slow, timeout=0.05)


def test_full_suite_requires_an_explicit_developer_flag(tmp_path, monkeypatch):
    checks = load_checks()
    project = source_project(tmp_path)
    (project / 'tests').mkdir()
    monkeypatch.setattr(checks, '__file__', str(project / 'pudge/install_checks.py'))
    monkeypatch.setattr(sys, 'argv', ['install_checks', '--full-suite'])
    monkeypatch.setattr(checks.shutil, 'which', lambda _: '/tool')
    monkeypatch.setattr(checks, '_test_python', lambda _: Path(sys.executable))
    # The existing full runner is independently exercised in test_v20_install_gate.
    monkeypatch.setattr(checks, 'run_suite', lambda *args: 73)
    assert checks.main() == 73


def test_reused_pid_session_marker_does_not_block_install(tmp_path, monkeypatch):
    import json
    marker = tmp_path / 'app-session.json'
    marker.write_text(json.dumps({'pid': 12345, 'started_at': 1.0}))
    source = (ROOT / 'install.sh').read_text()
    code = source.split("<<'PUDGE_SESSION_GUARD'\n", 1)[1].split('\nPUDGE_SESSION_GUARD', 1)[0]
    monkeypatch.setattr(os, 'kill', lambda *_: None)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, 'Sat Oct  3 12:00:00 2026\n', ''))
    monkeypatch.setattr(sys, 'argv', ['-', str(marker)])
    exec(compile(code, 'session-guard', 'exec'), {})
    assert marker.exists()
