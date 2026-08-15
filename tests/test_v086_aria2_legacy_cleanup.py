from pathlib import Path

import pytest

from pudge.providers.aria2 import Aria2Client, Aria2Error


def test_delete_tolerates_stale_legacy_info_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    legacy_hash = "957584f5701a375215c1a3946bb1d3ab8d023426"
    calls: list[str] = []

    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda value: value)

    def rpc(method: str, params=None):
        calls.append(method)
        if method in {
            "aria2.tellStatus",
            "aria2.forceRemove",
            "aria2.removeDownloadResult",
        }:
            raise Aria2Error(f"aria2 RPC: Invalid GID {legacy_hash}")
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        client.delete(legacy_hash, delete_files=False)
    finally:
        client.close()

    assert "aria2.forceRemove" in calls
    assert "aria2.removeDownloadResult" in calls


def test_delete_still_raises_real_aria2_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)

    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda value: value)

    def rpc(method: str, params=None):
        if method == "aria2.tellStatus":
            return {}
        if method == "aria2.forceRemove":
            raise Aria2Error("aria2 RPC: permission denied")
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        with pytest.raises(Aria2Error, match="permission denied"):
            client.delete("0123456789abcdef", delete_files=False)
    finally:
        client.close()
