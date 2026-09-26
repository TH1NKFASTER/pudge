from __future__ import annotations

import json
import zipfile
from pathlib import Path

from pudge.database import Database
from pudge.diagnostics import DebugBundleBuilder, DiagnosticRecorder
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor


def test_process_snapshot_does_not_store_command_arguments(monkeypatch) -> None:
    secret = "TEST_SENTINEL_PROCESS_67213"
    rows = [
        {
            "pid": 123, "ppid": 1, "cpu_percent": 5.0,
            "memory_percent": 1.0, "rss_mb": 300.0, "elapsed": "00:30",
            "command": f"/usr/bin/aria2c --rpc-secret={secret}",
        },
    ]
    monkeypatch.setattr("pudge.energy_diagnostics.os.getpid", lambda: 123)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))
    sample = EnergyDiagnosticsMonitor().sample()
    assert sample["processes"][0]["role"] == "app"
    assert "command" not in sample["processes"][0]
    assert secret not in json.dumps(sample)


def test_debug_bundle_scrubs_historical_jsonl_logs_rotations_and_free_text(tmp_path: Path) -> None:
    secrets = (
        "TEST_SENTINEL_RPC_EQUALS_101",
        "TEST_SENTINEL_RPC_SPACE_102",
        "TEST_SENTINEL_BEARER_103",
        "TEST_SENTINEL_URL_PASS_104",
        "TEST_SENTINEL_EVENT_105",
        "TEST_SENTINEL_NESTED_106",
        "TEST_SENTINEL_COOKIE_107",
    )
    energy = tmp_path / "energy.jsonl"
    energy.write_text(json.dumps({
        "timestamp": 100,
        "app_cpu_percent": 11,
        "processes": [{"pid": 101, "command": f"aria2c --rpc-secret={secrets[0]}", "role": "aria2"}],
    }) + "\n", encoding="utf-8")
    runtime = tmp_path / "runtime.log"
    runtime.write_text(
        f"INFO rpc --rpc-secret={secrets[0]}\n"
        f"WARN --rpc-secret {secrets[1]}\n"
        f"Authorization: Bearer {secrets[2]}\n"
        f"GET https://alice:{secrets[3]}@example.org/path\n"
        f"Cookie: session={secrets[6]}\n", encoding="utf-8",
    )
    Path(str(runtime) + ".1").write_text(
        json.dumps({"payload": {"nested": {"api_key": secrets[5]}}}) + "\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "library.sqlite3")
    recorder = DiagnosticRecorder(database)
    recorder.record("trace", "test", "request", payload={"description": "api_key=" + secrets[4]})
    result = tmp_path / "bundle.zip"
    DebugBundleBuilder(database, recorder).build(
        result, version="0.7.28", frontend={"note": "--rpc-secret " + secrets[0]},
        snapshots=[{"nested": [{"Authorization": secrets[2]}]}],
        logs={"runtime": runtime, "energy": energy},
    )
    with zipfile.ZipFile(result) as bundle:
        assert "logs/runtime.log.1" in bundle.namelist()
        joined = b"\n".join(bundle.read(name) for name in bundle.namelist())
        assert b"app_cpu_percent" in joined
        assert b'"pid": 101' in joined
        for secret in secrets:
            assert secret.encode() not in joined, f"Sensitive sample appeared in bundle: {secret[:14]}"
