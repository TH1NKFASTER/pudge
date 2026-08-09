from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..manager_models import DownloadItem, NyaaRelease
from ..branding import APP_SLUG, DATA_DIR
from .qbittorrent import QBittorrentError


class Aria2Error(QBittorrentError):
    """aria2 failure exposed through the existing torrent error hierarchy."""


class Aria2Client:
    """Small managed aria2c sidecar controlled through JSON-RPC.

    The app owns only downloads recorded in ``metadata.json``. This keeps an
    independently running aria2 instance out of Anime MPV's library cleanup.
    """

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
        timeout: float = 10.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.binary = binary.strip() or "aria2c"
        self.rpc_port = max(1024, min(65535, int(rpc_port)))
        self.state_dir = (state_dir or (DATA_DIR / "aria2")).expanduser()
        self.pre_download_command = pre_download_command.strip()
        self.paused_on_add = bool(paused_on_add)
        self.auto_start = bool(auto_start)
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
        env = os.getenv("ANIME_MPV_ARIA2C", "").strip()
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

    def _start(self) -> None:
        if not self.enabled:
            raise Aria2Error("Встроенный aria2 backend отключён")
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
            "--rpc-save-upload-metadata=true",
            "--continue=true",
            "--check-integrity=true",
            "--auto-file-renaming=false",
            "--max-concurrent-downloads=3",
            "--bt-save-metadata=true",
            "--bt-load-saved-metadata=true",
            "--bt-prioritize-piece=head=16M,tail=4M",
            "--seed-time=0",
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
                return
        raise Aria2Error(f"aria2c запущен, но RPC не ответил на порту {self.rpc_port}")

    def ensure_running(self) -> None:
        with self._lock:
            if self._probe():
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
            "dir", "files", "bittorrent", "infoHash", "errorCode", "errorMessage",
            "followedBy", "following",
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
            "seed-time": "0",
        }
        try:
            returned_gid = str(self._rpc_raw("aria2.addUri", [[release.magnet], options]) or gid)
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
            "added_on": int(time.time()),
            "completed_on": 0,
        }
        self._save_metadata(metadata)

    def start(self, torrent_hash: str) -> None:
        self.ensure_running()
        self._rpc_raw("aria2.unpause", [self._resolve_gid(torrent_hash)])

    def pause(self, torrent_hash: str) -> None:
        self.ensure_running()
        self._rpc_raw("aria2.pause", [self._resolve_gid(torrent_hash)])

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
            if status == "complete" and not int(meta.get("completed_on") or 0):
                meta["completed_on"] = int(time.time())
                metadata[gid] = meta
                changed = True
            info_hash = str(item.get("infoHash") or meta.get("info_hash") or "").lower()
            public_hash = info_hash or gid
            raw = {
                "backend": "aria2",
                "gid": gid,
                "infohash_v1": info_hash,
                "download_speed": int(item.get("downloadSpeed") or 0),
                "error_code": str(item.get("errorCode") or ""),
                "error_message": str(item.get("errorMessage") or ""),
                "_media_id_source": "aria2_metadata",
                "_release_score_tag": float(meta.get("release_score") or 0.0),
                "_anime_title_tag": str(meta.get("anime_title") or ""),
            }
            result.append(
                DownloadItem(
                    torrent_hash=public_hash,
                    name=str((((item.get("bittorrent") or {}).get("info") or {}).get("name")) or meta.get("title") or public_hash),
                    state=status,
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
            if "not found" not in str(exc).lower():
                raise
        try:
            self._rpc_raw("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            pass
        metadata = self._load_metadata()
        metadata.pop(gid, None)
        self._save_metadata(metadata)
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
