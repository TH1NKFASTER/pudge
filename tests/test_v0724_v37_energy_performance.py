from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.diagnostics import DebugBundleBuilder, DiagnosticRecorder
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor, summarize_energy_log
from pudge.web_app import WebAppApi


def test_energy_monitor_runs_even_in_safe_mode(monkeypatch) -> None:
    events: list[str] = []
    api = object.__new__(WebAppApi)
    api.safe_mode = SimpleNamespace(active=True)
    api.config = SimpleNamespace(
        diagnostics=SimpleNamespace(energy_monitoring_enabled=True)
    )
    api.energy_monitor = SimpleNamespace(
        running=False,
        start=lambda: events.append("start"),
        stop=lambda: events.append("stop"),
    )
    api.logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("pudge.web_app.sys.platform", "darwin")

    api._ensure_energy_monitor(reason="test")

    assert events == ["start"]


def test_energy_roles_include_agent_and_subtitle_worker(monkeypatch) -> None:
    rows = [
        {
            "pid": 100,
            "ppid": 1,
            "cpu_percent": 4.0,
            "memory_percent": 1.0,
            "rss_mb": 50.0,
            "elapsed": "10:00",
            "command": "/Applications/pudge.app/Contents/MacOS/pudge",
        },
        {
            "pid": 101,
            "ppid": 100,
            "cpu_percent": 30.0,
            "memory_percent": 1.0,
            "rss_mb": 120.0,
            "elapsed": "00:15",
            "command": "python -m pudge.cli --prepare-only /tmp/episode.mkv",
        },
        {
            "pid": 200,
            "ppid": 1,
            "cpu_percent": 8.0,
            "memory_percent": 1.0,
            "rss_mb": 90.0,
            "elapsed": "00:20",
            "command": "python -m pudge.agent --scheduled --config /tmp/config.toml",
        },
    ]
    monkeypatch.setattr("pudge.energy_diagnostics.os.getpid", lambda: 100)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))

    sample = EnergyDiagnosticsMonitor(interval_seconds=30).sample()

    roles = {row["pid"]: row["role"] for row in sample["processes"]}
    assert roles == {100: "app", 101: "subtitle-worker", 200: "agent"}
    assert sample["role_totals"]["subtitle-worker"]["cpu_percent"] == 30.0
    assert sample["role_totals"]["agent"]["cpu_percent"] == 8.0
    assert sample["app_rss_mb"] == 170.0
    assert sample["context_rss_mb"] == 90.0


def test_energy_summary_reports_5_15_60_minute_role_stats(tmp_path: Path) -> None:
    path = tmp_path / "energy.jsonl"
    now = 10_000.0
    samples = [
        {
            "timestamp": now - 100,
            "app_cpu_percent": 20.0,
            "context_cpu_percent": 5.0,
            "related_cpu_percent": 25.0,
            "app_rss_mb": 500.0,
            "context_rss_mb": 100.0,
            "role_totals": {
                "app": {"cpu_percent": 20.0, "rss_mb": 500.0},
                "agent": {"cpu_percent": 5.0, "rss_mb": 100.0},
            },
            "processes": [],
        },
        {
            "timestamp": now - 600,
            "app_cpu_percent": 40.0,
            "context_cpu_percent": 0.0,
            "related_cpu_percent": 40.0,
            "app_rss_mb": 520.0,
            "context_rss_mb": 0.0,
            "role_totals": {
                "app": {"cpu_percent": 10.0, "rss_mb": 500.0},
                "subtitle-worker": {"cpu_percent": 30.0, "rss_mb": 20.0},
            },
            "processes": [],
        },
        {
            "timestamp": now - 1800,
            "app_cpu_percent": 10.0,
            "context_cpu_percent": 0.0,
            "related_cpu_percent": 10.0,
            "app_rss_mb": 480.0,
            "context_rss_mb": 0.0,
            "role_totals": {"app": {"cpu_percent": 10.0, "rss_mb": 480.0}},
            "processes": [],
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in samples) + "\n", encoding="utf-8")

    summary = summarize_energy_log(path, now=now)

    assert summary["windows"]["5m"]["sample_count"] == 1
    assert summary["windows"]["15m"]["sample_count"] == 2
    assert summary["windows"]["60m"]["sample_count"] == 3
    assert summary["windows"]["15m"]["roles"]["subtitle-worker"]["cpu_percent"]["avg"] == 15.0
    assert summary["windows"]["5m"]["roles"]["agent"]["cpu_percent"]["max"] == 5.0
    assert summary["latest_sample_age_seconds"] == 100.0


def test_debug_bundle_contains_energy_summary(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    recorder = DiagnosticRecorder(database)
    energy = tmp_path / "energy.jsonl"
    energy.write_text(
        json.dumps(
            {
                "timestamp": 100.0,
                "app_cpu_percent": 3.0,
                "context_cpu_percent": 0.0,
                "related_cpu_percent": 3.0,
                "processes": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    target = tmp_path / "debug.zip"

    DebugBundleBuilder(database, recorder).build(
        target,
        version="0.7.24",
        logs={"energy": energy},
    )

    with zipfile.ZipFile(target) as archive:
        assert "energy-summary.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        summary = json.loads(archive.read("energy-summary.json"))
    assert manifest["energy"]["path"] == str(energy)
    assert summary["sample_count"] == 1


def test_refresh_passes_first_library_scan_to_interactive_refresh(tmp_path: Path, monkeypatch) -> None:
    # Use a minimal WebAppApi shell so this test targets the refresh orchestration,
    # not AniList/network setup.
    api = object.__new__(WebAppApi)
    api._local_refresh_lock = __import__("threading").Lock()
    api.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
    )
    received: list[int | None] = []
    scans: list[str] = []
    api.scan_watched_media_folders = lambda: {}
    api.manager = SimpleNamespace(
        scan_library=lambda **kwargs: scans.append(dict(kwargs)) or [1, 2, 3],
        run_interactive_refresh=lambda *, pre_scanned_library_count=None: received.append(pre_scanned_library_count) or {"library": 999},
        reconcile_prepared_subtitle_rows=lambda: 0,
        sync_downloads=lambda: 0,
        log=lambda *_args: None,
    )
    api.light_novels = SimpleNamespace(
        scan_downloaded=lambda: 0,
        auto_download_missing=lambda: [],
    )
    api._downloads_enabled = lambda: False
    api.get_state = lambda: {}

    result = api.refresh_local()

    assert scans == [{"reuse_unchanged": True, "user_requested": True}]
    assert received == [3]
    assert result["stats"]["library"] == 3


def test_debug_bundle_includes_rotated_runtime_logs(tmp_path):
    database = Database(tmp_path / "pudge.db")
    recorder = DiagnosticRecorder(database)
    runtime = tmp_path / "runtime.log"
    runtime.write_text("current\n", encoding="utf-8")
    Path(f"{runtime}.1").write_text("previous\n", encoding="utf-8")
    Path(f"{runtime}.2").write_text("older\n", encoding="utf-8")
    target = tmp_path / "debug.zip"

    DebugBundleBuilder(database, recorder).build(
        target,
        version="test",
        logs={"runtime": runtime},
    )

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "logs/runtime.log" in names
        assert "logs/runtime.log.1" in names
        assert "logs/runtime.log.2" in names
