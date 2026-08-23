from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .branding import APP_NAME, APP_SLUG, DEFAULT_ENERGY_LOG_PATH

ENERGY_LOG_PATH = DEFAULT_ENERGY_LOG_PATH


class EnergyDiagnosticsMonitor:
    """Low-overhead process activity logger for energy debugging.

    Activity Monitor's exact Energy Impact metric is not exposed to ordinary
    applications. Continuously invoking privileged ``powermetrics`` would itself
    distort the measurement, so this monitor records the useful low-overhead
    inputs instead: CPU%, memory/RSS and all pudge-related processes.
    """

    def __init__(self, *, interval_seconds: float = 30.0, logger: Any | None = None) -> None:
        self.interval_seconds = max(10.0, float(interval_seconds))
        self.logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"{APP_SLUG}-energy-diagnostics",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None

    def update_interval(self, seconds: float) -> None:
        self.interval_seconds = max(10.0, float(seconds))

    @staticmethod
    def _process_rows() -> list[dict[str, Any]]:
        # The selected fields exist on macOS and on the common BSD/GNU ps forms.
        # ``command`` is intentionally last so whitespace in argv is preserved.
        command = [
            "ps", "axo",
            "pid=,ppid=,%cpu=,%mem=,rss=,etime=,command=",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0:
            return []

        rows: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(None, 6)
            if len(parts) < 7:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
                cpu = float(parts[2].replace(",", "."))
                mem = float(parts[3].replace(",", "."))
                rss_kb = int(parts[4])
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "cpu_percent": cpu,
                    "memory_percent": mem,
                    "rss_mb": round(rss_kb / 1024.0, 2),
                    "elapsed": parts[5],
                    "command": parts[6],
                }
            )
        return rows

    @staticmethod
    def _elapsed_seconds(value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            try:
                days = int(day_text)
            except ValueError:
                return None
        try:
            parts = [int(part) for part in text.split(":")]
        except ValueError:
            return None
        if len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]
        elif len(parts) == 3:
            hours, minutes, seconds = parts
        else:
            return None
        return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)

    @staticmethod
    def _process_role(command: str, *, own: bool = False) -> str:
        value = str(command or "").casefold()
        executable = value.split(None, 1)[0] if value else ""
        if own:
            return "app"
        if "com.apple.webkit.webcontent" in value:
            return "webkit-webcontent"
        if "com.apple.webkit.gpu" in value:
            return "webkit-gpu"
        if "com.apple.webkit.networking" in value:
            return "webkit-network"
        if "pudge.agent" in value and "--scheduled" in value:
            return "agent"
        if "pudge.cli" in value and "--prepare-only" in value:
            return "subtitle-worker"
        if executable.endswith("/mpv") or executable == "mpv":
            return "player"
        if "qbittorrent" in value:
            return "qbittorrent"
        if "aria2c" in value:
            return "aria2"
        if executable.endswith("/7zz") or executable.endswith("/7z") or executable in {"7zz", "7z"}:
            return "archive-worker"
        if "ffmpeg" in value or "ffprobe" in value:
            return "media-worker"
        if "python" in value:
            return "python-worker"
        return "child"

    @classmethod
    def _related_processes(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        own_pid = os.getpid()
        # ``ps`` includes itself in its snapshot. Counting that short-lived
        # child made the diagnostics monitor appear as application activity on
        # every sample and kept a useless process row in long logs.
        rows = [
            row
            for row in rows
            if not (
                str(row.get("command") or "").lstrip().startswith("ps axo ")
                and "pid=,ppid=,%cpu=,%mem=,rss=,etime=,command="
                in str(row.get("command") or "")
            )
        ]
        by_parent: dict[int, list[int]] = {}
        by_pid = {int(row["pid"]): dict(row) for row in rows}
        for row in rows:
            by_parent.setdefault(int(row["ppid"]), []).append(int(row["pid"]))

        app_ids = {own_pid}
        stack = [own_pid]
        while stack:
            parent = stack.pop()
            for child in by_parent.get(parent, []):
                if child not in app_ids:
                    app_ids.add(child)
                    stack.append(child)

        # WKWebView helpers are commonly re-parented to launchd (PPID 1). Do not
        # blindly include every WebKit helper on the machine: associate only
        # helpers that were launched at roughly the same time as this app.
        own_elapsed = cls._elapsed_seconds(str(by_pid.get(own_pid, {}).get("elapsed") or ""))
        if own_elapsed is not None:
            for row in rows:
                command = str(row.get("command") or "").casefold()
                if "com.apple.webkit." not in command:
                    continue
                elapsed = cls._elapsed_seconds(str(row.get("elapsed") or ""))
                if elapsed is not None and abs(elapsed - own_elapsed) <= 180.0:
                    app_ids.add(int(row["pid"]))

        context_ids: set[int] = set()
        for row in rows:
            pid = int(row["pid"])
            if pid in app_ids:
                continue
            command = str(row.get("command") or "").casefold()
            executable = command.split(None, 1)[0] if command else ""
            is_mpv = executable.endswith("/mpv") or executable == "mpv"
            is_agent = "pudge.agent" in command and "--scheduled" in command
            is_subtitle_worker = "pudge.cli" in command and "--prepare-only" in command
            if (
                is_mpv
                or is_agent
                or is_subtitle_worker
                or "jpdb-mpv" in command
                or "qbittorrent" in command
                or "aria2c" in command
            ):
                context_ids.add(pid)

        # LaunchAgent and its subtitle/ffmpeg children are not descendants of
        # the GUI process. Once a Pudge context root is recognized, include its
        # own process tree so worker cost is not lost from the energy account.
        stack = list(context_ids)
        while stack:
            parent = stack.pop()
            for child in by_parent.get(parent, []):
                if child not in app_ids and child not in context_ids:
                    context_ids.add(child)
                    stack.append(child)

        result: list[dict[str, Any]] = []
        for pid in app_ids | context_ids:
            if pid not in by_pid:
                continue
            row = dict(by_pid[pid])
            row["scope"] = "app" if pid in app_ids else "context"
            row["role"] = cls._process_role(str(row.get("command") or ""), own=pid == own_pid)
            result.append(row)
        result.sort(key=lambda row: (0 if row["scope"] == "app" else 1, -float(row["cpu_percent"]), int(row["pid"])))
        return result

    @staticmethod
    def _rotate(path: Path) -> None:
        try:
            if not path.is_file() or path.stat().st_size < 5 * 1024 * 1024:
                return
            previous = path.with_suffix(path.suffix + ".1")
            previous.unlink(missing_ok=True)
            path.replace(previous)
        except OSError:
            pass

    def sample(self) -> dict[str, Any]:
        rows = self._related_processes(self._process_rows())
        app_cpu = sum(max(0.0, float(row["cpu_percent"])) for row in rows if row.get("scope") == "app")
        context_cpu = sum(max(0.0, float(row["cpu_percent"])) for row in rows if row.get("scope") == "context")
        app_rss = sum(max(0.0, float(row.get("rss_mb") or 0.0)) for row in rows if row.get("scope") == "app")
        context_rss = sum(max(0.0, float(row.get("rss_mb") or 0.0)) for row in rows if row.get("scope") == "context")
        total_cpu = app_cpu + context_cpu
        role_totals: dict[str, dict[str, float | int]] = {}
        for row in rows:
            scope_total = app_cpu if row.get("scope") == "app" else context_cpu
            row["activity_share_percent"] = (
                round(max(0.0, float(row["cpu_percent"])) / scope_total * 100.0, 1)
                if scope_total > 0
                else 0.0
            )
            role = str(row.get("role") or "unknown")
            total = role_totals.setdefault(role, {"cpu_percent": 0.0, "rss_mb": 0.0, "process_count": 0})
            total["cpu_percent"] = float(total["cpu_percent"]) + max(0.0, float(row.get("cpu_percent") or 0.0))
            total["rss_mb"] = float(total["rss_mb"]) + max(0.0, float(row.get("rss_mb") or 0.0))
            total["process_count"] = int(total["process_count"]) + 1
        for total in role_totals.values():
            total["cpu_percent"] = round(float(total["cpu_percent"]), 2)
            total["rss_mb"] = round(float(total["rss_mb"]), 2)
        return {
            "timestamp": time.time(),
            "platform": sys.platform,
            "app_name": APP_NAME,
            "sample_interval_seconds": self.interval_seconds,
            "app_cpu_percent": round(app_cpu, 2),
            "context_cpu_percent": round(context_cpu, 2),
            "app_rss_mb": round(app_rss, 2),
            "context_rss_mb": round(context_rss, 2),
            "related_rss_mb": round(app_rss + context_rss, 2),
            # Retained for compatibility with v0.6.55 log readers.
            "related_cpu_percent": round(total_cpu, 2),
            "energy_metric": "cpu_activity_proxy",
            "role_totals": role_totals,
            "processes": rows,
        }

    def _append(self, payload: dict[str, Any]) -> None:
        path = ENERGY_LOG_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning("Energy diagnostics write failed: %s", exc)

    def _run(self) -> None:
        if self.logger is not None:
            self.logger.info("EVENT energy_diagnostics.start interval_seconds=%s path=%s", self.interval_seconds, ENERGY_LOG_PATH)
        try:
            # Sample immediately so enabling the setting gives useful output
            # without waiting a full interval. A malformed/temporary process
            # snapshot must not kill the monitor for the rest of the app session.
            while not self._stop.is_set():
                try:
                    self._append(self.sample())
                except Exception as exc:
                    if self.logger is not None:
                        self.logger.exception(
                            "FAIL step=energy_diagnostics.sample error=%r",
                            str(exc),
                        )
                if self._stop.wait(self.interval_seconds):
                    break
        finally:
            if self.logger is not None:
                self.logger.info("EVENT energy_diagnostics.stop")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _metric_stats(values: Iterable[float]) -> dict[str, float]:
    rows = [max(0.0, float(value)) for value in values]
    if not rows:
        return {"avg": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "avg": round(sum(rows) / len(rows), 2),
        "median": round(float(statistics.median(rows)), 2),
        "p95": round(_percentile(rows, 0.95), 2),
        "max": round(max(rows), 2),
    }


def _role_totals_for_sample(sample: dict[str, Any]) -> dict[str, dict[str, float]]:
    raw = sample.get("role_totals")
    if isinstance(raw, dict):
        result: dict[str, dict[str, float]] = {}
        for role, values in raw.items():
            if not isinstance(values, dict):
                continue
            result[str(role)] = {
                "cpu_percent": max(0.0, float(values.get("cpu_percent") or 0.0)),
                "rss_mb": max(0.0, float(values.get("rss_mb") or 0.0)),
            }
        return result
    result: dict[str, dict[str, float]] = {}
    for row in sample.get("processes") or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "unknown")
        total = result.setdefault(role, {"cpu_percent": 0.0, "rss_mb": 0.0})
        total["cpu_percent"] += max(0.0, float(row.get("cpu_percent") or 0.0))
        total["rss_mb"] += max(0.0, float(row.get("rss_mb") or 0.0))
    return result


