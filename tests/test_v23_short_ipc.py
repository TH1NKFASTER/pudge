"""Regressions for macOS mpv IPC under deeply nested test isolation."""
import os
import tempfile

import pytest
from ipc_isolation import short_socket_directory


@pytest.mark.parametrize("component", ["nested-pytest-" * 12, "長い一時フォルダ" * 6])
def test_ipc_directory_ignores_long_tmpdir_and_cleans_up(tmp_path, monkeypatch, component):
    inherited = tmp_path / component / "pytest-runtime" / "tmp"
    inherited.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(inherited))
    monkeypatch.setattr(tempfile, "tempdir", str(inherited))
    legacy_socket = inherited / "pytest-user" / "test_native_quit" / "mpv.sock"
    assert len(os.fsencode(legacy_socket)) > 104

    with short_socket_directory() as directory:
        socket_path = directory / "mpv.sock"
        assert len(os.fsencode(socket_path)) < 96
        assert not directory.is_relative_to(inherited)
        assert directory.is_dir()
        # A placeholder verifies resource lifetime without requiring mpv here.
        socket_path.write_bytes(b"temporary socket placeholder")
        assert os.environ["TMPDIR"] == str(inherited)
        assert tempfile.tempdir == str(inherited)

    assert not directory.exists()
    assert inherited.is_dir()
    assert os.environ["TMPDIR"] == str(inherited)
