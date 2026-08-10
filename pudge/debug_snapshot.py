from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .logging_utils import DEFAULT_LOG_PATH, tail_log


DEBUG_SCHEMA = 1


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return dict(value)
    except Exception:
        return str(value)


def _redact(text: str) -> str:
    value = str(text or "")
    patterns = (
        (r"(?i)(authorization:\s*bearer\s+)[^\s'\"\\]+", r"\1<redacted>"),
        (r"(?i)(api[_ -]?key[=:]\s*)[^\s,;'\"\\]+", r"\1<redacted>"),
        (r"(?i)(access[_ -]?token[=:]\s*)[^\s,;'\"\\]+", r"\1<redacted>"),
        (r"(?i)(password[=:]\s*)[^\s,;'\"\\]+", r"\1<redacted>"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def subtitle_debug_paths(cache_dir: Path, video: Path) -> dict[str, Path]:
    root = Path(cache_dir).expanduser() / "subtitle-debug"
    digest = hashlib.sha1(str(Path(video).expanduser().resolve()).encode("utf-8")).hexdigest()
    return {
        "root": root,
        "trace": root / f"{digest}.jsonl",
        "result": root / f"{digest}.json",
    }


def append_debug_trace(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": time.time(), **_jsonable(payload)}
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def write_prepare_debug_result(
    path: Path,
    *,
    command: list[str],
    returncode: int,
    started_at: float,
    finished_at: float,
    stdout: str,
    stderr: str,
    prepare_status: str = "",
    subtitle_meta: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": DEBUG_SCHEMA,
        "command": [str(item) for item in command],
        "returncode": int(returncode),
        "started_at": float(started_at),
        "finished_at": float(finished_at),
        "duration_ms": round(max(0.0, finished_at - started_at) * 1000.0, 1),
        "prepare_status": str(prepare_status or ""),
        "subtitle_meta": _jsonable(subtitle_meta or {}),
        "stdout": _redact(str(stdout or "")[-120000:]),
        "stderr": _redact(str(stderr or "")[-40000:]),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def record_video_selection_debug(
    cache_dir: Path,
    *,
    media_id: int,
    episode: int | None,
    mode: str,
    releases: list[Any],
    selected: Any | None,
    threshold: float | None = None,
) -> None:
    root = Path(cache_dir).expanduser() / "video-selection-debug"
    root.mkdir(parents=True, exist_ok=True)
    key = f"{int(media_id)}-{episode if episode is not None else 'batch'}.json"
    payload = {
        "schema": DEBUG_SCHEMA,
        "updated_at": time.time(),
        "media_id": int(media_id),
        "episode": episode,
        "mode": str(mode),
        "threshold": threshold,
        "selected": _jsonable(selected),
        "candidates": [_jsonable(item) for item in releases[:50]],
    }
    target = root / key
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path, *, limit: int = 500) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def summarize_stage_trace(
    events: list[dict[str, Any]],
    *,
    finished_at: float | None = None,
) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    energy_by_stage: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("kind") or "") == "energy":
            stage = str(event.get("stage") or "")
            sample = event.get("sample")
            if stage and isinstance(sample, dict):
                energy_by_stage[stage] = sample
    seen: set[str] = set()
    for event in events:
        if str(event.get("kind") or "") not in {"worker_stage", "manager_stage"}:
            continue
        stage = str(event.get("stage") or "")
        if not stage or stage in seen:
            continue
        seen.add(stage)
        try:
            timestamp = float(event.get("updated_at") or event.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        stages.append(
            {
                "stage": stage,
                "started_at": timestamp,
                "details": _jsonable(event.get("details") or {}),
            }
        )
    stages.sort(key=lambda item: float(item["started_at"]))
    for index, stage in enumerate(stages):
        end = (
            float(stages[index + 1]["started_at"])
            if index + 1 < len(stages)
            else float(finished_at or stage["started_at"])
        )
        stage["duration_ms"] = round(max(0.0, end - float(stage["started_at"])) * 1000.0, 1)
        sample = energy_by_stage.get(str(stage["stage"])) or {}
        stage["cpu_activity_proxy_percent"] = sample.get("related_cpu_percent")
        processes = sample.get("processes") if isinstance(sample, dict) else None
        if isinstance(processes, list):
            stage["rss_mb"] = round(
                sum(float(row.get("rss_mb") or 0.0) for row in processes if isinstance(row, dict)),
                2,
            )
    return stages


def _timing_rows(lines: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"\b(?:DONE|FAIL|TIMING)\s+step=(?P<step>[^\s]+).*?\bduration_ms=(?P<ms>[0-9.]+)"
    )
    for line in lines:
        match = pattern.search(line)
        if match:
            rows.append(
                {
                    "step": match.group("step"),
                    "duration_ms": float(match.group("ms")),
                    "line": _redact(line),
                }
            )
    return rows[-120:]


class DebugSnapshotService:
    def __init__(self, manager: Any, *, cache_dir: Path, runtime_log_path: Path | None = None) -> None:
        self.manager = manager
        self.cache_dir = Path(cache_dir).expanduser()
        self.runtime_log_path = Path(runtime_log_path or DEFAULT_LOG_PATH).expanduser()

    def _selected_episode(self, media_id: int, episode: int | None) -> Any | None:
        rows = list(self.manager.db.episodes(int(media_id)))
        if not rows:
            return None
        if episode is not None:
            exact = next((item for item in rows if item.episode == int(episode)), None)
            if exact is not None:
                return exact
        anime = self.manager.db.get_anime(int(media_id))
        if anime is not None:
            exact = next((item for item in rows if item.episode == int(anime.next_episode)), None)
            if exact is not None:
                return exact
        rows.sort(
            key=lambda item: (
                float(item.playback_updated_at or 0.0),
                int(item.episode or 0),
            ),
            reverse=True,
        )
        return rows[0]

    def snapshot(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        media_id = int(media_id)
        anime = self.manager.db.get_anime(media_id)
        if anime is None:
            raise ValueError(f"AniList id={media_id} is not in the local database")
        selected = self._selected_episode(media_id, episode)
        selected_episode = int(selected.episode) if selected is not None and selected.episode is not None else episode
        try:
            diagnosis = self.manager.diagnose_episode(media_id, selected_episode)
        except Exception as exc:
            diagnosis = {"error": str(exc)}
        downloads = [
            _jsonable(item)
            for item in self.manager.db.downloads()
            if item.media_id == media_id
            and (selected_episode is None or item.episode in {None, selected_episode})
        ]
        jobs = []
        for row in self.manager.db.subtitle_jobs():
            if row["media_id"] is None or int(row["media_id"]) != media_id:
                continue
            try:
                jobs.append(_jsonable(dict(row)))
            except Exception:
                jobs.append({"video_path": str(row["video_path"])})
        video = Path(selected.video_path) if selected is not None else None
        subtitle_history: dict[str, Any] = {}
        timeline_attempts: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        prepare_result: dict[str, Any] = {}
        stages: list[dict[str, Any]] = []
        if video is not None:
            paths = subtitle_debug_paths(self.cache_dir, video)
            trace = _read_jsonl(paths["trace"])
            prepare_result = _read_json(paths["result"])
            stages = summarize_stage_trace(
                trace,
                finished_at=float(prepare_result.get("finished_at") or time.time()),
            )
            try:
                subtitle_history = self.manager.db.latest_selected_subtitle(video) or {}
            except Exception:
                subtitle_history = {}
            timeline_path = (
                self.cache_dir
                / "subtitle-timeline-debug"
                / f"{hashlib.sha1(str(video.expanduser().resolve()).encode('utf-8')).hexdigest()}.jsonl"
            )
            timeline_attempts = _read_jsonl(timeline_path)[-20:]
            started_at = float(prepare_result.get("started_at") or 0.0)
            if started_at:
                timeline_attempts = [
                    row for row in timeline_attempts
                    if float(row.get("timestamp") or 0.0) >= started_at - 2.0
                ]
        video_debug_path = (
            self.cache_dir
            / "video-selection-debug"
            / f"{media_id}-{selected_episode if selected_episode is not None else 'batch'}.json"
        )
        video_selection = _read_json(video_debug_path)
        runtime_lines = tail_log(self.runtime_log_path, limit=2500)
        needles = {str(media_id)}
        if video is not None:
            needles.add(video.name)
        for item in downloads:
            torrent_hash = str(item.get("torrent_hash") or "")
            if torrent_hash:
                needles.add(torrent_hash)
        relevant = [
            _redact(line)
            for line in runtime_lines
            if any(needle and needle in line for needle in needles)
        ][-500:]
        subtitle_lines = [
            line
            for line in relevant
            if any(
                marker in line.casefold()
                for marker in (
                    "subtitle", "jimaku", "alass", "ffsubsync", "embedded",
                    "reference", "ocr", "stt", "sync", "semantic",
                )
            )
        ][-300:]
        video_lines = [
            line
            for line in relevant
            if any(
                marker in line.casefold()
                for marker in (
                    "nyaa", "qbittorrent", "aria2", "download", "release",
                    "torrent", "seeders",
                )
            )
        ][-300:]
        return {
            "schema": DEBUG_SCHEMA,
            "generated_at": time.time(),
            "anime": _jsonable(anime),
            "requested_episode": episode,
            "selected_episode": selected_episode,
            "selected_local_episode": _jsonable(selected),
            "summary": {
                "diagnosis": diagnosis,
                "job_count": len(jobs),
                "download_count": len(downloads),
                "has_prepare_trace": bool(trace),
                "has_prepare_result": bool(prepare_result),
            },
            "video_selection": {
                "decision": video_selection,
                "downloads": downloads,
                "runtime_lines": video_lines,
            },
            "subtitle_selection": {
                "selected": {
                    "path": str(selected.subtitle_path) if selected is not None and selected.subtitle_path else "",
                    "embedded_sid": selected.embedded_subtitle_id if selected is not None else None,
                    "origin": selected.subtitle_origin if selected is not None else "",
                    "state": selected.state if selected is not None else "",
                },
                "latest_history": _jsonable(subtitle_history),
                "history_is_current": bool(
                    subtitle_history
                    and float(prepare_result.get("started_at") or 0.0) > 0.0
                    and float(subtitle_history.get("created_at") or 0.0)
                    >= float(prepare_result.get("started_at") or 0.0)
                ),
                "timeline_attempts": _jsonable(timeline_attempts),
                "current_timeline_attempt": _jsonable(timeline_attempts[-1] if timeline_attempts else {}),
                "jobs": jobs,
                "prepare_result": prepare_result,
                "runtime_lines": subtitle_lines,
            },
            "pipeline": {
                "stages": stages,
                "trace": trace,
            },
            "performance": {
                "metric_note": (
                    "Energy Impact is not exposed by macOS to ordinary apps. "
                    "CPU activity and RSS are stored as a low-overhead proxy."
                ),
                "prepare_duration_ms": prepare_result.get("duration_ms"),
                "stage_activity": stages,
                "runtime_timings": _timing_rows(relevant),
            },
            "raw_runtime_lines": relevant,
        }
