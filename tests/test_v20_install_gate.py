from __future__ import annotations

import os
import runpy
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pudge import process_cleanup

ROOT = Path(__file__).resolve().parents[1]
gate = runpy.run_path(str(ROOT / 'pudge/install_checks.py'))


@pytest.mark.skipif(not shutil.which('zsh'), reason='zsh required for macOS installer control flow')
def test_real_installer_exits_at_failed_gate_before_migration_or_shutdown(tmp_path):
    # Stub only external prerequisites. A nonzero gate must propagate through
    # the actual zsh installer, without reaching any later Python operation.
    events = tmp_path / 'events'
    programs = {
        'uname': '#!/bin/sh\necho Darwin\n',
        'brew': '#!/bin/sh\nexit 0\n',
        'python3.12': '#!/bin/sh\nprintf "%s\\n" "$1" >> ' + str(events) + '\nexit 73\n',
    }
    for name, source in programs.items():
        path = tmp_path / name
        path.write_text(source)
        path.chmod(0o755)
    fixture = tmp_path / 'installer'
    (fixture / 'pudge').mkdir(parents=True)
    shutil.copy2(ROOT / 'pudge/brand.env', fixture / 'pudge/brand.env')
    source = (ROOT / 'install.sh').read_text()
    source = source.replace('/opt/homebrew/bin/brew /usr/local/bin/brew', shlex.quote(str(tmp_path / 'brew')))
    (fixture / 'install.sh').write_text(source)
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'])
    result = subprocess.run([shutil.which('zsh'), str(fixture / 'install.sh'), '--update'],
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 73, result.stderr
    assert events.read_text().splitlines() == [str(fixture / 'pudge/install_checks.py')]


def test_gate_runs_whole_suite_ignores_selection_and_isolates_real_state(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'tests').mkdir(parents=True)
    real_home = tmp_path / 'real-home'
    real_home.mkdir()
    marker = real_home / 'config.toml'
    marker.write_text('keep this')
    monkeypatch.setenv('PUDGE_HOME', str(real_home))
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k nonexistent_test')
    (project / 'tests/test_complete.py').write_text(
        'import os\nfrom pathlib import Path\n'
        'def test_first():\n'
        '    assert os.environ["PUDGE_HOME"] != ' + repr(str(real_home)) + '\n'
        '    Path(os.environ["PUDGE_HOME"]).mkdir(parents=True)\n'
        'def test_second():\n    assert True\n'
    )
    logs = tmp_path / 'logs'
    assert gate['run_suite'](project, Path(sys.executable), logs) == 0
    assert '2 passed' in (logs / 'pytest.log').read_text()
    assert marker.read_text() == 'keep this'


def test_gate_failure_stops_update_and_keeps_error_log(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_failure.py').write_text('def test_failure():\n    assert False, "broken behavior"\n')
    logs = tmp_path / 'logs'
    assert gate['run_suite'](tmp_path, Path(sys.executable), logs) == 1
    assert 'broken behavior' in (logs / 'pytest.log').read_text()


def test_gate_rejects_a_suite_where_every_test_was_skipped(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_skipped.py').write_text(
        'import pytest\n@pytest.mark.skip(reason="fixture")\ndef test_skipped():\n    pass\n'
    )
    with pytest.raises(RuntimeError, match='No passing tests'):
        gate['run_suite'](tmp_path, Path(sys.executable), tmp_path / 'logs')


def test_linux_process_snapshot_translates_only_our_namespace(tmp_path):
    def row(outer, inner, parent, namespace, started):
        entry = tmp_path / str(outer)
        (entry / 'ns').mkdir(parents=True)
        (entry / 'ns/pid').symlink_to(namespace)
        (entry / 'status').write_text(f'PPid:\t{parent}\nNSpid:\t{outer}\t{inner}\n')
        fields = ['S', str(parent)] + ['0'] * 17 + [str(started)]
        (entry / 'stat').write_text(f'{outer} (name with ) parentheses) ' + ' '.join(fields))
    (tmp_path / 'self/ns').mkdir(parents=True)
    (tmp_path / 'self/ns/pid').symlink_to('pid:[ours]')
    row(1001, os.getpid(), 900, 'pid:[ours]', 101)
    row(1002, 777, 1001, 'pid:[ours]', 102)
    row(1003, 777, 1001, 'pid:[foreign]', 999)
    snapshot = process_cleanup._linux_process_snapshot(tmp_path)
    assert snapshot[777] == process_cleanup.ProcessIdentity(777, os.getpid(), 'ticks:102', 'S')
    assert snapshot[os.getpid()].parent == 0
    assert len(snapshot) == 2


def test_linux_process_snapshot_fails_closed_when_self_is_not_visible(tmp_path):
    (tmp_path / 'self/ns').mkdir(parents=True)
    (tmp_path / 'self/ns/pid').symlink_to('pid:[ours]')
    with pytest.raises(OSError, match='own PID namespace'):
        process_cleanup._linux_process_snapshot(tmp_path)
