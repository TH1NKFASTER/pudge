from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from pudge.updater import AppUpdater

requires_zsh = pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the generated macOS updater runs under /bin/zsh")


@requires_zsh
@pytest.mark.parametrize('installer_status', [0, 73])
def test_downloaded_update_source_is_removed_after_installer_exit(tmp_path, monkeypatch, installer_status):
    updater = AppUpdater()
    updater.update_root = tmp_path / 'updates'
    updater.log_path = tmp_path / 'update.log'
    download = updater.update_root / 'v0.7.30-download'
    project = download / 'release/pudge'; project.mkdir(parents=True)
    (project / 'install.sh').write_text('#!/bin/zsh\nexit ' + str(installer_status) + '\n')
    home = tmp_path / 'home'; home.mkdir()
    app = home / 'Applications/pudge.app'; app.mkdir(parents=True)
    (app / 'old').write_text('old bundle')
    reopened = tmp_path / 'reopened'
    opener = tmp_path / 'open'; opener.write_text('#!/bin/sh\ntouch ' + repr(str(reopened)) + '\n'); opener.chmod(0o700)
    copier = tmp_path / 'ditto'; copier.write_text('#!/bin/sh\ncp -R "$1" "$2"\n'); copier.chmod(0o700)
    with monkeypatch.context() as patch:
        patch.setattr('pudge.updater.Path.home', lambda: home)
        patch.setattr('pudge.updater.os.getpid', lambda: 0)
        patch.setattr('pudge.updater.subprocess.Popen', lambda *a, **k: None)
        updater._launch_script(project, [])
    script = project.parent / 'run-pudge-update.zsh'
    script.write_text(script.read_text().replace('/bin/kill -0 0', '/bin/kill -0 2147483647')
                      .replace('/usr/bin/open', str(opener)).replace('/usr/bin/ditto', str(copier)))
    result = subprocess.run(['/bin/zsh', str(script)], check=False, timeout=5)
    assert result.returncode == (0 if installer_status == 0 else 1)
    assert reopened.exists()
    assert (app / 'old').read_text() == 'old bundle'
    assert not download.exists()


def test_old_download_sources_are_pruned_without_touching_other_data(tmp_path):
    updater = AppUpdater(); updater.update_root = tmp_path
    old = tmp_path / 'v0.7.29-obsolete'; old.mkdir()
    (old / 'test-venv').write_text('old')
    stale = time.time() - 8 * 86400
    os.utime(old, (stale, stale))
    recent = tmp_path / 'v0.7.30-active'; recent.mkdir()
    rollback = tmp_path / 'rollback'; rollback.mkdir(); os.utime(rollback, (stale, stale))
    elsewhere = tmp_path.parent / 'unrelated'; elsewhere.mkdir()
    symlink = tmp_path / 'v0.7.1-link'; symlink.symlink_to(elsewhere)
    prune = getattr(updater, '_prune_update_sources', None)
    assert prune is not None, 'updater never prunes obsolete extracted downloads'
    prune()
    assert not old.exists()
    assert recent.is_dir() and rollback.is_dir() and elsewhere.is_dir() and symlink.is_symlink()
