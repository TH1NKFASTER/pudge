from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/release' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_metadata_rejects_a_longer_version_heading(tmp_path, monkeypatch):
    helper = load_helper('check_release_metadata')
    (tmp_path / 'pudge').mkdir()
    (tmp_path / 'pudge/__init__.py').write_text('__version__ = "0.7.2"\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "0.7.2"\n')
    (tmp_path / 'CHANGELOG.md').write_text('## v0.7.29 — Previous release\n\n- Notes\n')
    (tmp_path / 'uv.lock').touch()
    monkeypatch.setattr(helper, 'ROOT', tmp_path)
    with pytest.raises(SystemExit, match='has no v0.7.2 entry'):
        helper.main()


@pytest.mark.parametrize('tag,requested', [('v0.7.29', '0.7.29'), ('0.7.29', '0.7.28')])
def test_release_refuses_existing_or_older_versions_before_validation(tmp_path, monkeypatch, tag, requested):
    helper = load_helper('release')
    repo = tmp_path / 'repo'; repo.mkdir()
    remote = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Test'),
                 ('config', 'user.email', 'test@example.invalid')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    (repo / 'pyproject.toml').write_text('[project]\nversion = "' + requested + '"\n')
    for args in [('add', 'pyproject.toml'), ('commit', '-m', 'Base'), ('tag', tag),
                 ('remote', 'add', 'origin', str(remote)), ('push', 'origin', 'main', '--tags')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    monkeypatch.setattr(helper, 'ROOT', repo)
    monkeypatch.setattr(sys, 'argv', ['release', requested, '--no-push'])
    monkeypatch.setattr(helper, 'validate', lambda _: pytest.fail('published versions reached validation'))
    monkeypatch.setattr(helper, 'commit_if_needed', lambda _: pytest.fail('published versions reached commit'))
    with pytest.raises(helper.ReleaseError, match='already exists|must be newer'):
        helper.main()


def test_release_build_entrypoint_is_executable():
    assert os.access(ROOT / 'build_release.sh', os.X_OK)


def test_bump_uses_the_exact_heading_and_promotes_unreleased_notes(tmp_path, monkeypatch):
    helper = load_helper('bump_version')
    changelog = tmp_path / 'CHANGELOG.md'
    changelog.write_text('# Changes\n\n## Unreleased\n\n- New behavior\n\n## v0.7.29\n\n- Old behavior\n')
    monkeypatch.setattr(helper, 'ROOT', tmp_path)
    helper.ensure_changelog_entry('0.7.2')
    assert changelog.read_text() == '# Changes\n\n## Unreleased\n\n## v0.7.2\n\n- New behavior\n\n## v0.7.29\n\n- Old behavior\n'


def test_release_archive_keeps_the_legacy_updater_name(tmp_path):
    helper_path = ROOT / '.github/release/finish_release_archives.py'
    assert helper_path.is_file(), 'release has no compatibility archive step'
    archive = tmp_path / '0.7.30.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('pudge/install.sh', '#!/bin/zsh\n')
    result = subprocess.run([sys.executable, str(helper_path), str(archive), 'pudge-macos-v0.7.30.zip'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    legacy = tmp_path / 'pudge-macos-v0.7.30.zip'
    assert legacy.read_bytes() == archive.read_bytes()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (tmp_path / '0.7.30.zip.sha256').read_text() == digest + '  0.7.30.zip\n'
    assert (tmp_path / 'pudge-macos-v0.7.30.zip.sha256').read_text() == digest + '  pudge-macos-v0.7.30.zip\n'


def test_release_build_identity_verifies_the_actual_package_files(tmp_path):
    helper_path = ROOT / '.github/release/write_build_info.py'
    assert helper_path.is_file(), 'release does not regenerate build identity'
    package = tmp_path / 'pudge'; package.mkdir()
    (package / '__init__.py').write_text('__version__ = "0.7.30"\n')
    (package / 'build-info.json').write_text('{"id":"pudge-testing-stale"}')
    (package / '__pycache__').mkdir()
    (package / '__pycache__/old.pyc').write_bytes(b'cache')
    result = subprocess.run([sys.executable, str(helper_path), str(package), '0.7.30', '--revision', 'abc1234'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads((package / 'build-info.json').read_text())
    assert payload['id'] == '0.7.30-abc1234'
    assert payload['files'] == {'__init__.py': hashlib.sha256(b'__version__ = "0.7.30"\n').hexdigest()}
    from pudge.build_identity import read_build_identity
    assert read_build_identity(package)['verified'] is True
    (package / '__init__.py').write_text('__version__ = "wrong"\n')
    assert read_build_identity(package)['verified'] is False
