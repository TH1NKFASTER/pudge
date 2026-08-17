from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..branding import APP_SLUG, DATA_DIR
from ..manager_models import DownloadItem, NyaaRelease
from .qbittorrent import QBittorrentError


class Aria2Error(QBittorrentError):
    """aria2 failure exposed through the existing torrent error hierarchy."""


class Aria2Client:
    """Small managed aria2c sidecar controlled through JSON-RPC.

    The app owns only downloads recorded in ``metadata.json``. This keeps an
    independently running aria2 instance out of pudge's library cleanup.
    """

    _RUNTIME_PROFILE = "v4-detach-seed-only"
    _SAFE_RPC_TORRENT_BYTES = 1_250_000

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
        self.seed_mode = normalized_seed_mode if normalized_seed_mode in {
            "off", "ratio", "ratio_or_time", "unlimited"
        } else "off"
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

    def _load_secret(self) -> str:
        path = self.state_dir / "rpc-secret"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if not value:
            value = secrets.token_urlsafe(32)
            path.write_text(value + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return value

    def _binary_path(self) -> str:
        env = os.getenv("PUDGE_ARIA2C", "").strip()
        candidates = [env, self.binary, "/opt/homebrew/bin/aria2c", "/usr/local/bin/aria2c"]
        for candidate in candidates:
            if not candidate:
                continue
            found = shutil.which(candidate) if "/" not in candidate else candidate
            if found and Path(found).is_file():
                return str(found)
        raise Aria2Error(
            "Встроенный torrent backend не найден: aria2c не установлен. "
            "Повторно запусти install.sh или выполни: brew install aria2"
        )

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
        path = self._metadata_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _run_hook(self) -> None:
        if not self.pre_download_command:
            return
        completed = subprocess.run(
            self.pre_download_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise Aria2Error(f"Команда VPN перед скачиванием завершилась с ошибкой: {detail}")

    def _rpc_raw(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": f"{APP_SLUG}-{time.time_ns()}",
            "method": method if method.startswith("aria2.") or method.startswith("system.") else f"aria2.{method}",
            "params": [f"token:{self._secret}", *(params or [])],
        }
        try:
            response = self._http.post(self._rpc_url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            detail = str(exc.response.text or "").strip().replace("\n", " ")[:500]
            suffix = f"; aria2 response: {detail}" if detail else ""
            raise Aria2Error(f"aria2 RPC недоступен: {exc}{suffix}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise Aria2Error(f"aria2 RPC недоступен: {exc}") from exc
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise Aria2Error(f"aria2 RPC: {message}")
        return body.get("result") if isinstance(body, dict) else None

    def _probe(self) -> bool:
        try:
            self._rpc_raw("aria2.getVersion")
            return True
        except Aria2Error:
            return False

    def _interface_names(self) -> set[str]:
        try:
            return {str(name) for _index, name in socket.if_nameindex()}
        except OSError:
            return set()

    def _validate_network_guard(self) -> None:
        if self.vpn_kill_switch and not self.vpn_interface:
            raise Aria2Error(
                "Kill switch включён, но VPN interface не указан. "
                "Укажи активный интерфейс VPN (обычно utun…)"
            )
        if self.vpn_interface and self.vpn_interface not in self._interface_names():
            raise Aria2Error(
                f"VPN interface {self.vpn_interface!r} сейчас недоступен; "
                "torrent traffic заблокирован"
            )

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
            "max-upload-limit": (
                f"{self.upload_limit_kib}K" if self.upload_limit_kib else "0"
            ),
        }

    def _apply_runtime_options(self) -> None:
        options = self._runtime_options()
        try:
            current_global = self._rpc_raw("aria2.getGlobalOption") or {}
        except Aria2Error:
            current_global = {}
        global_delta = {
            key: value
            for key, value in options.items()
            if str(current_global.get(key, "")) != str(value)
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
        values = ["--no-conf=true", "--bt-detach-seed-only=true"]
        values.extend(f"--{key}={value}" for key, value in self._seed_options().items())
        if self.upload_limit_kib:
            values.append(f"--max-upload-limit={self.upload_limit_kib}K")
        if self.vpn_interface:
            values.append(f"--interface={self.vpn_interface}")
        return values

    def _start(self) -> None:
        if not self.enabled:
            raise Aria2Error("Встроенный aria2 backend отключён")
        self._validate_network_guard()
        binary = self._binary_path()
        session = self.state_dir / "session.txt"
        session.touch(exist_ok=True)
        log = self.state_dir / "aria2.log"
        cmd = [
            binary,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.rpc_port}",
            f"--rpc-secret={self._secret}",
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
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise Aria2Error(f"Не удалось запустить aria2c: {exc}") from exc
        for delay in (0.15, 0.25, 0.4, 0.7, 1.0, 1.5):
            time.sleep(delay)
            if self._probe():
                self._apply_runtime_options()
                try:
                    self._runtime_profile_path().write_text(
                        self._RUNTIME_PROFILE + "\n", encoding="utf-8"
                    )
                except OSError:
                    pass
                return
        raise Aria2Error(f"aria2c запущен, но RPC не ответил на порту {self.rpc_port}")

    def ensure_running(self) -> None:
        with self._lock:
            self._validate_network_guard()
            if self._probe():
                try:
                    runtime_profile = self._runtime_profile_path().read_text(
                        encoding="utf-8"
                    ).strip()
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
            if not self.auto_start:
                raise Aria2Error("aria2 RPC не запущен")
            self._start()

    def version(self) -> str:
        self.ensure_running()
        result = self._rpc_raw("aria2.getVersion")
        return str((result or {}).get("version") or "unknown")

    @staticmethod
    def _safe_gid(info_hash: str, magnet: str) -> str:
        import hashlib

        source = (info_hash or hashlib.sha1(magnet.encode("utf-8")).hexdigest()).lower()
        return source[:16].ljust(16, "0")

    def _all_statuses(self) -> list[dict[str, Any]]:
        self.ensure_running()
        keys = [
            "gid", "status", "totalLength", "completedLength", "downloadSpeed",
            "uploadSpeed", "connections", "numSeeders", "seeder",
            "dir", "files", "bittorrent", "infoHash", "errorCode", "errorMessage",
            "followedBy", "following", "verifiedLength", "verifyIntegrityPending",
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
                    response = client.get(source)
                    response.raise_for_status()
                    payload = bytes(response.content)
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
            raise Aria2Error("Релиз не содержит ни magnet-ссылки, ни torrent URL")
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
    ) -> None:
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
                    return
                except Aria2Error:
                    pass
        gid = self._safe_gid(release.info_hash, release.magnet)
        options = {
            "dir": str(target),
            "pause": "true" if (paused or self.paused_on_add) else "false",
            "gid": gid,
            "bt-save-metadata": "true",
            "bt-load-saved-metadata": "true",
            "check-integrity": "false",
            **self._seed_options(),
        }
        try:
            returned_gid = self._add_source(release, options)
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
        metadata[returned_gid] = {
            "info_hash": wanted_hash,
            "title": release.title,
            "save_path": str(target),
            "media_id": media_id,
            "episode": episode,
            "is_batch": bool(is_batch),
            "anime_title": anime_title,
            "release_score": float(score),
            "source_url": str(release.torrent_url or release.link or ""),
            "magnet": self._richest_magnet(release.magnet, release.torrent_url),
            "listed_seeders": max(0, int(release.seeders or 0)),
            "listed_leechers": max(0, int(release.leechers or 0)),
            "added_on": int(time.time()),
            "completed_on": 0,
        }
        self._save_metadata(metadata)

    def start(self, torrent_hash: str) -> None:
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = self._rpc_raw("aria2.tellStatus", [gid, ["status"]]) or {}
        if str(status.get("status") or "").casefold() != "paused":
            return
        self._rpc_raw("aria2.unpause", [gid])

    def pause(self, torrent_hash: str) -> None:
        self.ensure_running()
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
            if (
                len(info_hash) in {32, 40}
                and info_hash.isalnum()
            ):
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
            returned_gid = str(
                self._rpc_raw("aria2.addTorrent", [encoded, [], options]) or gid
            )
        else:
            returned_gid = str(
                self._rpc_raw("aria2.addUri", [[magnet], options]) or gid
            )
        metadata.pop(gid, None)
        meta["recovery_started_at"] = int(time.time())
        metadata[returned_gid] = meta
        self._save_metadata(metadata)
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

        returned_gid = str(
            self._rpc_raw("aria2.addUri", [[replacement], options]) or gid
        )
        metadata.pop(gid, None)
        meta["magnet"] = replacement
        meta["recovery_started_at"] = int(time.time())
        metadata[returned_gid] = meta
        self._save_metadata(metadata)
        try:
            self._rpc_raw("aria2.saveSession")
        except Aria2Error:
            pass
        return True

    def reconnect(self, torrent_hash: str) -> bool:
        """Reannounce a stall or safely reconstruct a lost BitTorrent control file."""

        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status = self._rpc_raw(
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
        ) or {}
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


    def torrent_status(self, torrent_hash: str) -> dict[str, Any]:
        """Return the small progress surface used by the common release racer."""
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        item = self._rpc_raw(
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
        ) or {}
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
        status = self._rpc_raw(
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
        ) or {}
        if self._missing_control_file_error(status):
            return self._recover_missing_control_file(
                gid, status, torrent_payload=payload
            )
        files = status.get("files") if isinstance(status.get("files"), list) else []
        has_data = any(
            int(entry.get("length") or 0) > 0
            for entry in files
            if isinstance(entry, dict)
        )
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
            "pause": (
                "true"
                if str(status.get("status") or "").casefold() == "paused"
                else "false"
            ),
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
        metadata.pop(gid, None)
        meta.update(
            {
                "info_hash": str(release.info_hash or meta.get("info_hash") or "").lower(),
                "title": release.title or str(meta.get("title") or ""),
                "source_url": str(release.torrent_url or release.link or ""),
                "magnet": str(release.magnet or meta.get("magnet") or ""),
                "listed_seeders": max(0, int(release.seeders or 0)),
                "listed_leechers": max(0, int(release.leechers or 0)),
            }
        )
        metadata[returned_gid] = meta
        self._save_metadata(metadata)
        return True

    @staticmethod
    def _content_path(item: dict[str, Any], meta: dict[str, Any]) -> str:
        files = item.get("files") if isinstance(item.get("files"), list) else []
        paths = [str(entry.get("path") or "") for entry in files if isinstance(entry, dict) and entry.get("path")]
        if len(paths) == 1:
            return paths[0]
        directory = str(item.get("dir") or meta.get("save_path") or "")
        name = str(((item.get("bittorrent") or {}).get("info") or {}).get("name") or "")
        return str(Path(directory) / name) if directory and name else directory

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        del category
        statuses = self._all_statuses()
        metadata = self._load_metadata()
        changed = False
        result: list[DownloadItem] = []
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
            total = int(item.get("totalLength") or 0)
            completed = int(item.get("completedLength") or 0)
            progress = (completed / total) if total > 0 else 0.0
            status = str(item.get("status") or "unknown")
            verified = max(0, int(item.get("verifiedLength") or 0))
            verifying = (
                str(item.get("verifyIntegrityPending") or "false").casefold() == "true"
                or (
                    status in {"active", "waiting"}
                    and total > 0
                    and completed >= total
                    and 0 < verified < total
                )
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
                "verification_progress": (
                    max(0.0, min(1.0, verified / total)) if total > 0 else 0.0
                ),
                "_media_id_source": "aria2_metadata",
                "_release_score_tag": float(meta.get("release_score") or 0.0),
                "_anime_title_tag": str(meta.get("anime_title") or ""),
            }
            result.append(
                DownloadItem(
                    torrent_hash=public_hash,
                    name=str((((item.get("bittorrent") or {}).get("info") or {}).get("name")) or meta.get("title") or public_hash),
                    state=public_status,
                    progress=max(0.0, min(1.0, progress)),
                    save_path=str(item.get("dir") or meta.get("save_path") or ""),
                    content_path=self._content_path(item, meta),
                    media_id=int(meta["media_id"]) if meta.get("media_id") is not None else None,
                    episode=int(meta["episode"]) if meta.get("episode") is not None else None,
                    is_batch=bool(meta.get("is_batch")),
                    added_on=int(meta.get("added_on") or 0),
                    completed_on=int(meta.get("completed_on") or 0),
                    raw=raw,
                )
            )
        if changed:
            self._save_metadata(metadata)
        return result

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        self.ensure_running()
        item = self._rpc_raw("aria2.tellStatus", [self._resolve_gid(torrent_hash), ["files"]]) or {}
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(item.get("files") or []):
            if not isinstance(entry, dict):
                continue
            length = int(entry.get("length") or 0)
            completed = int(entry.get("completedLength") or 0)
            result.append({
                "index": index,
                "name": str(entry.get("path") or ""),
                "size": length,
                "progress": (completed / length) if length else 0.0,
                "priority": 1 if entry.get("selected") != "false" else 0,
            })
        return result

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.ensure_running()
        gid = self._resolve_gid(torrent_hash)
        status: dict[str, Any] = {}
        try:
            status = self._rpc_raw("aria2.tellStatus", [gid, ["status", "files", "dir"]]) or {}
        except Aria2Error:
            pass
        try:
            self._rpc_raw("aria2.forceRemove", [gid])
        except Aria2Error as exc:
            detail = str(exc).casefold()
            if "not found" not in detail and "invalid gid" not in detail:
                raise
        try:
            self._rpc_raw("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            pass
        metadata = self._load_metadata()
        metadata.pop(gid, None)
        self._save_metadata(metadata)
        try:
            self._rpc_raw("aria2.saveSession")
        except Aria2Error:
            pass
        if delete_files:
            for entry in status.get("files") or []:
                path = Path(str(entry.get("path") or "")).expanduser()
                try:
                    if path.is_file():
                        path.unlink()
                    Path(str(path) + ".aria2").unlink(missing_ok=True)
                except OSError:
                    pass

    def cleanup_tags(self) -> dict[str, int]:
        return {"score_tags_removed": 0, "unused_tags_deleted": 0}
