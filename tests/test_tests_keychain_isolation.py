"""Tests run with a temporary HOME and must never call the real macOS Keychain."""

from __future__ import annotations

import shutil
import subprocess

from pudge.secrets_store import SecretStore
from pudge import study_secrets


def test_in_process_keychain_is_off() -> None:
    assert SecretStore().available is False
    assert study_secrets.keychain_available() is False
    assert study_secrets.store("jpdb_api_token", "token") is False


def test_child_processes_get_a_security_stub() -> None:
    path = shutil.which("security")
    assert path is not None and "gui-stubs" in path
    completed = subprocess.run([path, "add-generic-password", "-s", "x", "-a", "y", "-w", "z"], check=False)
    assert completed.returncode == 44
