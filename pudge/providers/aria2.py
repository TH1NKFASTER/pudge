from __future__ import annotations

import base64
import copy
import json
import logging
import os
import secrets
import signal
import shutil
import socket
import stat as stat_module
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from ..branding import APP_SLUG, DATA_DIR
from ..fsutil import atomic_write_json, hide_from_finder
from ..manager_models import DownloadItem, NyaaRelease
from ..i18n import err
from .base import read_capped_body
from .qbittorrent import QBittorrentError
from .nyaa import BATCH_RE, release_episode_range
from .nyaa import release_episode as parsed_release_episode

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment is macOS
    fcntl = None  # type: ignore[assignment]


class Aria2Error(QBittorrentError):
    """aria2 failure exposed through the existing torrent error hierarchy."""


_TORRENT_LAYOUT_MAX_BYTES = 16 * 1024 * 1024


def _hollow(path: Path, length: int, info: os.stat_result | None = None) -> bool:
    """A file of ``length`` bytes with much less than that on disk (a sparse file being
    downloaded). False when the file system does not tell (no st_blocks)."""
    try:
        blocks = getattr(info if info is not None else path.stat(), "st_blocks", None)
    except OSError:
        return False
    if blocks is None or length <= 0:
        return False
    return int(blocks) * 512 < length - max(4 * 1024 * 1024, length // 50)


def torrent_layout(payload: bytes) -> tuple[str, list[tuple[str, int]], bool] | None:
    """(name, [(path inside the torrent, length)], multi-file) of a .torrent, or None.

    Only the bencode needed for the file table; paths that would leave the
    torrent's folder are refused.
    """
    data = bytes(payload or b"")
    if not data.startswith(b"d") or len(data) > _TORRENT_LAYOUT_MAX_BYTES:
        return None
    pos = 0

    def parse(depth: int = 0) -> Any:
        nonlocal pos
        if depth > 32 or pos >= len(data):
            raise ValueError("invalid bencode")
        token = data[pos : pos + 1]
        if token == b"i":
            end = data.index(b"e", pos)
            value = int(data[pos + 1 : end])
            pos = end + 1
            return value
        if token in {b"l", b"d"}:
            pos += 1
            values: list[Any] = []
            while data[pos : pos + 1] != b"e":
                if pos >= len(data):
                    raise ValueError("unterminated")
                values.append(parse(depth + 1))
            pos += 1
            if token == b"l":
                return values
            if len(values) % 2:
                raise ValueError("invalid dictionary")
            return dict(zip(values[::2], values[1::2]))
        if token.isdigit():
            colon = data.index(b":", pos)
            size = int(data[pos:colon])
            pos = colon + 1 + size
            if size < 0 or pos > len(data):
                raise ValueError("truncated")
            return data[colon + 1 : pos]
        raise ValueError("unsupported token")

    def text(value: object) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else ""

    try:
        root = parse()
    except (ValueError, TypeError, OverflowError, IndexError):
        return None
    info = root.get(b"info") if isinstance(root, dict) else None
    if not isinstance(info, dict):
        return None
    name = text(info.get(b"name.utf-8") or info.get(b"name")).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    raw_files = info.get(b"files")
    if not isinstance(raw_files, list):
        length = info.get(b"length")
        return (name, [(name, int(length))], False) if isinstance(length, int) and length >= 0 else None
    files: list[tuple[str, int]] = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            return None
        parts = [text(part) for part in (raw.get(b"path.utf-8") or raw.get(b"path") or [])]
        length = raw.get(b"length")
        if not parts or any(part in {"", ".", ".."} or "/" in part for part in parts):
            return None
        if not isinstance(length, int) or length < 0:
            return None
        files.append(("/".join(parts), length))
    return (name, files, True) if files else None


# (.torrent path, mtime_ns, size) -> its parsed layout. Every listing checks each
# finished task aria2 forgot (all of them after aria2 restarts) against its saved
# .torrent; a batch's .torrent is hundreds of KB of piece hashes, so reading and
# parsing them all again on every poll cost ~1 ms each.
_TORRENT_LAYOUT_CACHE: dict[tuple[str, int, int], tuple[str, list[tuple[str, int]], bool] | None] = {}
_TORRENT_LAYOUT_CACHE_MAX = 2048
_TORRENT_LAYOUT_CACHE_GUARD = threading.Lock()


def _saved_torrent_layout(torrent: Path) -> tuple[str, list[tuple[str, int]], bool] | None:
    """``torrent_layout`` of a saved .torrent file, parsed again only when the file changed."""
    try:
        info = torrent.stat()
    except OSError:
        return None
    if not stat_module.S_ISREG(info.st_mode) or info.st_size > _TORRENT_LAYOUT_MAX_BYTES:
        return None
    key = (str(torrent), int(info.st_mtime_ns), int(info.st_size))
    with _TORRENT_LAYOUT_CACHE_GUARD:
        if key in _TORRENT_LAYOUT_CACHE:
            return _TORRENT_LAYOUT_CACHE[key]
    try:
        layout = torrent_layout(torrent.read_bytes())
    except OSError:
        return None
    with _TORRENT_LAYOUT_CACHE_GUARD:
        if len(_TORRENT_LAYOUT_CACHE) >= _TORRENT_LAYOUT_CACHE_MAX:
            _TORRENT_LAYOUT_CACHE.clear()
        _TORRENT_LAYOUT_CACHE[key] = layout
    return layout


# (state dir, RPC port) -> aria2 session id proven to enforce Pudge's secret.
_OWN_SIDECAR_SESSIONS: dict[tuple[str, int], str] = {}
# RPC port -> (pid, start time, monotonic time last seen alive) of the listener
# proven to be Pudge's aria2c. Shared because the manager makes a client per
# call: lsof runs again only when that process is gone (or its pid was reused).
_TRUSTED_RPC_LISTENERS: dict[int, tuple[int, str, float]] = {}
# metadata.json path -> [lock, depth]. The manager makes a client per call, from many
# threads (and the agent is another process): every read-change-write of the ownership
# records holds this lock and a file lock, or a listing that saved its snapshot dropped
# the record of a release added meanwhile (that download was no longer Pudge's).
_METADATA_LOCKS: dict[str, list[Any]] = {}
_METADATA_LOCKS_GUARD = threading.Lock()


# A finished download whose files are gone is forgotten only this long after it finished.
_PRUNE_AFTER_SECONDS = 3600.0


class Aria2Client:
    """Small managed aria2c sidecar controlled through JSON-RPC.

    The app owns only downloads recorded in ``metadata.json``. This keeps an
    independently running aria2 instance out of pudge's library cleanup.
    """

    # v5: no file preallocation and at most 30 peers per torrent (less disk
    # writing and fewer sockets); bumping the profile restarts older sidecars.
    _RUNTIME_PROFILE = "v5-light-io"
    _IDLE_MARKER = "idle-stopped"
    _SAFE_RPC_TORRENT_BYTES = 1_250_000
    _VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts"}

    def __init__(
        self,
        *,
        enabled: bool = True,
        binary: str = "aria2c",
        rpc_port: int = 6801,
        state_dir: Path | None = None,
        pre_download_command: str = "",
        paused_on_add: bool = False,
        auto_start: bool = True,
        source_proxy_mode: str = "direct_then_proxy",
        source_proxy_url: str = "",
        seed_mode: str = "off",
        seed_ratio: float = 1.0,
        seed_time_minutes: float = 120.0,
        upload_limit_kib: int = 0,
        vpn_interface: str = "",
        vpn_kill_switch: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.binary = binary.strip() or "aria2c"
        self.rpc_port = max(1024, min(65535, int(rpc_port)))
        self.state_dir = (state_dir or (DATA_DIR / "aria2")).expanduser()
        self.pre_download_command = pre_download_command.strip()
        self.paused_on_add = bool(paused_on_add)
        self.auto_start = bool(auto_start)
        self.source_proxy_mode = str(source_proxy_mode or "direct_then_proxy").casefold()
        self.source_proxy_url = str(source_proxy_url or "").strip()
        normalized_seed_mode = str(seed_mode or "off").strip().casefold()
        self.seed_mode = (
            normalized_seed_mode
            if normalized_seed_mode in {"off", "ratio", "ratio_or_time", "unlimited"}
            else "off"
        )
        self.seed_ratio = max(0.0, float(seed_ratio))
        self.seed_time_minutes = max(0.0, float(seed_time_minutes))
        self.upload_limit_kib = max(0, int(upload_limit_kib))
        self.vpn_interface = str(vpn_interface or "").strip()
        self.vpn_kill_switch = bool(vpn_kill_switch)
        self.timeout = timeout
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._secret = self._load_secret()
        self._rpc_url = f"http://127.0.0.1:{self.rpc_port}/jsonrpc"
        self._http = httpx.Client(timeout=timeout)
        self._lock = threading.RLock()

    @property
    def backend_name(self) -> str:
        return "aria2"

    @property
    def base_url(self) -> str:
        return self._rpc_url

    def close(self) -> None:
        self._http.close()

    def _managed_sidecar_pids(self) -> list[int] | None:
        """Return Pudge-owned aria2 PIDs, or None when process inspection failed.

        Ownership is deliberately narrow: the argv must reference this exact
        state directory's session file.  A user's unrelated aria2 process is
        never selected just because it uses the same executable or RPC port.
        """
        session = str((self.state_dir / "session.txt").resolve())
        needles = (f"--input-file={session}", f"--save-session={session}")
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        result: list[int] = []
        for raw in completed.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            command = parts[1]
            if pid == os.getpid() or not any(needle in command for needle in needles):
                continue
            if "aria2" not in command.casefold():
                continue
            result.append(pid)
        return sorted(set(result))

    def managed_sidecar_running(self) -> bool | None:
        """Tri-state observation of a process owned by this state directory."""
        pids = self._managed_sidecar_pids()
        return None if pids is None else bool(pids)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _stop_unreachable_managed_sidecars(self) -> bool | None:
        """Stop only stale aria2 processes tied to this exact Pudge session."""
        pids = self._managed_sidecar_pids()
        if pids is None:
            return None
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if not any(self._pid_alive(pid) for pid in pids):
                return True
            time.sleep(0.05)
        for pid in pids:
            if not self._pid_alive(pid):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        time.sleep(0.05)
        return not any(self._pid_alive(pid) for pid in pids)

    def traffic_stats(self) -> dict[str, int]:
        """Return current managed-sidecar traffic without starting aria2c."""
        self._require_observable()
        result = self._rpc_raw("aria2.getGlobalStat") or {}
        return {
            "download_speed": max(0, int(result.get("downloadSpeed") or 0)),
            "upload_speed": max(0, int(result.get("uploadSpeed") or 0)),
            "active": max(0, int(result.get("numActive") or 0)),
            "waiting": max(0, int(result.get("numWaiting") or 0)),
        }

    def live_progress(self) -> dict[str, dict[str, Any]]:
        """Per-info-hash progress of active aria2 downloads, without starting aria2c.

        One cheap tellActive call; the UI polls it with the traffic snapshot so
        percentages move as often as the speed does.
        """
        self._require_observable()
        rows = (
            self._rpc_raw(
                "aria2.tellActive",
                [["gid", "infoHash", "totalLength", "completedLength", "downloadSpeed", "followedBy"]],
            )
            or []
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("followedBy"):
                continue
            info_hash = str(row.get("infoHash") or "").strip().lower()
            total = max(0, int(row.get("totalLength") or 0))
            if not info_hash or total <= 0:
                continue
            done = max(0, min(total, int(row.get("completedLength") or 0)))
            result[info_hash] = {
                "progress": done / total,
                "downloaded_bytes": done,
                "total_bytes": total,
                "download_speed": max(0, int(row.get("downloadSpeed") or 0)),
            }
        return result

    def shutdown(self, *, save_session: bool = True) -> bool:
        """Stop the Pudge-owned aria2c sidecar without ever auto-starting it."""
        with self._lock:
            if not self._probe():
                # An older Pudge may still have a detached sidecar with a stale
                # RPC secret/port.  RPC cannot shut that process down, but its
                # exact session argv still proves ownership.
                stopped = self._stop_unreachable_managed_sidecars()
                return bool(stopped)
            if not self._rpc_is_own_sidecar():
                # Never shut down an aria2 that Pudge did not start.
                return False
            if save_session:
                try:
                    self._rpc_raw("aria2.saveSession")
                except Aria2Error:
                    pass
            try:
                self._rpc_raw("aria2.forceShutdown")
            except Aria2Error:
                try:
                    self._rpc_raw("aria2.shutdown")
                except Aria2Error:
                    return False
            _TRUSTED_RPC_LISTENERS.pop(self.rpc_port, None)  # whoever takes the port next is checked again
            # Saving a large session under load can take seconds: waiting only
            # ~1.5 s reported "not stopped" for an aria2 that exited right after.
            for delay in (0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0):
                time.sleep(delay)
                if not self._probe():
                    return True
            return not self._probe()

    def is_idle(self) -> bool:
        """Nothing downloads, seeds or waits in the queue (paused items may remain)."""
        self._require_observable()
        stats = self._rpc_raw("aria2.getGlobalStat") or {}
        if int(stats.get("numActive") or 0) > 0:
            return False
        if int(stats.get("numWaiting") or 0) > 0:
            rows = self._rpc_raw("aria2.tellWaiting", [0, 1000, ["gid", "status"]]) or []
            if any(
                isinstance(row, dict) and str(row.get("status") or "").casefold() != "paused"
                for row in (rows if isinstance(rows, list) else [])
            ):
                return False
        return True

    def stop_when_idle(self) -> bool:
        """Stop an idle sidecar; the next add, resume or start launches it again.

        The session file keeps paused downloads (with pause=true), so nothing
        is lost. The marker tells status reads that the stop was deliberate.
        """
        with self._lock:
            try:
                if not self.is_idle():
                    return False
            except Aria2Error:
                return False
            stopped = self.shutdown(save_session=True)
            if not stopped:
                # The shutdown was sent; aria2 may exit after the wait above. Its
                # marker must still say "stopped on purpose" so the next add or
                # resume starts it instead of failing on a dead RPC.
                time.sleep(2.0)
                stopped = not self._probe()
            if stopped:
                try:
                    (self.state_dir / self._IDLE_MARKER).write_text(f"{time.time():.0f}\n", encoding="utf-8")
                except OSError:
                    pass
            return stopped

    def mark_deliberately_stopped(self) -> bool:
        """Record that the sidecar is down on purpose (Torrents Off, app start or quit).

        Like an idle stop: status reads then report nothing instead of an error,
        and the next add or resume starts aria2 again.
        """
        if self._probe():
            return False
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / self._IDLE_MARKER).write_text(f"{time.time():.0f}\n", encoding="utf-8")
        except OSError:
            return False
        return True

    def idle_stopped(self) -> bool:
        """True while the sidecar is down because it was stopped for being idle."""
        return (self.state_dir / self._IDLE_MARKER).is_file() and not self._probe()

    def _load_secret(self) -> str:
        path = self.state_dir / "rpc-secret"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if not value:
            value = secrets.token_urlsafe(32)
            self._write_private(path, value + "\n")
        return value

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        # Created 0600 from the start: write-then-chmod left the secret
        # readable by other users for a moment.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # an existing file may still have wider permissions
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _conf_path(self) -> Path:
        return self.state_dir / "aria2-rpc.conf"

    def _binary_path(self) -> str:
        env = os.getenv("PUDGE_ARIA2C", "").strip()
        candidates = [env, self.binary, "/opt/homebrew/bin/aria2c", "/usr/local/bin/aria2c"]
        for candidate in candidates:
            if not candidate:
                continue
            found = shutil.which(candidate) if "/" not in candidate else candidate
            if found and Path(found).is_file():
                return str(found)
        raise Aria2Error(err("aria2.notInstalled"))

    def _metadata_path(self) -> Path:
        return self.state_dir / "metadata.json"

    def _runtime_profile_path(self) -> Path:
        return self.state_dir / "runtime-profile"

    def _load_metadata(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._metadata_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_metadata(self, data: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self._metadata_path(), data, ensure_ascii=False, indent=2, sort_keys=True)

    @contextmanager
    def _metadata_transaction(self) -> Iterator[dict[str, dict[str, Any]]]:
        """The current records, saved after the block (not when it raises), with no other
        client (thread or process) changing them in between."""
        path = self._metadata_path()
        with _METADATA_LOCKS_GUARD:
            entry = _METADATA_LOCKS.setdefault(str(path), [threading.RLock(), 0])
        with entry[0]:
            handle = None
            if entry[1] == 0 and fcntl is not None:
                try:
                    handle = os.open(self.state_dir / "metadata.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(handle, fcntl.LOCK_EX)
                except OSError:
                    if handle is not None:
                        os.close(handle)
                    handle = None  # the thread lock still applies
            entry[1] += 1
            try:
                data = self._load_metadata()
                yield data
                self._save_metadata(data)
            finally:
                entry[1] -= 1
                if handle is not None:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    finally:
                        os.close(handle)

    def prepare_network_start(self, *, hook: bool) -> None:
        """Run the pre-download hook (up to 90 s) before the start is admitted under the shared Off lock."""
        if hook and self.pre_download_command:
            self._run_hook()
            self._hook_prepared_at = time.monotonic()

    def _run_hook(self) -> None:
        if not self.pre_download_command:
            return
        prepared, self._hook_prepared_at = getattr(self, "_hook_prepared_at", 0.0), 0.0
        if prepared and time.monotonic() - prepared < 60.0:
            return  # prepare_network_start() has just run it.
        try:
            completed = subprocess.run(
                self.pre_download_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # A hung VPN script: the add fails like any refused command (callers catch
            # QBittorrentError), not with a TimeoutExpired out of the bridge.
            raise Aria2Error(err("manager.vpnCommandFailed", error=str(exc)[:300])) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise Aria2Error(err("manager.vpnCommandFailed", error=detail))

    def _rpc_raw(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": f"{APP_SLUG}-{time.time_ns()}",
            "method": method
            if method.startswith("aria2.") or method.startswith("system.")
            else f"aria2.{method}",
            "params": [f"token:{self._secret}", *(params or [])],
        }
        try:
            response = self._http.post(self._rpc_url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            # aria2 answers JSON-RPC errors (unknown GID, bad params) with
            # HTTP 400 plus a normal error body. Surface those exactly like a
            # 200 error response so callers can recognise "Invalid GID".
            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict) and isinstance(error_body.get("error"), dict):
                message = error_body["error"].get("message")
                if message:
                    raise Aria2Error(f"aria2 RPC: {message}") from exc
            detail = str(exc.response.text or "").strip().replace("\n", " ")[:500]
            if detail:
                raise Aria2Error(err("aria2.rpcUnavailableResponse", error=exc, response=detail)) from exc
            raise Aria2Error(err("aria2.rpcUnavailable", error=exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.ConnectError):
                _TRUSTED_RPC_LISTENERS.pop(self.rpc_port, None)  # stopped: check whoever listens next
            raise Aria2Error(err("aria2.rpcUnavailable", error=exc)) from exc
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise Aria2Error(f"aria2 RPC: {message}")
        return body.get("result") if isinstance(body, dict) else None

    def _probe(self) -> bool:
        # Another program (e.g. another account's) on the port never gets the secret.
        self._port_held_by_stranger = self._rpc_port_held_by_stranger()
        if self._port_held_by_stranger:
            return False
        try:
            self._rpc_raw("aria2.getVersion")
            return True
        except Aria2Error:
            return False

    def _rpc_listener_pids(self) -> list[int] | None:
        """PIDs listening on the RPC port that this account can see, or None when lsof could not tell.

        lsof run by an ordinary user lists only that user's processes, so a
        port held by another account's process comes back empty.
        """
        lsof = shutil.which("lsof") or next(
            (path for path in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.access(path, os.X_OK)), None
        )
        if not lsof:
            return None
        try:
            completed = subprocess.run(
                [lsof, "-nP", "-w", "-t", f"-iTCP:{self.rpc_port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        # 1 with no output is "no such listener"; anything else is a failure.
        if completed.returncode not in (0, 1) or (completed.returncode == 1 and completed.stderr.strip()):
            return None
        pids: list[int] = []
        for line in completed.stdout.split():
            try:
                pids.append(int(line))
            except ValueError:
                return None
        return sorted(set(pids))

    def _process_is_own_aria2(self, pid: int) -> bool:
        """An aria2c process of this account (``ps``: uid and executable name)."""
        try:
            completed = subprocess.run(
                ["ps", "-o", "uid=,comm=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        parts = completed.stdout.strip().split(None, 1)
        if completed.returncode != 0 or len(parts) != 2:
            return False
        try:
            uid = int(parts[0])
        except ValueError:
            return False
        name = Path(parts[1].strip()).name.casefold()
        return uid == os.getuid() and (name.startswith("aria2c") or name == Path(self.binary).name.casefold())

    @staticmethod
    def _process_start_time(pid: int) -> str | None:
        """When the process started (``ps -o lstart=``): with the pid it names one process."""
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        started = " ".join(completed.stdout.split())
        return started if completed.returncode == 0 and started else None

    @staticmethod
    def _own_process_alive(pid: int) -> bool:
        """The pid exists and belongs to this account (signal 0; no subprocess)."""
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False  # gone, or another account's process (PermissionError)
        return True

    # A listener not seen alive for this long has its start time compared again
    # (its pid may have been reused meanwhile); polls in between cost one signal-0 call.
    _LISTENER_RECHECK_SECONDS = 10.0

    def _trusted_listener_still_there(self) -> bool:
        cached = _TRUSTED_RPC_LISTENERS.get(self.rpc_port)
        if cached is None:
            return False
        pid, started, seen_at = cached
        now = time.monotonic()
        if not self._own_process_alive(pid) or (
            now - seen_at > self._LISTENER_RECHECK_SECONDS and self._process_start_time(pid) != started
        ):
            _TRUSTED_RPC_LISTENERS.pop(self.rpc_port, None)
            return False
        # Alive, same process: it still holds the port, nobody else can listen there.
        _TRUSTED_RPC_LISTENERS[self.rpc_port] = (pid, started, now)
        return True

    def _remember_trusted_listener(self, pid: int) -> None:
        started = self._process_start_time(pid)
        if started is not None:
            _TRUSTED_RPC_LISTENERS[self.rpc_port] = (int(pid), started, time.monotonic())

    def _rpc_listener_trusted(self) -> bool | None:
        """Whether the process on the RPC port is Pudge's aria2: True, False, or None (cannot tell).

        Ours is the aria2c this client started, or an aria2c of this account
        (a sidecar an earlier Pudge started). Anything else, e.g. another
        account's process that took the port while the sidecar was stopped,
        must never see the RPC secret or be asked which files to delete.
        """
        if self._trusted_listener_still_there():
            return True
        process = getattr(self, "_managed_process", None)
        started_pid = getattr(process, "pid", None) if self._started_sidecar_alive() else None
        pids = self._rpc_listener_pids()
        if pids is None:
            return None
        for pid in pids:
            if pid == started_pid or self._process_is_own_aria2(pid):
                self._remember_trusted_listener(pid)
                return True
        return False

    def _rpc_port_held_by_stranger(self) -> bool:
        """True when a process that is not Pudge's aria2 holds the RPC port.

        Asked before a request carries the RPC secret. Nobody listening, or no
        way to tell (no lsof), is no stranger. Once a listener is proven ours,
        later calls only check that the same process is still alive.
        """
        if self._trusted_listener_still_there():
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.rpc_port), timeout=0.5):
                pass
        except OSError:
            return False  # nothing listens: the request fails as before
        trusted = self._rpc_listener_trusted()
        if trusted is False:
            logging.getLogger(__name__).warning(
                "SKIP step=aria2.rpc reason=port_held_by_other_process port=%s", self.rpc_port
            )
        return trusted is False

    def _rpc_is_own_sidecar(self) -> bool:
        """True only when the aria2 answering on our port is Pudge's own sidecar.

        aria2 started without --rpc-secret accepts any token, so someone else's
        aria2 on this port used to pass _probe(); Pudge then changed its global
        options and shut it down. Pudge always sets a secret, so its sidecar
        rejects a wrong token with "Unauthorized". The proof is remembered per
        aria2 session id, because aria2 delays its answer to a wrong token.
        Before that, the listening process must be Pudge's aria2c: any program
        can answer "Unauthorized" (macOS lsof; without lsof only the token test).
        """
        key = (str(self.state_dir), self.rpc_port)
        if self._rpc_port_held_by_stranger():
            _OWN_SIDECAR_SESSIONS.pop(key, None)
            return False
        try:
            info = self._rpc_raw("aria2.getSessionInfo")
        except Aria2Error:
            info = None
        session = str(info.get("sessionId") or "") if isinstance(info, dict) else ""
        if session and _OWN_SIDECAR_SESSIONS.get(key) == session:
            return True
        payload = {
            "jsonrpc": "2.0",
            "id": f"{APP_SLUG}-{time.time_ns()}",
            "method": "aria2.getVersion",
            "params": [f"token:not-{APP_SLUG}-{secrets.token_urlsafe(16)}"],
        }
        try:
            body = self._http.post(self._rpc_url, json=payload).json()
        except httpx.TransportError:
            # No answer at all (it may be stopping): only the process this
            # client started itself still counts as ours.
            return self._started_sidecar_alive()
        except (httpx.HTTPError, ValueError):
            return False
        error = body.get("error") if isinstance(body, dict) else None
        message = str(error.get("message") or "") if isinstance(error, dict) else ""
        owned = "unauthorized" in message.casefold()
        if owned and session:
            _OWN_SIDECAR_SESSIONS[key] = session
        return owned

    def _started_sidecar_alive(self) -> bool:
        process = getattr(self, "_managed_process", None)
        poll = getattr(process, "poll", None)
        return callable(poll) and poll() is None

    def _interface_names(self) -> set[str]:
        try:
            return {str(name) for _index, name in socket.if_nameindex()}
        except OSError:
            return set()

    def _validate_network_guard(self) -> None:
        if self.vpn_kill_switch and not self.vpn_interface:
            raise Aria2Error(err("aria2.vpnInterfaceMissing"))
        if self.vpn_interface and self.vpn_interface not in self._interface_names():
            raise Aria2Error(err("aria2.vpnInterfaceDown", interface=repr(self.vpn_interface)))

    def network_guard_status(self) -> dict[str, Any]:
        active = bool(self.vpn_interface and self.vpn_interface in self._interface_names())
        return {
            "interface": self.vpn_interface,
            "active": active,
            "bound": bool(self.vpn_interface),
            "kill_switch": self.vpn_kill_switch,
            "protected": bool(self.vpn_interface and active),
            "seed_mode": self.seed_mode,
            "upload_limit_kib": self.upload_limit_kib,
        }

    def _seed_options(self) -> dict[str, str]:
        if self.seed_mode == "off":
            return {"seed-time": "0"}
        if self.seed_mode == "ratio":
            return {"seed-ratio": str(self.seed_ratio), "seed-time": "5256000"}
        if self.seed_mode == "ratio_or_time":
            return {
                "seed-ratio": str(self.seed_ratio),
                "seed-time": str(self.seed_time_minutes),
            }
        return {"seed-ratio": "0.0", "seed-time": "5256000"}

    def _runtime_options(self) -> dict[str, str]:
        return {
            **self._seed_options(),
            "bt-detach-seed-only": "true",
            "max-upload-limit": (f"{self.upload_limit_kib}K" if self.upload_limit_kib else "0"),
        }

    def _apply_runtime_options(self) -> None:
        options = self._runtime_options()
        try:
            current_global = self._rpc_raw("aria2.getGlobalOption") or {}
        except Aria2Error:
            current_global = {}
        global_delta = {
            key: value for key, value in options.items() if str(current_global.get(key, "")) != str(value)
        }
        if global_delta:
            try:
                self._rpc_raw("aria2.changeGlobalOption", [global_delta])
            except Aria2Error:
                pass

        # Never rewrite per-download options during ordinary polling. aria2 can
        # restart an active task when changeOption() touches mutable options, and
        # a recovery task intentionally needs check-integrity=true until its hash
        # pass is finished. New downloads already receive the current options at
        # add time, so global changes are sufficient here.

    def _launch_options(self) -> list[str]:
        values = [
            # Pudge's own 0600 config holds only the RPC secret, so it is not in
            # argv (visible to every user via ps). It also keeps the user's
            # ~/.aria2/aria2.conf from being read, which --no-conf did before.
            f"--conf-path={self._conf_path()}",
            "--bt-detach-seed-only=true",
            # Preallocation writes zeros over the whole file before the first
            # piece arrives; APFS does not need it.
            "--file-allocation=none",
            "--bt-max-peers=30",
        ]
        values.extend(f"--{key}={value}" for key, value in self._seed_options().items())
        if self.upload_limit_kib:
            values.append(f"--max-upload-limit={self.upload_limit_kib}K")
        if self.vpn_interface:
            values.append(f"--interface={self.vpn_interface}")
        return values

    def _start(self) -> None:
        if not self.enabled:
            raise Aria2Error(err("aria2.disabled"))
        self._validate_network_guard()
        binary = self._binary_path()
        _TRUSTED_RPC_LISTENERS.pop(self.rpc_port, None)  # the new process is checked once it listens
        session = self.state_dir / "session.txt"
        session.touch(exist_ok=True)
        (self.state_dir / self._IDLE_MARKER).unlink(missing_ok=True)
        log = self.state_dir / "aria2.log"
        try:
            self._write_private(self._conf_path(), f"rpc-secret={self._secret}\n")
        except OSError as exc:
            raise Aria2Error(err("aria2.startFailed", error=exc)) from exc
        cmd = [
            binary,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.rpc_port}",
            "--rpc-max-request-size=16M",
            "--rpc-save-upload-metadata=true",
            "--continue=true",
            # BitTorrent verifies every downloaded piece already. Rechecking a
            # large partial batch on every sidecar restart can keep one CPU core
            # busy for hours without adding safety.
            "--check-integrity=false",
            "--auto-file-renaming=false",
            "--max-concurrent-downloads=3",
            "--bt-save-metadata=true",
            "--bt-load-saved-metadata=true",
            "--bt-prioritize-piece=head=16M,tail=4M",
            *self._launch_options(),
            f"--input-file={session}",
            f"--save-session={session}",
            "--save-session-interval=30",
            f"--log={log}",
            "--log-level=notice",
            "--console-log-level=warn",
            "--summary-interval=0",
        ]
        try:
            self._managed_process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise Aria2Error(err("aria2.startFailed", error=exc)) from exc
        for delay in (0.15, 0.25, 0.4, 0.7, 1.0, 1.5):
            time.sleep(delay)
            if self._probe():
                if not self._rpc_is_own_sidecar():
                    # Another aria2 holds the port; ours could not take it.
                    raise Aria2Error(err("aria2.portInUse", port=self.rpc_port))
                self._apply_runtime_options()
                try:
                    self._runtime_profile_path().write_text(self._RUNTIME_PROFILE + "\n", encoding="utf-8")
                except OSError:
                    pass
                return
        if getattr(self, "_port_held_by_stranger", False):
            raise Aria2Error(err("aria2.portInUse", port=self.rpc_port))
        raise Aria2Error(err("aria2.rpcNoResponse", port=self.rpc_port))

    def ensure_running(self) -> None:
        with self._lock:
            self._validate_network_guard()
            if self._probe():
                if not self._rpc_is_own_sidecar():
                    # Someone else's aria2 answers on Pudge's port: never change
                    # its options, add downloads to it or shut it down.
                    raise Aria2Error(err("aria2.portInUse", port=self.rpc_port))
                try:
                    runtime_profile = self._runtime_profile_path().read_text(encoding="utf-8").strip()
                except OSError:
                    runtime_profile = ""
                if runtime_profile != self._RUNTIME_PROFILE:
                    # A managed sidecar started by an older Pudge needs the
                    # larger RPC limit and low-heat resume profile. Save its
                    # session, stop it cleanly and let aria2 resume every piece.
                    try:
                        self._rpc_raw("aria2.saveSession")
                        self._rpc_raw("aria2.forceShutdown")
                    except Aria2Error:
                        pass
                    for delay in (0.1, 0.2, 0.4, 0.8, 1.2):
                        time.sleep(delay)
                        if not self._probe():
                            break
                    self._start()
                    return
                try:
                    current = self._rpc_raw("aria2.getGlobalOption")
                except Aria2Error:
                    current = None
                if isinstance(current, dict):
                    running_interface = str(current.get("interface") or "").strip()
                    if running_interface != self.vpn_interface:
                        try:
                            self._rpc_raw("aria2.saveSession")
                            self._rpc_raw("aria2.forceShutdown")
                        except Aria2Error:
                            pass
                        time.sleep(0.2)
                        self._start()
                        return
                self._apply_runtime_options()
                return
            if getattr(self, "_port_held_by_stranger", False):
                raise Aria2Error(err("aria2.portInUse", port=self.rpc_port))
            if not self.auto_start:
                raise Aria2Error(err("aria2.rpcNotRunning"))
            # Repair a Pudge-owned orphan from an older RPC secret/port before
            # starting the current sidecar.  Never kill a foreign aria2: the
            # ownership matcher requires this exact state/session path.
            stopped = self._stop_unreachable_managed_sidecars()
            if stopped is False:
                raise Aria2Error(err("aria2.oldSidecarStuck"))
            self._start()

    def _require_observable(self) -> None:
        """Read-only RPC guard: a status/inspection call must not launch or restart aria2."""
        if not self._probe():
            raise Aria2Error(err("aria2.rpcNotRunning"))

    def version(self) -> str:
        self._require_observable()
        result = self._rpc_raw("aria2.getVersion")
        return str((result or {}).get("version") or "unknown")

    @staticmethod
    def _safe_gid(info_hash: str, magnet: str) -> str:
        import hashlib

        source = (info_hash or hashlib.sha1(magnet.encode("utf-8")).hexdigest()).lower()
        return source[:16].ljust(16, "0")

    def _all_statuses(self) -> list[dict[str, Any]]:
        self._require_observable()
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "uploadSpeed",
            "connections",
            "numSeeders",
            "seeder",
            "dir",
            "files",
            "bittorrent",
            "infoHash",
            "errorCode",
            "errorMessage",
            "followedBy",
            "following",
            "verifiedLength",
            "verifyIntegrityPending",
        ]
        result: list[dict[str, Any]] = []
        for method, params in (
            ("aria2.tellActive", [keys]),
            ("aria2.tellWaiting", [0, 1000, keys]),
            ("aria2.tellStopped", [0, 1000, keys]),
        ):
            payload = self._rpc_raw(method, params)
            if isinstance(payload, list):
                result.extend(item for item in payload if isinstance(item, dict))
        by_gid = {str(item.get("gid") or ""): item for item in result if item.get("gid")}
        return list(by_gid.values())

    def _resolve_gid(self, torrent_hash: str) -> str:
        wanted = str(torrent_hash or "").strip().lower()
        if not wanted:
            # An empty hash names nothing: it matched a task or record without an
            # info hash (an HTTP download, a bad record), and delete("") removed that.
            return ""
        # Prefer the live payload GID. Magnet metadata downloads can retain the
        # same info hash in our ownership map after handing off to followedBy.
        for item in self._all_statuses():
            if wanted in {str(item.get("gid") or "").lower(), str(item.get("infoHash") or "").lower()}:
                if not item.get("followedBy"):
                    return str(item["gid"])
        metadata = self._load_metadata()
        for gid, meta in metadata.items():
            if wanted in {gid.lower(), str(meta.get("info_hash") or "").lower()}:
                return gid
        return str(torrent_hash or "").strip()

    def _source_proxies(self) -> list[str | None]:
        mode = self.source_proxy_mode
        proxy = self.source_proxy_url or None
        if mode == "proxy_only":
            attempts = [proxy] if proxy else []
        elif mode == "proxy_then_direct":
            attempts = [proxy, None]
        elif mode == "direct":
            attempts = [None]
        else:
            attempts = [None, proxy]
        return list(dict.fromkeys(attempts))

    def _torrent_payload(self, url: str) -> bytes | None:
        source = str(url or "").strip()
        if not source.casefold().startswith(("http://", "https://")):
            return None
        for proxy in self._source_proxies():
            try:
                with httpx.Client(
                    timeout=min(float(self.timeout), 8.0),
                    follow_redirects=True,
                    proxy=proxy,
                    headers={"User-Agent": APP_SLUG},
                ) as client:
                    # Streamed with a byte cap and an overall deadline: the
                    # whole body used to be read before the 10 MB check.
                    payload = read_capped_body(client, source, max_bytes=10 * 1024 * 1024)
            except (ImportError, httpx.HTTPError, ValueError):
                continue
            # A bencoded torrent is a dictionary. Refuse HTML/error pages and
            # unexpectedly large responses before sending anything to aria2.
            if 0 < len(payload) <= 10 * 1024 * 1024 and payload.startswith(b"d"):
                return payload
        return None

    @staticmethod
    def _richest_magnet(*sources: str) -> str:
        magnets = [
            str(source or "").strip()
            for source in sources
            if str(source or "").strip().casefold().startswith("magnet:?")
        ]
        if not magnets:
            return ""
        return max(
            magnets,
            key=lambda value: (
                value.casefold().count("&tr="),
                len(value),
            ),
        )

    @classmethod
    def _preferred_release_source(cls, release: NyaaRelease) -> str:
        richest = cls._richest_magnet(release.magnet, release.torrent_url)
        if richest:
            return richest
        return str(release.magnet or release.torrent_url or "").strip()

    def _add_source(
        self,
        release: NyaaRelease,
        options: dict[str, str],
        *,
        torrent_payload: bytes | None = None,
    ) -> str:
        payload = torrent_payload
        if payload is None:
            payload = self._torrent_payload(release.torrent_url)
        if payload and len(payload) <= self._SAFE_RPC_TORRENT_BYTES:
            encoded = base64.b64encode(payload).decode("ascii")
            try:
                returned = self._rpc_raw("aria2.addTorrent", [encoded, [], options])
                return str(returned or options["gid"])
            except Aria2Error as exc:
                if "GID" in str(exc).upper() or "duplicate" in str(exc).lower():
                    raise
                # If the source host was reachable but aria2 rejected the file,
                # retain the old magnet path as a safe compatibility fallback.
        source = self._preferred_release_source(release)
        if not source:
            raise Aria2Error(err("aria2.noReleaseLink"))
        returned = self._rpc_raw("aria2.addUri", [[source], options])
        return str(returned or options["gid"])

    def add_release(
        self,
        release: NyaaRelease,
        *,
        save_path: Path,
        category: str,
        tags: list[str],
        paused: bool = False,
        stop_at_metadata: bool = False,
    ) -> str:
        del category
        self._run_hook()
        self.ensure_running()
        target = save_path.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        metadata = self._load_metadata()
        wanted_hash = str(release.info_hash or "").lower()
        for gid, meta in metadata.items():
            if wanted_hash and str(meta.get("info_hash") or "").lower() == wanted_hash:
                try:
                    self._rpc_raw("aria2.tellStatus", [gid, ["gid", "status"]])
                    return wanted_hash or gid
                except Aria2Error:
                    pass
        gid = self._safe_gid(release.info_hash, release.magnet)
        torrent_payload = self._torrent_payload(release.torrent_url)
        pause_initial = bool(paused or self.paused_on_add)
        if stop_at_metadata and not torrent_payload:
            # A magnet/remote source must run long enough to obtain metadata;
            # pause-metadata then pauses the actual BitTorrent payload.
            pause_initial = False
        options = {
            "dir": str(target),
            "pause": "true" if pause_initial else "false",
            "pause-metadata": "true" if stop_at_metadata else "false",
            "gid": gid,
            "bt-save-metadata": "true",
            "bt-load-saved-metadata": "true",
            "check-integrity": "false",
            **self._seed_options(),
        }
        try:
            returned_gid = self._add_source(release, options, torrent_payload=torrent_payload)
        except Aria2Error as exc:
            if "GID" not in str(exc).upper() and "duplicate" not in str(exc).lower():
                raise
            returned_gid = gid
        media_id = None
        episode = None
        is_batch = False
        anime_title = ""
        score = release.score
        for tag in tags:
            low = tag.casefold()
            if low.startswith("anilist:"):
                try:
                    media_id = int(tag.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif low.startswith("episode:"):
                try:
                    episode = int(tag.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif low.startswith("anime:"):
                anime_title = tag.split(":", 1)[1].strip()
            elif low in {"series pack", "batch"}:
                is_batch = True
            elif low.startswith("score:"):
                try:
                    score = float(tag.split(":", 1)[1].strip())
                except ValueError:
                    pass
        record = {
            "info_hash": wanted_hash,
            "title": release.title,
            "save_path": str(target),
            "media_id": media_id,
            "episode": episode,
            "release_episode": parsed_release_episode(release.title) or episode,
            "is_batch": bool(is_batch),
            "anime_title": anime_title,
            "release_score": float(score),
            "expected_size_bytes": max(0, int(release.size_bytes or 0)),
            "source_url": str(release.torrent_url or release.link or ""),
            "magnet": self._richest_magnet(release.magnet, release.torrent_url),
            "listed_seeders": max(0, int(release.seeders or 0)),
            "listed_leechers": max(0, int(release.leechers or 0)),
            "added_on": int(time.time()),
            "completed_on": 0,
        }
        with self._metadata_transaction() as current:
            current[returned_gid] = record
        # A .torrent added by upload is saved at once; a magnet's later (torrents()).
        self._hide_saved_metadata(target, wanted_hash)
        return wanted_hash or returned_gid

    @staticmethod
    def _hide_saved_metadata(directory: object, info_hash: object) -> bool:
        """Hide aria2's ``<dir>/<info hash>.torrent`` from Finder in the library folder.

        aria2 writes it next to the download (``--bt-save-metadata``,
        ``--rpc-save-upload-metadata``) and reads it back from exactly there on a
        restart (the session and ``--bt-load-saved-metadata``), so it can be
        neither moved nor renamed: it only gets macOS's hidden flag.
        """
        value = str(info_hash or "").strip().lower()
        folder = str(directory or "").strip()
        if len(value) != 40 or not folder:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        path = Path(folder) / f"{value}.torrent"
        try:
            if not path.is_file():
                return False
        except OSError:
            return False
        return hide_from_finder(path)

    def start(self, torrent_hash: str) -> None:
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = self._rpc_raw("aria2.tellStatus", [gid, ["status"]]) or {}
        if str(status.get("status") or "").casefold() != "paused":
            return
        self._rpc_raw("aria2.unpause", [gid])

    def pause(self, torrent_hash: str) -> None:
        self._require_observable()
        gid = self._resolve_gid(torrent_hash)
        status = self._rpc_raw("aria2.tellStatus", [gid, ["status"]]) or {}
        if str(status.get("status") or "").casefold() not in {"active", "waiting"}:
            return
        self._rpc_raw("aria2.pause", [gid])

    @staticmethod
    def _missing_control_file_error(status: dict[str, Any]) -> bool:
        return bool(
            str(status.get("errorCode") or "") == "13"
            and ".aria2" in str(status.get("errorMessage") or "").casefold()
        )

    def _recover_missing_control_file(
        self,
        gid: str,
        status: dict[str, Any],
        *,
        torrent_payload: bytes | None = None,
    ) -> bool:
        """Rebuild BitTorrent piece state after a lost ``*.aria2`` file.

        ``allow-overwrite`` is deliberately never used: aria2 documents that it
        starts from scratch. A torrent hash-check can reconstruct the piece map
        safely from the existing files instead.
        """

        if not self._missing_control_file_error(status):
            return False
        metadata = self._load_metadata()
        meta = dict(metadata.get(gid) or {})
        if not meta:
            return False
        payload = torrent_payload
        if payload is None:
            payload = self._torrent_payload(str(meta.get("source_url") or ""))
        magnet = self._richest_magnet(
            str(meta.get("magnet") or ""),
            str(meta.get("source_url") or ""),
        )
        if not magnet:
            # Older Pudge metadata predates the persisted magnet field. The
            # BitTorrent info hash is sufficient to ask DHT/trackers for the
            # torrent metadata again, after which check-integrity reconstructs
            # the piece map from the existing files.
            info_hash = str(meta.get("info_hash") or "").strip()
            if len(info_hash) in {32, 40} and info_hash.isalnum():
                title = str(meta.get("title") or "").strip()
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
                if title:
                    magnet += f"&dn={quote(title)}"
        if not payload and not magnet:
            return False

        options = {
            "dir": str(meta.get("save_path") or status.get("dir") or self.state_dir),
            "pause": "false",
            "gid": gid,
            "bt-save-metadata": "true",
            "bt-load-saved-metadata": "true",
            "check-integrity": "true",
            **self._seed_options(),
        }
        try:
            self._rpc_raw("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            try:
                self._rpc_raw("aria2.forceRemove", [gid])
            except Aria2Error:
                pass
            try:
                self._rpc_raw("aria2.removeDownloadResult", [gid])
            except Aria2Error:
                pass

        if payload:
            encoded = base64.b64encode(payload).decode("ascii")
            returned_gid = str(self._rpc_raw("aria2.addTorrent", [encoded, [], options]) or gid)
        else:
            returned_gid = str(self._rpc_raw("aria2.addUri", [[magnet], options]) or gid)
        meta["recovery_started_at"] = int(time.time())
        with self._metadata_transaction() as current:
            current.pop(gid, None)
            current[returned_gid] = meta
        try:
            self._rpc_raw("aria2.saveSession")
        except Aria2Error:
            pass
        return True

    def _upgrade_metadata_only_source(
        self,
        gid: str,
        status: dict[str, Any],
    ) -> bool:
        state = str(status.get("status") or "").casefold()
        if state not in {"active", "waiting", "paused"}:
            return False
        if int(status.get("totalLength") or 0) > 0:
            return False
        if int(status.get("completedLength") or 0) > 0:
            return False

        metadata = self._load_metadata()
        meta = dict(metadata.get(gid) or {})
        if not meta:
            return False

        current = str(meta.get("magnet") or "").strip()
        replacement = self._richest_magnet(
            current,
            str(meta.get("source_url") or ""),
        )
        if not replacement:
            return False
        if replacement == current:
            return False
        if replacement.casefold().count("&tr=") <= current.casefold().count("&tr="):
            return False

        options = {
            "dir": str(meta.get("save_path") or status.get("dir") or self.state_dir),
            "pause": "false",
            "gid": gid,
            "bt-save-metadata": "true",
            "bt-load-saved-metadata": "true",
            "check-integrity": "false",
            **self._seed_options(),
        }
        try:
            self._rpc_raw("aria2.forceRemove", [gid])
        except Aria2Error:
            pass
        try:
            self._rpc_raw("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            pass

        returned_gid = str(self._rpc_raw("aria2.addUri", [[replacement], options]) or gid)
        meta["magnet"] = replacement
        meta["recovery_started_at"] = int(time.time())
        with self._metadata_transaction() as current:
            current.pop(gid, None)
            current[returned_gid] = meta
        try:
            self._rpc_raw("aria2.saveSession")
        except Aria2Error:
            pass
        return True

    def reconnect(self, torrent_hash: str) -> bool:
        """Reannounce a stall or safely reconstruct a lost BitTorrent control file."""

        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = (
            self._rpc_raw(
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "errorCode",
                        "errorMessage",
                        "dir",
                    ],
                ],
            )
            or {}
        )
        state = str(status.get("status") or "").casefold()
        if self._upgrade_metadata_only_source(gid, status):
            return True
        if state == "error":
            return self._recover_missing_control_file(gid, status)
        if state in {"complete", "removed"}:
            return False
        if state in {"active", "waiting"}:
            # aria2 has no force-reannounce RPC. A force-pause/unpause cycle is
            # its safe equivalent and preserves the .aria2 control file and all
            # verified pieces.
            self._rpc_raw("aria2.forcePause", [gid])
        last_error: Aria2Error | None = None
        for delay in (0.0, 0.05, 0.15):
            if delay:
                time.sleep(delay)
            try:
                self._rpc_raw("aria2.unpause", [gid])
                return True
            except Aria2Error as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return False

    def prioritize(self, torrent_hash: str) -> bool:
        """Move one Pudge download to the front of aria2's waiting queue."""
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = self._rpc_raw("aria2.tellStatus", [gid, ["status"]]) or {}
        state = str(status.get("status") or "").casefold()
        if state == "waiting":
            self._rpc_raw("aria2.changePosition", [gid, 0, "POS_SET"])
            return True
        if state == "paused":
            self._rpc_raw("aria2.changePosition", [gid, 0, "POS_SET"])
            self._rpc_raw("aria2.unpause", [gid])
            return True
        return state == "active"

    def prioritize_queue(self, torrent_hashes: list[str]) -> bool:
        """Order Pudge downloads in aria2's queue as given (first = most wanted).

        Waiting ones move to the front in that order. When the first one still waits while a
        later one of the list is active, that later one gives its slot up (pause, then back to
        the waiting queue) so the first starts now."""
        hashes = [str(value or "").strip() for value in torrent_hashes if str(value or "").strip()]
        if len(hashes) < 2:
            return False
        self.ensure_running()
        gids: list[str] = []
        states: dict[str, str] = {}
        for value in hashes:
            try:
                gid = self._resolve_gid(value)
                status = self._rpc_raw("aria2.tellStatus", [gid, ["status"]]) or {}
            except Aria2Error:
                continue
            gids.append(gid)
            states[gid] = str(status.get("status") or "").casefold()
        moved = False
        for gid in reversed(gids):
            if states.get(gid) in {"waiting", "paused"}:
                try:
                    self._rpc_raw("aria2.changePosition", [gid, 0, "POS_SET"])
                    moved = True
                except Aria2Error:
                    continue
        if gids and states.get(gids[0]) == "waiting":
            for gid in reversed(gids[1:]):
                if states.get(gid) != "active":
                    continue
                try:
                    self._rpc_raw("aria2.forcePause", [gid])
                    self._rpc_raw("aria2.unpause", [gid])
                    # A paused active download goes to the front of the waiting queue: the
                    # wanted one is put back in front of it, or it took its slot again.
                    self._rpc_raw("aria2.changePosition", [gids[0], 0, "POS_SET"])
                    moved = True
                except Aria2Error:
                    pass
                break
        return moved

    def known_offline(self, torrent_hash: str) -> bool:
        """Whether Pudge's aria2 records hold this torrent, without the RPC.

        An add that never reached aria2 leaves no record, so while the RPC is
        stopped a hash missing here cannot be waiting inside aria2.
        """
        wanted = str(torrent_hash or "").strip().lower()
        if not wanted:
            return False
        return any(
            wanted in {str(gid).lower(), str((meta or {}).get("info_hash") or "").lower()}
            for gid, meta in self._load_metadata().items()
        )

    def has_torrent(self, torrent_hash: str) -> bool:
        """Whether aria2 already holds this torrent, so add_release would reuse it."""
        wanted = str(torrent_hash or "").strip().lower()
        if not wanted:
            return False
        if not self._probe():
            if not self.known_offline(wanted):
                return False  # no record: starting aria2 cannot restore it either
            # Starting aria2 restores the task from its session. Start it the
            # way add_release does (hook first), then look.
            self.prepare_network_start(hook=True)
            self.ensure_running()
        gids = {self._safe_gid(wanted, "")} | {
            str(gid).lower()
            for gid, meta in self._load_metadata().items()
            if str((meta or {}).get("info_hash") or "").lower() == wanted
        }
        return any(
            str(item.get("infoHash") or "").lower() == wanted or str(item.get("gid") or "").lower() in gids
            for item in self._all_statuses()
        )

    def torrent_status(self, torrent_hash: str) -> dict[str, Any] | None:
        """Return the small progress surface used by the common release racer."""
        self._require_observable()
        gid = self._resolve_gid(torrent_hash)
        try:
            item = (
                self._rpc_raw(
                    "aria2.tellStatus",
                    [
                        gid,
                        [
                            "status",
                            "totalLength",
                            "completedLength",
                            "downloadSpeed",
                            "connections",
                            "numSeeders",
                            "seeder",
                        ],
                    ],
                )
                or {}
            )
        except Aria2Error as exc:
            # Only a tellStatus rejection for this exact GID proves absence.
            # Transport failures and failures while resolving the GID remain
            # errors, so cleanup cannot discard ownership on an unknown result.
            missing = {
                f"aria2 rpc: gid #{gid}# is not found",
                f"aria2 rpc: gid {gid} is not found",
                f"aria2 rpc: invalid gid {gid}",
            }
            if str(exc).strip().casefold() in {message.casefold() for message in missing}:
                return None
            raise
        if str(item.get("status") or "").casefold() == "removed":
            # A removed task never resumes; only its result is still listed.
            return None
        total = max(0, int(item.get("totalLength") or 0))
        downloaded = max(0, int(item.get("completedLength") or 0))
        progress = downloaded / total if total > 0 else 0.0
        return {
            "status": str(item.get("status") or ""),
            "dlspeed": max(0, int(item.get("downloadSpeed") or 0)),
            "downloaded": downloaded,
            "progress": max(0.0, min(1.0, progress)),
            "total_size": total,
            "connections": max(0, int(item.get("connections") or 0)),
            "num_seeders": max(0, int(item.get("numSeeders") or 0)),
            "seeder": str(item.get("seeder") or "false").casefold() == "true",
        }

    def repair_stalled_release(self, torrent_hash: str, release: NyaaRelease) -> bool:
        """Replace a metadata-only magnet with Nyaa's tracker-rich torrent."""

        payload = self._torrent_payload(release.torrent_url)
        if not payload and not self._preferred_release_source(release):
            return False
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = (
            self._rpc_raw(
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "files",
                        "errorCode",
                        "errorMessage",
                        "dir",
                    ],
                ],
            )
            or {}
        )
        if self._missing_control_file_error(status):
            return self._recover_missing_control_file(gid, status, torrent_payload=payload)
        files = status.get("files") if isinstance(status.get("files"), list) else []
        has_data = any(int(entry.get("length") or 0) > 0 for entry in files if isinstance(entry, dict))
        if (
            str(status.get("status") or "").casefold() not in {"active", "waiting", "paused"}
            or int(status.get("totalLength") or 0) > 0
            or int(status.get("completedLength") or 0) > 0
            or has_data
        ):
            return False

        metadata = self._load_metadata()
        meta = dict(metadata.get(gid) or {})
        options = {
            "dir": str(meta.get("save_path") or self.state_dir),
            "pause": ("true" if str(status.get("status") or "").casefold() == "paused" else "false"),
            "gid": gid,
            "bt-save-metadata": "true",
            "bt-load-saved-metadata": "true",
            "check-integrity": "false",
            **self._seed_options(),
        }
        self._rpc_raw("aria2.forceRemove", [gid])
        try:
            self._rpc_raw("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            pass
        returned_gid = self._add_source(release, options, torrent_payload=payload)
        meta.update(
            {
                "info_hash": str(release.info_hash or meta.get("info_hash") or "").lower(),
                "title": release.title or str(meta.get("title") or ""),
                "source_url": str(release.torrent_url or release.link or ""),
                "magnet": str(release.magnet or meta.get("magnet") or ""),
                "listed_seeders": max(0, int(release.seeders or 0)),
                "listed_leechers": max(0, int(release.leechers or 0)),
                "expected_size_bytes": max(0, int(release.size_bytes or 0)),
            }
        )
        with self._metadata_transaction() as current:
            current.pop(gid, None)
            current[returned_gid] = meta
        return True

    @staticmethod
    def _content_path(item: dict[str, Any], meta: dict[str, Any]) -> str:
        files = item.get("files") if isinstance(item.get("files"), list) else []
        paths = [
            str(entry.get("path") or "") for entry in files if isinstance(entry, dict) and entry.get("path")
        ]
        if len(paths) == 1:
            return paths[0]
        directory = str(item.get("dir") or meta.get("save_path") or "")
        name = str(((item.get("bittorrent") or {}).get("info") or {}).get("name") or "")
        return str(Path(directory) / name) if directory and name else directory

    @staticmethod
    def _has_control_file(content: Path, save_path: Path) -> bool:
        """True when aria2's ``<name>.aria2`` exists for the file or a folder above it."""
        current = content
        while current != save_path and current.parent != current:
            if current.with_name(current.name + ".aria2").exists():
                return True
            current = current.parent
        return False

    @classmethod
    def _video_file_names(cls, item: dict[str, Any]) -> list[str]:
        """Names of the video files of a live aria2 task (empty before its metadata arrived)."""
        files = item.get("files") if isinstance(item.get("files"), list) else []
        names: list[str] = []
        for entry in files:
            path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
            if (
                path
                and not path.startswith("[METADATA]")
                and Path(path).suffix.casefold() in cls._VIDEO_EXTENSIONS
            ):
                names.append(Path(path).name)
        return names

    @staticmethod
    def _single_episode_title(title: str) -> bool:
        """True for a title naming one episode ("Show S03E49 1080p"), not a range, pack or season."""
        return bool(
            title
            and parsed_release_episode(title) is not None
            and release_episode_range(title) is None
            and not BATCH_RE.search(title)
        )

    @classmethod
    def _batch_flag(cls, meta: dict[str, Any], *, title_fallback: bool) -> bool:
        """A "series pack" request whose torrent holds one video file is not a batch."""
        if not meta.get("is_batch"):
            return False
        count = int(meta.get("video_count") or 0)
        if count:
            return count > 1
        return not (title_fallback and cls._single_episode_title(str(meta.get("title") or "")))

    @classmethod
    def _completed_payload_on_disk(cls, save_path: Path, info_hash: str) -> tuple[Path, int] | None:
        """The finished payload of a torrent, proven by aria2's own saved .torrent.

        aria2 keeps a task's metadata as ``<dir>/<info hash>.torrent`` and deletes
        the ``<name>.aria2`` control file only when the download is complete. So:
        no control file, every file of the torrent with its exact length (a file
        that is not there at all was deselected or deleted), and at least one video.
        Returns (content path, bytes present).
        """
        value = str(info_hash or "").strip().lower()
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            return None
        torrent = save_path / f"{value}.torrent"
        layout = _saved_torrent_layout(torrent)
        if layout is None:
            return None
        name, files, multi = layout
        content = save_path / name
        if (save_path / f"{name}.aria2").exists():
            return None
        present = 0
        videos = 0
        for relative, length in files:
            path = content / relative if multi else content
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                return None
            if info.st_size != length or Path(str(path) + ".aria2").exists():
                return None
            if _hollow(path, length, info):
                # aria2 writes the last pieces early (tail priority, no preallocation): the file
                # has its full length while most of it is a hole. Not finished.
                return None
            present += length
            if path.suffix.casefold() in cls._VIDEO_EXTENSIONS:
                videos += 1
        return (content, present) if videos else None

    def _orphan_download_item(
        self,
        gid: str,
        meta: dict[str, Any],
        *,
        save_path: Path,
        content: Path,
        size: int,
    ) -> DownloadItem | None:
        title = str(meta.get("title") or "").strip()
        media_id = meta.get("media_id")
        media_episode = meta.get("episode")
        if media_id is None:
            return None
        release_number = meta.get("release_episode")
        if release_number is None:
            release_number = parsed_release_episode(title) or media_episode
        torrent_hash = str(meta.get("info_hash") or gid).strip().lower() or str(gid)
        is_batch = self._batch_flag(meta, title_fallback=True)
        if content.is_dir():
            release_number = meta.get("release_episode")
        video_episodes = list(meta.get("video_episodes") or [])
        if not video_episodes and not is_batch and release_number is not None:
            video_episodes = [int(release_number)]
        return DownloadItem(
            torrent_hash=torrent_hash,
            name=title or content.name,
            state="complete",
            progress=1.0,
            save_path=str(save_path.resolve()),
            content_path=str(content.resolve()),
            media_id=int(media_id),
            episode=int(media_episode) if media_episode is not None else None,
            media_episode=int(media_episode) if media_episode is not None else None,
            release_episode=int(release_number) if release_number is not None else None,
            is_batch=is_batch,
            added_on=int(meta.get("added_on") or 0),
            completed_on=int(meta.get("completed_on") or 0) or int(time.time()),
            raw={
                "backend": "aria2",
                "gid": str(gid),
                "infohash_v1": str(meta.get("info_hash") or "").lower(),
                "total_size": size,
                "downloaded": size,
                "download_speed": 0,
                "upload_speed": 0,
                "orphaned_metadata": True,
                "video_episodes": video_episodes,
                "single_video_file": bool(meta.get("is_batch")) and not is_batch,
                "verified": size,
                "verifying": False,
                "_media_id_source": "aria2_metadata_orphan",
                "_release_score_tag": float(meta.get("release_score") or 0.0),
                "_anime_title_tag": str(meta.get("anime_title") or ""),
            },
        )

    def completed_on_disk(
        self, *, skip_hashes: set[str] | frozenset[str] = frozenset()
    ) -> list[DownloadItem]:
        """Managed torrents found finished on disk, without asking aria2 (no RPC).

        With Torrents off aria2 is not running, and a task that finished just
        before aria2 stopped is no longer in its session (Rakugo Shinjuu S2: a
        12-episode batch complete on disk, its row "downloading 99.8%" for good).
        Only the exact check against the saved .torrent is used here.
        """
        skip = {str(value).strip().casefold() for value in skip_hashes}
        result: list[DownloadItem] = []
        for gid, meta in self._load_metadata().items():
            if not isinstance(meta, dict):
                continue
            info_hash = str(meta.get("info_hash") or "").strip().casefold()
            if not info_hash or info_hash in skip:
                continue
            save_path = Path(str(meta.get("save_path") or "")).expanduser()
            found = self._completed_payload_on_disk(save_path, info_hash)
            if found is None:
                continue
            item = self._orphan_download_item(
                str(gid), dict(meta), save_path=save_path, content=found[0], size=found[1]
            )
            if item is not None:
                result.append(item)
                skip.add(info_hash)
        return result

    def _orphaned_completed_item(self, gid: str, meta: dict[str, Any]) -> DownloadItem | None:
        """Recover a completed managed torrent after aria2 forgot its task row.

        aria2 can drop the stopped/result entry while Pudge's metadata.json still
        contains the ownership information. The payload itself remains on disk.
        Reconstructing a read-only DownloadItem here lets normal sync_downloads()
        recreate the missing LibraryEpisode without re-adding or touching traffic.
        A batch is recognised only by aria2's saved .torrent: no folder guessing.
        """
        save_path = Path(str(meta.get("save_path") or "")).expanduser()
        if not save_path.is_dir():
            return None
        found = self._completed_payload_on_disk(save_path, str(meta.get("info_hash") or ""))
        if found is not None:
            return self._orphan_download_item(gid, meta, save_path=save_path, content=found[0], size=found[1])
        title = str(meta.get("title") or "").strip()
        candidates: list[Path] = []
        # The title is the release name from the feed: only a plain file name can be the
        # payload. "../../Movies/x.mkv" named a file outside the download folder, which was
        # then taken for a finished episode of this anime.
        if title and "/" not in title and title not in {".", ".."}:
            exact = save_path / title
            if exact.is_file():
                candidates.append(exact)
        if not candidates:
            try:
                candidates = [
                    path
                    for path in save_path.rglob("*")
                    if path.is_file() and path.suffix.casefold() in self._VIDEO_EXTENSIONS
                ]
            except OSError:
                candidates = []
        if len(candidates) > 1:
            release_number = meta.get("release_episode")
            if release_number is None:
                release_number = parsed_release_episode(title)
            if release_number is not None:
                matching = [
                    path for path in candidates if parsed_release_episode(path.name) == int(release_number)
                ]
                if len(matching) == 1:
                    candidates = matching
        if len(candidates) != 1:
            return None
        content = candidates[0].resolve()
        if self._has_control_file(content, save_path.resolve()):
            # aria2 preallocates the payload; its .aria2 control file next to the
            # file (or the torrent's top folder) means the download never finished.
            return None
        try:
            size = max(1, int(content.stat().st_size))
        except OSError:
            return None
        try:
            expected_size = max(0, int(meta.get("expected_size_bytes") or 0))
        except (TypeError, ValueError):
            expected_size = 0
        if expected_size > 0:
            size_ratio = size / max(1, expected_size)
            # Nyaa size strings are rounded, so this is deliberately broad.
            # It only rejects obvious orphan placeholders/wrong payloads; a
            # normal completed torrent remains well inside this range.
            if size_ratio < 0.65 or size_ratio > 1.35:
                return None
        return self._orphan_download_item(gid, meta, save_path=save_path, content=content, size=size)

    @staticmethod
    def _folder_reachable(save_path: Path) -> bool:
        """The download folder's parent is there: a missing folder below it was deleted, not on
        an unmounted drive (whose files are only out of reach)."""
        try:
            return save_path.parent.is_dir()
        except OSError:
            return False

    @classmethod
    def _live_payload_gone(cls, item: dict[str, Any]) -> bool:
        """Every selected file of a live aria2 task is gone from a reachable folder."""
        paths = [
            Path(str(entry.get("path") or "")).expanduser()
            for entry in item.get("files") or []
            if isinstance(entry, dict) and entry.get("path") and entry.get("selected") != "false"
        ]
        if not paths or not cls._folder_reachable(Path(str(item.get("dir") or paths[0].parent))):
            return False
        try:
            return not any(path.exists() for path in paths)
        except OSError:
            return False

    @classmethod
    def _recorded_payload_gone(cls, meta: dict[str, Any]) -> bool:
        """A finished torrent aria2 no longer lists has none of its files on disk (proven by its
        saved .torrent, or its folder gone). In doubt (no layout, drive away): not gone."""
        raw = str(meta.get("save_path") or "").strip()
        if not raw:
            return False
        save_path = Path(raw).expanduser()
        if not cls._folder_reachable(save_path):
            return False
        try:
            if not save_path.is_dir():
                return True  # its folder was deleted
        except OSError:
            return False
        value = str(meta.get("info_hash") or "").strip().lower()
        layout = _saved_torrent_layout(save_path / f"{value}.torrent") if len(value) == 40 else None
        if layout is None:
            return False
        name, files, multi = layout
        content = save_path / name
        try:
            return not any((content / relative if multi else content).exists() for relative, _length in files)
        except OSError:
            return False

    def _forget_finished_gone(
        self, metadata: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]], stale: set[str]
    ) -> set[str]:
        """metadata.json grew by every finished download for ever: a record whose download is
        finished and whose files are gone from disk (the episode deleted, the folder removed)
        goes, with aria2's own row of it (a stopped result or a paused finished task). Records
        sharing its info hash (a magnet's [METADATA] parent) go with it. Returns the gids."""
        dead_hashes = {
            str(metadata[gid].get("info_hash") or "").strip().lower() for gid in stale if gid in metadata
        }
        dead_hashes.discard("")
        # Not a live download of its own: a new download of the same torrent (added again
        # after its files were deleted) keeps its record; a parent aria2 lists still goes.
        gone = set(stale) | {
            gid
            for gid, meta in metadata.items()
            if str((meta or {}).get("info_hash") or "").strip().lower() in dead_hashes
            and (gid not in live or (live.get(gid) or {}).get("followedBy"))
        }
        for gid in sorted(gone):
            row = live.get(gid)
            if row is None:
                continue
            try:
                if gid in stale and str(row.get("status") or "") in {"active", "waiting", "paused"}:
                    self._rpc_raw("aria2.forceRemove", [gid])
                self._rpc_raw("aria2.removeDownloadResult", [gid])
            except Aria2Error:
                pass  # removed again on a later listing; its record goes now
        if gone:
            with self._metadata_transaction() as current:
                for gid in gone:
                    current.pop(gid, None)
            try:
                self._rpc_raw("aria2.saveSession")
            except Aria2Error:
                pass
            logging.getLogger(__name__).info(
                "STEP=aria2.metadata_pruned count=%d reason=finished_and_files_gone", len(gone)
            )
        return gone

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        del category
        statuses = self._all_statuses()
        metadata = self._load_metadata()
        loaded = copy.deepcopy(metadata)
        changed = False
        result: list[DownloadItem] = []
        # Finished downloads whose files are gone: their records (and aria2 rows) go.
        stale: set[str] = set()
        # First propagate ownership from magnet metadata downloads to their
        # followed payload GIDs. The metadata-only parent is not shown as a
        # second download in Activity.
        for parent in statuses:
            parent_gid = str(parent.get("gid") or "")
            parent_meta = metadata.get(parent_gid)
            if not parent_meta:
                continue
            for child_gid in parent.get("followedBy") or []:
                child_gid = str(child_gid or "")
                if child_gid and child_gid not in metadata:
                    metadata[child_gid] = dict(parent_meta)
                    changed = True

        for item in statuses:
            gid = str(item.get("gid") or "")
            self._hide_saved_metadata(item.get("dir"), item.get("infoHash"))
            if item.get("followedBy"):
                continue
            meta = metadata.get(gid)
            if not meta:
                parent_gid = str(item.get("following") or "")
                meta = metadata.get(parent_gid)
                if meta:
                    metadata[gid] = dict(meta)
                    changed = True
            if not meta:
                continue
            video_names = self._video_file_names(item)
            if video_names:
                video_episodes = sorted(
                    {number for number in map(parsed_release_episode, video_names) if number is not None}
                )
                if (
                    meta.get("video_count") != len(video_names)
                    or meta.get("video_episodes") != video_episodes
                ):
                    # Remembered for the orphan rows: aria2 may forget the task later.
                    meta["video_count"] = len(video_names)
                    meta["video_episodes"] = video_episodes
                    metadata[gid] = meta
                    changed = True
            is_batch = self._batch_flag(meta, title_fallback=False)
            total = int(item.get("totalLength") or 0)
            completed = int(item.get("completedLength") or 0)
            progress = (completed / total) if total > 0 else 0.0
            status = str(item.get("status") or "unknown")
            verified = max(0, int(item.get("verifiedLength") or 0))
            verifying = str(item.get("verifyIntegrityPending") or "false").casefold() == "true" or (
                status in {"active", "waiting"} and total > 0 and completed >= total and 0 < verified < total
            )
            # Downloads created by older Pudge builds may still carry a long
            # per-task seed-time even after the user switched seeding off. Do
            # not changeOption() here: aria2 restarts active downloads for most
            # option changes. Once all pieces are present, pausing the seeder is
            # enough to guarantee zero torrent upload without touching files.
            legacy_seeding_stopped = False
            if (
                self.seed_mode == "off"
                and status == "active"
                and total > 0
                and completed >= total
                and not verifying
            ):
                try:
                    self._rpc_raw("aria2.forcePause", [gid])
                    status = "paused"
                    legacy_seeding_stopped = True
                except Aria2Error:
                    pass

            completed_without_seeding = bool(
                self.seed_mode == "off"
                and status == "paused"
                and total > 0
                and completed >= total
                and not verifying
            )
            public_status = (
                "verifying"
                if verifying and status in {"active", "waiting"}
                else "complete"
                if completed_without_seeding
                else status
            )
            if public_status == "complete" and not int(meta.get("completed_on") or 0):
                meta["completed_on"] = int(time.time())
                metadata[gid] = meta
                changed = True
            if (
                public_status == "complete"
                # Finished a while ago: files moved or renamed right after finishing (an
                # import, a rename) are not mistaken for a deleted download.
                and 0 < int(meta.get("completed_on") or 0) < time.time() - _PRUNE_AFTER_SECONDS
                and not verifying
                and self._live_payload_gone(item)
            ):
                stale.add(gid)
                continue
            info_hash = str(item.get("infoHash") or meta.get("info_hash") or "").lower()
            public_hash = info_hash or gid
            raw = {
                "backend": "aria2",
                "gid": gid,
                "infohash_v1": info_hash,
                "total_size": total,
                "downloaded": completed,
                "download_speed": int(item.get("downloadSpeed") or 0),
                "upload_speed": int(item.get("uploadSpeed") or 0),
                "num_seeders": int(item.get("numSeeders") or 0),
                "num_connections": int(item.get("connections") or 0),
                "listed_seeders": max(0, int(meta.get("listed_seeders") or 0)),
                "listed_leechers": max(0, int(meta.get("listed_leechers") or 0)),
                "seeder": str(item.get("seeder") or "false").casefold() == "true",
                "error_code": str(item.get("errorCode") or ""),
                "error_message": str(item.get("errorMessage") or ""),
                "recoverable_missing_control": self._missing_control_file_error(item),
                "legacy_seeding_stopped": legacy_seeding_stopped,
                "aria2_status": status,
                "recovery_started_at": int(meta.get("recovery_started_at") or 0),
                "verifying": verifying,
                "verified": verified,
                "verification_progress": (max(0.0, min(1.0, verified / total)) if total > 0 else 0.0),
                "_media_id_source": "aria2_metadata",
                "_release_score_tag": float(meta.get("release_score") or 0.0),
                "_anime_title_tag": str(meta.get("anime_title") or ""),
                "video_episodes": list(meta.get("video_episodes") or []),
                "single_video_file": bool(meta.get("is_batch")) and not is_batch,
            }
            result.append(
                DownloadItem(
                    torrent_hash=public_hash,
                    name=str(
                        (((item.get("bittorrent") or {}).get("info") or {}).get("name"))
                        or meta.get("title")
                        or public_hash
                    ),
                    state=public_status,
                    progress=max(0.0, min(1.0, progress)),
                    save_path=str(item.get("dir") or meta.get("save_path") or ""),
                    content_path=self._content_path(item, meta),
                    media_id=int(meta["media_id"]) if meta.get("media_id") is not None else None,
                    episode=int(meta["episode"]) if meta.get("episode") is not None else None,
                    media_episode=int(meta["episode"]) if meta.get("episode") is not None else None,
                    release_episode=(
                        int(meta["release_episode"])
                        if meta.get("release_episode") is not None
                        else parsed_release_episode(str(meta.get("title") or ""))
                    ),
                    is_batch=is_batch,
                    added_on=int(meta.get("added_on") or 0),
                    completed_on=int(meta.get("completed_on") or 0),
                    raw=raw,
                )
            )
        represented_hashes = {str(item.torrent_hash or "").strip().casefold() for item in result}
        represented_paths = {
            str(item.content_path or "").strip() for item in result if str(item.content_path or "").strip()
        }
        live = {str(item.get("gid") or ""): item for item in statuses if item.get("gid")}
        for gid, meta in metadata.items():
            info_hash = str(meta.get("info_hash") or "").strip().casefold()
            if gid in stale or (info_hash and info_hash in represented_hashes):
                continue
            orphan = self._orphaned_completed_item(str(gid), dict(meta))
            if (
                orphan is None
                and gid not in live
                and 0 < int(meta.get("completed_on") or 0) < time.time() - _PRUNE_AFTER_SECONDS
                and self._recorded_payload_gone(meta)
            ):
                stale.add(str(gid))
                continue
            if orphan is None or str(orphan.content_path) in represented_paths:
                continue
            result.append(orphan)
            represented_hashes.add(str(orphan.torrent_hash or "").strip().casefold())
            represented_paths.add(str(orphan.content_path))
        if stale:
            # A live record sharing a pruned hash (a magnet's parent) is no longer written back.
            for gid in self._forget_finished_gone(metadata, live, stale):
                metadata.pop(gid, None)
                loaded.pop(gid, None)
        if changed:
            # Only what this listing changed goes back: the records were read before the
            # RPC calls, and a release added (or a task deleted) meanwhile must stay so.
            with self._metadata_transaction() as current:
                for gid, meta in metadata.items():
                    before = loaded.get(gid)
                    if before == meta:
                        continue
                    if before is not None and gid not in current:
                        continue  # deleted meanwhile
                    delta = {
                        key: value
                        for key, value in meta.items()
                        if not isinstance(before, dict) or before.get(key) != value
                    }
                    current[gid] = {**(current.get(gid) or {}), **delta}
        return result

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        self._require_observable()
        item = self._rpc_raw("aria2.tellStatus", [self._resolve_gid(torrent_hash), ["files"]]) or {}
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(item.get("files") or []):
            if not isinstance(entry, dict):
                continue
            length = int(entry.get("length") or 0)
            completed = int(entry.get("completedLength") or 0)
            result.append(
                {
                    "index": index,
                    "name": str(entry.get("path") or ""),
                    "size": length,
                    "progress": (completed / length) if length else 0.0,
                    "priority": 1 if entry.get("selected") != "false" else 0,
                }
            )
        return result

    def set_file_priority(
        self,
        torrent_hash: str,
        file_indices: list[int],
        priority: int,
    ) -> None:
        """Select files through aria2's 1-based ``select-file`` option."""
        wanted = {int(index) for index in file_indices if int(index) >= 0}
        if not wanted:
            return
        rows = self.files(torrent_hash)
        selected = {int(row.get("index") or 0) for row in rows if int(row.get("priority") or 0) > 0}
        previous_selected = selected.copy()
        if int(priority) > 0:
            selected.update(wanted)
        else:
            selected.difference_update(wanted)
        if not selected:
            raise Aria2Error(err("aria2.keepOneFile"))
        # A resumed torrent may already be complete/stopped. aria2 rejects
        # changeOption for those GIDs, even when the selection is identical.
        if selected == previous_selected:
            return
        value = ",".join(str(index + 1) for index in sorted(selected))
        gid = self._resolve_gid(torrent_hash)
        self._rpc_raw("aria2.changeOption", [gid, {"select-file": value}])

    def _remove_result(self, gid: str) -> None:
        """Drop the stopped task; forceRemove only asks aria2 to stop it."""
        for delay in (0.05, 0.1, 0.2, 0.4, 0.8):
            try:
                self._rpc_raw("aria2.removeDownloadResult", [gid])
                return
            except Aria2Error:
                pass
            # Still stopping: its result stayed listed ("removed") and the
            # removal was never confirmed. Gone already: nothing to wait for.
            try:
                self._rpc_raw("aria2.tellStatus", [gid, ["status"]])
            except Aria2Error:
                return
            time.sleep(delay)

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self._require_observable()
        gid = self._resolve_gid(torrent_hash)
        if not gid:
            return  # no hash: nothing of Pudge's to remove (never another download)
        wanted = str(torrent_hash or "").strip().lower()
        status: dict[str, Any] = {}
        try:
            status = self._rpc_raw("aria2.tellStatus", [gid, ["status", "files", "dir", "following"]]) or {}
        except Aria2Error:
            pass
        try:
            self._rpc_raw("aria2.forceRemove", [gid])
        except Aria2Error as exc:
            detail = str(exc).casefold()
            if "not found" not in detail and "invalid gid" not in detail:
                raise
        self._remove_result(gid)
        metadata = self._load_metadata()
        # Files are deleted only inside the folder Pudge chose for this torrent
        # (its own record), never wherever the RPC answer says they are.
        owned_dirs = self._recorded_save_dirs(metadata, gid, wanted)
        # A magnet's finished [METADATA] task (the one this payload follows)
        # kept the hash's record: the hash still resolved to it after the
        # delete, so removing a race candidate never looked confirmed and the
        # race ended without a word (Sora yori mo Tooi Basho, 2026-10-15).
        parents = {str(status.get("following") or "")} | {
            key
            for key, meta in metadata.items()
            if key != gid and wanted and str((meta or {}).get("info_hash") or "").lower() == wanted
        }
        for parent in sorted(parents - {""}):
            try:
                self._rpc_raw("aria2.removeDownloadResult", [parent])
            except Aria2Error:
                pass
        with self._metadata_transaction() as current:
            for key in parents | {gid}:
                current.pop(key, None)
        try:
            self._rpc_raw("aria2.saveSession")
        except Aria2Error:
            pass
        if delete_files:
            for entry in status.get("files") or []:
                raw = str(entry.get("path") or "").strip()
                if not raw:
                    continue
                path = Path(raw).expanduser()
                if not self._inside_dirs(path, owned_dirs):
                    logging.getLogger(__name__).warning(
                        "SKIP step=aria2.delete_file reason=outside_download_dir path=%r dirs=%s",
                        raw,
                        [str(item) for item in owned_dirs],
                    )
                    continue
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                    control = Path(str(path) + ".aria2")
                    if not control.is_symlink():
                        control.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _recorded_save_dirs(metadata: dict[str, dict[str, Any]], gid: str, wanted: str) -> tuple[Path, ...]:
        """The download folders Pudge recorded when it added this torrent (metadata.json)."""
        dirs: list[Path] = []
        for key, meta in metadata.items():
            meta = meta or {}
            if key != gid and not (wanted and str(meta.get("info_hash") or "").lower() == wanted):
                continue
            raw = str(meta.get("save_path") or "").strip()
            if not raw:
                continue
            try:
                folder = Path(raw).expanduser().resolve()
            except OSError:
                continue
            # A record pointing at / or a home folder is no download folder.
            if folder == Path(folder.anchor) or folder == Path.home().resolve():
                continue
            dirs.append(folder)
        return tuple(dict.fromkeys(dirs))

    @staticmethod
    def _inside_dirs(path: Path, dirs: tuple[Path, ...]) -> bool:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError:
            return False
        return any(folder in resolved.parents for folder in dirs)

    def cleanup_tags(self) -> dict[str, int]:
        return {"score_tags_removed": 0, "unused_tags_deleted": 0}
