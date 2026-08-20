from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .cache_registry import CachePolicy, CacheRegistry
from .subtitle_runtime import repair_episode_subtitle, resolve_episode_subtitle
from .task_supervisor import TaskSupervisor
from .work_scheduler import WorkPriority, WorkScheduler


_STREAM_TTL_SECONDS = 30 * 60
_CACHE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
_CACHE_MAX_BYTES = 6 * 1024 * 1024 * 1024
_ALLOWED_MEDIA_FILES = {"index.m3u8", "subtitles.vtt"}


@dataclass(slots=True)
class _Job:
    cache_key: str
    entity_id: str
    video_path: Path
    output_dir: Path
    process: subprocess.Popen[bytes] | None = None
    state: str = "preparing"
    encoder: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class _Ticket:
    ticket: str
    cache_key: str
    entity_id: str
    output_dir: Path
    expires_at: float
    device_id: str = ""


class CompanionStreamingService:
    """On-demand iPhone-compatible HLS transcoding for local anime episodes.

    The browser receives only short-lived capability URLs. The original video
    path never leaves the Mac. Streams are cached briefly so reopening an
    episode does not immediately re-transcode it.
    """

    def __init__(
        self,
        database: Database,
        *,
        cache_dir: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        logger: Any = None,
        work_scheduler: WorkScheduler | None = None,
        task_supervisor: TaskSupervisor | None = None,
        cache_registry: CacheRegistry | None = None,
    ) -> None:
        self.database = database
        self.cache_root = Path(cache_dir).expanduser() / "companion-hls"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.ffprobe = str(ffprobe or "ffprobe")
        self.logger = logger
        self.work_scheduler = work_scheduler
        self.task_supervisor = task_supervisor
        self.cache_registry = cache_registry
        self._lock = threading.RLock()
        self._subtitle_lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._closed = False

    def _log(self, level: str, message: str, *args: object) -> None:
        logger = self.logger
        if logger is None:
            return
        callback = getattr(logger, level, None)
        if callable(callback):
            try:
                callback(message, *args)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass

    def _resolve_episode(self, entity_id: str) -> dict[str, Any]:
        with self.database.connect() as conn:
            entity = conn.execute(
                "SELECT kind,local_key,title FROM sync_entities WHERE entity_id=?",
                (str(entity_id),),
            ).fetchone()
            if entity is None or str(entity["kind"]) != "anime_episode":
                raise ValueError("Entity is not a local anime episode")
            local_key = str(entity["local_key"] or "")
            try:
                media_id_text, episode_text = local_key.split(":", 1)
                media_id = int(media_id_text)
                episode = int(episode_text)
            except (ValueError, TypeError) as exc:
                raise ValueError("Anime entity has invalid local identity") from exc
            rows = conn.execute(
                """
                SELECT e.video_path,e.subtitle_path,e.embedded_subtitle_id,e.subtitle_origin,
                       e.playback_position,e.playback_duration,e.playback_updated_at,e.updated_at,
                       e.title AS episode_title,a.title AS anime_title
                FROM episodes e
                LEFT JOIN anime a ON a.media_id=e.media_id
                WHERE e.media_id=? AND COALESCE(e.media_episode,e.episode)=?
                ORDER BY CASE e.state WHEN 'watched' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END,
                         COALESCE(e.playback_updated_at,e.updated_at) DESC,e.id DESC
                """,
                (media_id, episode),
            ).fetchall()
        if not rows:
            raise ValueError("Episode is not available on this Mac")
        selected = None
        for row in rows:
            path = Path(str(row["video_path"] or "")).expanduser()
            if path.is_file():
                selected = row
                break
        if selected is None:
            raise ValueError("Episode video file is missing")
        video_path = Path(str(selected["video_path"])).expanduser().resolve()
        return {
            "entity_id": str(entity_id),
            "media_id": media_id,
            "episode": episode,
            "video_path": video_path,
            "subtitle_path": str(selected["subtitle_path"] or ""),
            "embedded_subtitle_id": selected["embedded_subtitle_id"],
            "subtitle_origin": str(selected["subtitle_origin"] or ""),
            "position_ms": int(round(max(0.0, float(selected["playback_position"] or 0.0)) * 1000.0)),
            "duration_ms": int(round(max(0.0, float(selected["playback_duration"] or 0.0)) * 1000.0)),
            "anime_title": str(selected["anime_title"] or ""),
            "episode_title": str(selected["episode_title"] or ""),
        }

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        raw = f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8", errors="surrogatepass")
        return hashlib.sha1(raw).hexdigest()

    def _cache_dir(self, path: Path) -> tuple[str, Path]:
        key = self._fingerprint(path)
        return key, self.cache_root / key

    @staticmethod
    def _playlist_ready(output_dir: Path) -> bool:
        playlist = output_dir / "index.m3u8"
        if not playlist.is_file() or playlist.stat().st_size < 24:
            return False
        try:
            text = playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "#EXTM3U" in text and any(output_dir.glob("segment-*.ts"))

    @staticmethod
    def _playlist_complete(output_dir: Path) -> bool:
        playlist = output_dir / "index.m3u8"
        if not playlist.is_file():
            return False
        try:
            return "#EXT-X-ENDLIST" in playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def _issue_ticket(
        self,
        *,
        cache_key: str,
        entity_id: str,
        output_dir: Path,
        device_id: str = "",
    ) -> _Ticket:
        now = time.time()
        with self._lock:
            for ticket in self._tickets.values():
                if (
                    ticket.cache_key == cache_key
                    and ticket.entity_id == entity_id
                    and ticket.device_id == str(device_id)
                    and ticket.expires_at > now + 60
                ):
                    ticket.expires_at = now + _STREAM_TTL_SECONDS
                    return ticket
            token = secrets.token_urlsafe(24)
            ticket = _Ticket(
                token,
                cache_key,
                entity_id,
                output_dir,
                now + _STREAM_TTL_SECONDS,
                str(device_id),
            )
            self._tickets[token] = ticket
            return ticket

    def _cleanup_tickets(self) -> None:
        now = time.time()
        with self._lock:
            expired = [token for token, item in self._tickets.items() if item.expires_at <= now]
            for token in expired:
                self._tickets.pop(token, None)

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    total += item.stat().st_size
        except OSError:
            pass
        return total

    def cleanup_cache(self, *, keep: set[str] | None = None) -> dict[str, int]:
        keep = set(keep or ())
        now = time.time()
        entries: list[tuple[float, int, Path]] = []
        removed = 0
        removed_bytes = 0
        for path in self.cache_root.iterdir() if self.cache_root.is_dir() else ():
            if not path.is_dir() or path.name in keep:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            size = self._directory_size(path)
            if now - mtime > _CACHE_MAX_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
                removed_bytes += size
                continue
            entries.append((mtime, size, path))
        total = sum(size for _mtime, size, _path in entries)
        for _mtime, size, path in sorted(entries):
            if total <= _CACHE_MAX_BYTES:
                break
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            removed += 1
            removed_bytes += size
        return {"removed": removed, "removed_bytes": removed_bytes, "remaining_bytes": max(0, total)}

    def _ffmpeg_path(self) -> str:
        candidate = Path(self.ffmpeg).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(self.ffmpeg)
        if resolved:
            return resolved
        raise ValueError("ffmpeg is required for anime streaming")

    def _ffmpeg_optional_path(self) -> str:
        """Return the best ffmpeg command hint without requiring it to exist.

        Subtitle recovery can finish directly from an existing SRT/VTT and must
        not require ffmpeg merely to discover that direct path.
        """
        candidate = Path(self.ffmpeg).expanduser()
        if candidate.is_file():
            return str(candidate)
        return shutil.which(self.ffmpeg) or str(candidate)

    def _ffprobe_path(self) -> str:
        candidate = Path(self.ffprobe).expanduser()
        if candidate.is_file():
            return str(candidate)
        return shutil.which(self.ffprobe) or self.ffprobe

    def _stream_copy_compatible(self, source: Path) -> bool:
        try:
            completed = subprocess.run(
                [
                    self._ffprobe_path(),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name",
                    "-of",
                    "json",
                    str(source),
                ],
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            payload = json.loads(completed.stdout or "{}") if completed.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
            return False
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list):
            return False
        video = next((row for row in streams if row.get("codec_type") == "video"), None)
        audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
        return bool(
            isinstance(video, dict)
            and str(video.get("codec_name") or "").casefold() in {"h264", "avc1"}
            and (
                audio is None
                or str(audio.get("codec_name") or "").casefold() in {"aac", "mp3"}
            )
        )

    @staticmethod
    def _subtitle_marker(output_dir: Path) -> Path:
        return output_dir / "subtitles.source"

    @staticmethod
    def _subtitle_signature(selection: Any, video_path: Path) -> str:
        external = getattr(selection, "external_path", None)
        if external is not None:
            path = Path(external)
            try:
                stat = path.stat()
                return f"file:{path}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                return f"file:{path}:missing"
        sid = getattr(selection, "embedded_subtitle_id", None)
        stream = getattr(selection, "embedded_stream_index", None)
        if sid is not None:
            try:
                stat = video_path.stat()
                return f"embedded:{video_path}:{stat.st_size}:{stat.st_mtime_ns}:sid={sid}:stream={stream}"
            except OSError:
                return f"embedded:{video_path}:sid={sid}:stream={stream}"
        return "none"

    @staticmethod
    def _write_srt_as_vtt(source: Path, target: Path) -> None:
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(
            r"(?m)^(\s*\d{1,2}:\d{2}:\d{2}),(\d{3})(\s+-->\s+\d{1,2}:\d{2}:\d{2}),(\d{3})(.*)$",
            r"\1.\2\3.\4\5",
            text,
        )
        target.write_text("WEBVTT\n\n" + text.lstrip("\ufeff\n"), encoding="utf-8")

    def _prepare_subtitles(self, episode: dict[str, Any], output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "subtitles.vtt"
        marker = self._subtitle_marker(output_dir)
        video_path = Path(episode["video_path"]).expanduser().resolve()
        with self._subtitle_lock:
            selection = resolve_episode_subtitle(
                self.database,
                video_path=video_path,
                media_id=int(episode.get("media_id") or 0) or None,
                episode=int(episode.get("episode") or 0) or None,
                stored_path=episode.get("subtitle_path"),
                stored_embedded_id=episode.get("embedded_subtitle_id"),
                stored_origin=str(episode.get("subtitle_origin") or ""),
                ffprobe=self._ffprobe_path(),
                ffmpeg=self._ffmpeg_optional_path(),
                allow_bitmap=False,
            )
            if selection.found:
                try:
                    repaired = repair_episode_subtitle(
                        self.database,
                        video_path=video_path,
                        selection=selection,
                    )
                except Exception:
                    repaired = False
                if repaired:
                    self._log(
                        "info",
                        "RESULT step=companion.subtitle_recover entity=%s source=%s reason=%s",
                        episode.get("entity_id"),
                        selection.source,
                        selection.reason,
                    )

            if not selection.found or not selection.is_text:
                for stale in (target, marker):
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass
                return {
                    "ready": False,
                    "state": "missing",
                    "source": str(selection.source or ""),
                    "reason": str(selection.reason or "no_japanese_text_subtitle"),
                    "error": "",
                }

            signature = self._subtitle_signature(selection, video_path)
            if target.is_file() and marker.is_file():
                try:
                    if target.stat().st_size > 10 and marker.read_text(encoding="utf-8") == signature:
                        return {
                            "ready": True,
                            "state": "ready",
                            "source": str(selection.source or ""),
                            "reason": str(selection.reason or "cached"),
                            "error": "",
                        }
                except OSError:
                    pass

            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

            error = ""
            external = selection.external_path
            if external is not None:
                suffix = external.suffix.casefold()
                try:
                    if suffix == ".vtt":
                        shutil.copy2(external, target)
                    elif suffix == ".srt":
                        self._write_srt_as_vtt(external, target)
                    else:
                        command = [
                            self._ffmpeg_optional_path(), "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(external), "-c:s", "webvtt", str(target),
                        ]
                        completed = subprocess.run(
                            command,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=90,
                            check=False,
                        )
                        if completed.returncode != 0:
                            error = completed.stderr.decode("utf-8", errors="replace")[-1200:].strip()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    error = str(exc)
            elif selection.embedded_stream_index is not None:
                command = [
                    self._ffmpeg_optional_path(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(video_path), "-map", f"0:{int(selection.embedded_stream_index)}",
                    "-c:s", "webvtt", str(target),
                ]
                try:
                    completed = subprocess.run(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=90,
                        check=False,
                    )
                    if completed.returncode != 0:
                        error = completed.stderr.decode("utf-8", errors="replace")[-1200:].strip()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    error = str(exc)
            else:
                error = "Embedded subtitle stream index could not be resolved"

            ready = False
            try:
                ready = target.is_file() and target.stat().st_size > 10
            except OSError:
                ready = False
            if ready:
                try:
                    marker.write_text(signature, encoding="utf-8")
                except OSError:
                    pass
                self._log(
                    "info",
                    "RESULT step=companion.subtitle entity=%s source=%s reason=%s",
                    episode.get("entity_id"),
                    selection.source,
                    selection.reason,
                )
                return {
                    "ready": True,
                    "state": "ready",
                    "source": str(selection.source or ""),
                    "reason": str(selection.reason or ""),
                    "error": "",
                }

            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            self._log(
                "warning",
                "FAIL step=companion.subtitle entity=%s source=%s reason=%s error=%r",
                episode.get("entity_id"),
                selection.source,
                selection.reason,
                error or "conversion failed",
            )
            return {
                "ready": False,
                "state": "failed",
                "source": str(selection.source or ""),
                "reason": str(selection.reason or ""),
                "error": error or "Subtitle conversion failed",
            }

    def _command(self, source: Path, output_dir: Path, encoder: str) -> list[str]:
        ffmpeg = self._ffmpeg_path()
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-sn",
            "-dn",
        ]
        if encoder == "copy":
            command += ["-c:v", "copy", "-c:a", "copy"]
        elif encoder == "h264_videotoolbox":
            command += ["-c:v", encoder, "-pix_fmt", "yuv420p"]
            command += ["-allow_sw", "1", "-b:v", "3500k", "-maxrate", "4500k", "-bufsize", "7000k"]
        else:
            command += ["-c:v", encoder, "-pix_fmt", "yuv420p"]
            command += ["-preset", "veryfast", "-crf", "22", "-maxrate", "4500k", "-bufsize", "7000k"]
        if encoder != "copy":
            command += [
                "-c:a", "aac",
                "-b:a", "160k",
                "-ac", "2",
                "-force_key_frames", "expr:gte(t,n_forced*4)",
            ]
        command += [
            "-f", "hls",
            "-hls_time", "4",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_filename", str(output_dir / "segment-%05d.ts"),
            str(output_dir / "index.m3u8"),
        ]
        return command

    @staticmethod
    def _clear_hls_files(output_dir: Path) -> None:
        for pattern in ("index.m3u8", "segment-*.ts", "*.tmp"):
            for path in output_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _run_job(self, job: _Job, episode: dict[str, Any]) -> None:
        output_dir = job.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        encoders = ["h264_videotoolbox", "libx264"] if os.uname().sysname == "Darwin" else ["libx264"]
        if self._stream_copy_compatible(job.video_path):
            encoders.insert(0, "copy")
        error = ""
        lease = None
        try:
            if self.work_scheduler is not None:
                lease = self.work_scheduler.acquire_heavy(
                    f"companion-hls:{job.entity_id}",
                    blocking=True,
                    wait_for_foreground=True,
                    cancel_check=lambda: self._closed,
                    priority=WorkPriority.USER,
                    resource="gpu",
                )
                if lease is None:
                    return
            for encoder in encoders:
                if self._closed:
                    return
                self._clear_hls_files(output_dir)
                command = self._command(job.video_path, output_dir, encoder)
                self._log("info", "START step=companion.hls entity=%s encoder=%s", job.entity_id, encoder)
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                except OSError as exc:
                    error = str(exc)
                    continue
                with self._lock:
                    job.process = process
                    job.encoder = encoder
                    job.state = "preparing"
                stderr = process.communicate()[1] or b""
                if process.returncode == 0 and self._playlist_ready(output_dir):
                    with self._lock:
                        job.state = "ready"
                        job.finished_at = time.time()
                        job.error = ""
                        job.process = None
                    try:
                        os.utime(output_dir, None)
                    except OSError:
                        pass
                    self._log("info", "RESULT step=companion.hls entity=%s encoder=%s", job.entity_id, encoder)
                    if self.cache_registry is not None:
                        self.cache_registry.register(
                            "hls",
                            output_dir,
                            expires_at=time.time() + _CACHE_MAX_AGE_SECONDS,
                            metadata={"entity_id": job.entity_id, "encoder": encoder},
                        )
                        self.cache_registry.enforce(
                            {"hls": CachePolicy(_CACHE_MAX_BYTES, _CACHE_MAX_AGE_SECONDS)}
                        )
                    return
                error = stderr.decode("utf-8", errors="replace")[-1800:].strip() or f"ffmpeg exited {process.returncode}"
                self._log("warning", "FALLBACK step=companion.hls entity=%s encoder=%s error=%r", job.entity_id, encoder, error)
            with self._lock:
                job.state = "failed"
                job.finished_at = time.time()
                job.error = error or "Unable to prepare HLS stream"
                job.process = None
        finally:
            if lease is not None:
                lease.release()
            with self._lock:
                if self._closed and job.process is not None:
                    try:
                        job.process.terminate()
                    except OSError:
                        pass

    def _ensure_job(self, episode: dict[str, Any], cache_key: str, output_dir: Path) -> _Job | None:
        if self._playlist_complete(output_dir):
            return None
        with self._lock:
            existing = self._jobs.get(cache_key)
            if existing is not None and existing.state in {"preparing", "ready"}:
                return existing
            output_dir.mkdir(parents=True, exist_ok=True)
            job = _Job(
                cache_key=cache_key,
                entity_id=str(episode["entity_id"]),
                video_path=Path(episode["video_path"]),
                output_dir=output_dir,
                state="preparing",
                started_at=time.time(),
            )
            self._jobs[cache_key] = job
            if self.task_supervisor is not None:
                self.task_supervisor.start(
                    f"pudge-hls-{cache_key[:8]}",
                    self._run_job,
                    args=(job, dict(episode)),
                )
            else:
                thread = threading.Thread(
                    target=self._run_job,
                    args=(job, dict(episode)),
                    name=f"pudge-hls-{cache_key[:8]}",
                    daemon=True,
                )
                thread.start()
            return job

    def prepare(self, entity_id: str, *, device_id: str = "") -> dict[str, Any]:
        if self._closed:
            raise ValueError("Companion streaming is shutting down")
        episode = self._resolve_episode(entity_id)
        cache_key, output_dir = self._cache_dir(Path(episode["video_path"]))
        self._cleanup_tickets()
        self.cleanup_cache(keep={cache_key})
        subtitle_result = self._prepare_subtitles(episode, output_dir)
        job = self._ensure_job(episode, cache_key, output_dir)
        ticket = self._issue_ticket(
            cache_key=cache_key,
            entity_id=str(entity_id),
            output_dir=output_dir,
            device_id=str(device_id),
        )

        playlist_ready = self._playlist_ready(output_dir)
        with self._lock:
            if job is None:
                state = "ready"
                encoder = "cache"
                error = ""
            else:
                state = "ready" if playlist_ready else job.state
                encoder = job.encoder
                error = job.error
        segment_count = len(list(output_dir.glob("segment-*.ts"))) if output_dir.is_dir() else 0
        subtitle_ready = bool(subtitle_result.get("ready")) and (output_dir / "subtitles.vtt").is_file()
        base = f"/api/v1/media/{ticket.ticket}"
        return {
            "supported": True,
            "state": state,
            "encoder": encoder,
            "error": error,
            "segment_count": segment_count,
            "ticket_expires_at": ticket.expires_at,
            "playlist_url": f"{base}/index.m3u8" if playlist_ready else "",
            "subtitles_url": f"{base}/subtitles.vtt" if subtitle_ready else "",
            "subtitle_state": str(subtitle_result.get("state") or "missing"),
            "subtitle_source": str(subtitle_result.get("source") or ""),
            "subtitle_reason": str(subtitle_result.get("reason") or ""),
            "subtitle_error": str(subtitle_result.get("error") or ""),
            "position_ms": episode["position_ms"],
            "duration_ms": episode["duration_ms"],
            "episode": episode["episode"],
            "anime_title": episode["anime_title"],
            "episode_title": episode["episode_title"],
        }

    def media_path(self, ticket_value: str, filename: str) -> tuple[Path, str]:
        self._cleanup_tickets()
        token = str(ticket_value or "")
        with self._lock:
            ticket = self._tickets.get(token)
        if ticket is None or ticket.expires_at <= time.time():
            raise ValueError("Stream ticket expired")
        ticket.expires_at = time.time() + _STREAM_TTL_SECONDS
        name = str(filename or "")
        if name not in _ALLOWED_MEDIA_FILES and not (
            name.startswith("segment-") and name.endswith(".ts") and name[8:-3].isdigit()
        ):
            raise ValueError("Invalid stream media path")
        path = (ticket.output_dir / name).resolve()
        root = ticket.output_dir.resolve()
        if root not in path.parents or not path.is_file():
            raise FileNotFoundError(name)
        try:
            os.utime(ticket.output_dir, None)
        except OSError:
            pass
        if name.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
        elif name.endswith(".ts"):
            content_type = "video/mp2t"
        elif name.endswith(".vtt"):
            content_type = "text/vtt; charset=utf-8"
        else:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return path, content_type

    def revoke_device(self, device_id: str) -> int:
        with self._lock:
            tokens = [
                token for token, ticket in self._tickets.items()
                if ticket.device_id == str(device_id)
            ]
            for token in tokens:
                self._tickets.pop(token, None)
        return len(tokens)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            jobs = list(self._jobs.values())
            self._tickets.clear()
        for job in jobs:
            process = job.process
            if process is None or process.poll() is not None:
                continue
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