def summarize_energy_samples(
    samples: Iterable[dict[str, Any]],
    *,
    now: float | None = None,
    windows_seconds: tuple[int, ...] = (300, 900, 3600),
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        try:
            timestamp = float(sample.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            continue
        if timestamp <= 0:
            continue
        item = dict(sample)
        item["timestamp"] = timestamp
        rows.append(item)
    rows.sort(key=lambda item: float(item["timestamp"]))
    anchor = float(time.time() if now is None else now)
    latest = float(rows[-1]["timestamp"]) if rows else 0.0
    summary: dict[str, Any] = {
        "schema": 1,
        "generated_at": anchor,
        "latest_sample_at": latest or None,
        "latest_sample_age_seconds": round(max(0.0, anchor - latest), 2) if latest else None,
        "sample_count": len(rows),
        "windows": {},
    }
    for seconds in windows_seconds:
        seconds = max(1, int(seconds))
        selected = [row for row in rows if anchor - float(row["timestamp"]) <= seconds]
        label = f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"
        roles = sorted({role for row in selected for role in _role_totals_for_sample(row)})
        role_summary: dict[str, Any] = {}
        for role in roles:
            cpu_values: list[float] = []
            rss_values: list[float] = []
            for row in selected:
                values = _role_totals_for_sample(row).get(role, {})
                cpu_values.append(float(values.get("cpu_percent") or 0.0))
                rss_values.append(float(values.get("rss_mb") or 0.0))
            role_summary[role] = {
                "cpu_percent": _metric_stats(cpu_values),
                "rss_mb": _metric_stats(rss_values),
            }
        summary["windows"][label] = {
            "window_seconds": seconds,
            "sample_count": len(selected),
            "app_cpu_percent": _metric_stats(float(row.get("app_cpu_percent") or 0.0) for row in selected),
            "context_cpu_percent": _metric_stats(float(row.get("context_cpu_percent") or 0.0) for row in selected),
            "related_cpu_percent": _metric_stats(float(row.get("related_cpu_percent") or 0.0) for row in selected),
            "app_rss_mb": _metric_stats(
                float(row.get("app_rss_mb") or sum(
                    float(proc.get("rss_mb") or 0.0)
                    for proc in row.get("processes") or []
                    if isinstance(proc, dict) and proc.get("scope") == "app"
                ))
                for row in selected
            ),
            "context_rss_mb": _metric_stats(
                float(row.get("context_rss_mb") or sum(
                    float(proc.get("rss_mb") or 0.0)
                    for proc in row.get("processes") or []
                    if isinstance(proc, dict) and proc.get("scope") == "context"
                ))
                for row in selected
            ),
            "roles": role_summary,
        }
    return summary


def summarize_energy_log(
    path: Path = ENERGY_LOG_PATH,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    source = Path(path).expanduser()
    samples: list[dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            samples.append(value)
    summary = summarize_energy_samples(samples, now=now)
    summary["path"] = str(source)
    summary["exists"] = source.is_file()
    return summary
