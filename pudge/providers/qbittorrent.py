from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..branding import APP_NAME, APP_SLUG, LEGACY_APP_SLUGS
from ..manager_models import DownloadItem, NyaaRelease


class QBittorrentError(RuntimeError):
    pass


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        api_key: str = "",
        *,
        verify_tls: bool = True,
        timeout: float = 20.0,
        pre_download_command: str = "",
        auto_start_app: bool = True,
    ) -> None:
        self.base_url = self._normalize_url(base_url or "http://127.0.0.1:8080")
        self.username = username
        self.password = password
        self.api_key = api_key.strip()
        self.pre_download_command = pre_download_command.strip()
        self.auto_start_app = auto_start_app
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.client = self._new_client(self.base_url)
        self._authenticated = bool(self.api_key)
        self._version: str | None = None

    @property
    def backend_name(self) -> str:
        return "qbittorrent"

    @staticmethod
    def _normalize_url(value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return "http://127.0.0.1:8080"
        if "://" not in value:
            value = f"http://{value}"
        return value

    def _headers(self, base_url: str) -> dict[str, str]:
        headers = {"Referer": base_url, "Origin": base_url, "User-Agent": APP_SLUG}
        # qBittorrent >= 5.2 authenticates API keys as an HTTP Bearer token.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _new_client(self, base_url: str) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            timeout=self.timeout,
            follow_redirects=True,
            verify=self.verify_tls,
            headers=self._headers(base_url),
        )

    def _switch_base_url(self, base_url: str) -> None:
        base_url = self._normalize_url(base_url)
        if base_url == self.base_url:
            return
        self.client.close()
        self.base_url = base_url
        self.client = self._new_client(base_url)
        self._authenticated = bool(self.api_key)
        self._version = None

    def close(self) -> None:
        self.client.close()

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
            raise QBittorrentError(f"Команда VPN перед скачиванием завершилась с ошибкой: {detail}")

    @staticmethod
    def _mac_config_paths() -> tuple[Path, ...]:
        home = Path.home()
        return (
            home / "Library" / "Preferences" / "qBittorrent" / "qBittorrent.ini",
            home / "Library" / "Application Support" / "qBittorrent" / "qBittorrent.ini",
            home / ".config" / "qBittorrent" / "qBittorrent.conf",
        )

    def _config_candidate_urls(self) -> list[str]:
        result: list[str] = []
        for path in self._mac_config_paths():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            port_match = re.search(r"(?m)^WebUI\\Port=(\d+)\s*$", text)
            if not port_match:
                continue
            port = int(port_match.group(1))
            https = bool(re.search(r"(?m)^WebUI\\HTTPS\\Enabled=true\s*$", text, re.I))
            scheme = "https" if https else "http"
            result.extend((f"{scheme}://127.0.0.1:{port}", f"{scheme}://localhost:{port}"))
        return result

    @staticmethod
    def _lsof_candidate_urls() -> list[str]:
        try:
            completed = subprocess.run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        result: list[str] = []
        for line in completed.stdout.splitlines():
            if "qbittorrent" not in line.casefold():
                continue
            match = re.search(r"TCP\s+\S+:(\d+)\s+\(LISTEN\)", line)
            if match:
                result.append(f"http://127.0.0.1:{int(match.group(1))}")
        return result

    def _candidate_urls(self) -> list[str]:
        parsed = urlparse(self.base_url)
        candidates = [self.base_url]
        candidates.extend(self._config_candidate_urls())
        candidates.extend(self._lsof_candidate_urls())
        if (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost", "::1"}:
            for port in (8080, 8081, 8082, 8090, 8091, 8999):
                candidates.extend((f"http://127.0.0.1:{port}", f"http://localhost:{port}"))
        return list(dict.fromkeys(self._normalize_url(value) for value in candidates))

    def _probe(self, base_url: str) -> tuple[bool, str]:
        try:
            with self._new_client(base_url) as client:
                response = client.get("/api/v2/app/version", timeout=min(self.timeout, 2.5))
        except httpx.HTTPError:
            return False, ""
        # 403 means qBittorrent is reachable but authentication was rejected.
        if response.status_code == 403:
            return True, ""
        if response.status_code == 200 and response.text.strip().lower().startswith("v"):
            return True, response.text.strip()
        return False, ""

    def _discover_local_webui(self) -> str | None:
        for candidate in self._candidate_urls():
            reachable, _version = self._probe(candidate)
            if reachable:
                return candidate
        return None

    def _recover_connection(self) -> bool:
        if self.auto_start_app:
            subprocess.run(["open", "-gja", "qBittorrent"], check=False)
            # qBittorrent can take several seconds to expose WebUI after launch.
            for delay in (1.0, 1.5, 2.5, 3.0):
                time.sleep(delay)
                detected = self._discover_local_webui()
                if detected:
                    self._switch_base_url(detected)
                    return True
        detected = self._discover_local_webui()
        if detected:
            self._switch_base_url(detected)
            return True
        return False

    def _connection_error(self, exc: Exception) -> QBittorrentError:
        return QBittorrentError(
            "qBittorrent установлен, но Web UI API не отвечает по адресу "
            f"{self.base_url}. Откройте qBittorrent → Settings → Web UI, включите "
            f"«Web User Interface (Remote control)» и укажите в {APP_NAME} тот же порт. "
            "API key отвечает только за авторизацию и сам Web UI не запускает. "
            f"Техническая ошибка: {exc}"
        )

    def login(self) -> None:
        if self._authenticated:
            return
        try:
            response = self.client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
        except httpx.ConnectError as exc:
            if not self._recover_connection():
                raise self._connection_error(exc) from exc
            try:
                response = self.client.post(
                    "/api/v2/auth/login",
                    data={"username": self.username, "password": self.password},
                )
            except httpx.HTTPError as second_exc:
                raise self._connection_error(second_exc) from second_exc
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"qBittorrent Web API недоступен: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QBittorrentError(f"qBittorrent не принял логин: HTTP {response.status_code}") from exc
        if response.text.strip() != "Ok.":
            raise QBittorrentError(f"qBittorrent не принял логин: {response.text.strip()}")
        self._authenticated = True

    def version(self) -> str:
        if self._version is not None:
            return self._version
        self.login()
        try:
            response = self.client.get("/api/v2/app/version")
        except httpx.ConnectError as exc:
            if not self._recover_connection():
                raise self._connection_error(exc) from exc
            try:
                response = self.client.get("/api/v2/app/version")
            except httpx.HTTPError as second_exc:
                raise self._connection_error(second_exc) from second_exc
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось получить версию qBittorrent: {exc}") from exc
        if response.status_code == 403 and self.api_key:
            raise QBittorrentError(
                "qBittorrent найден, но API key отклонён. Проверьте, что ключ создан в "
                "qBittorrent 5.2 → Settings → Web UI → API keys и вставлен полностью."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QBittorrentError(
                f"Не удалось получить версию qBittorrent: HTTP {response.status_code}"
            ) from exc
        self._version = response.text.strip()
        return self._version

    @staticmethod
    def _version_at_least(value: str, minimum: tuple[int, int]) -> bool:
        numbers = [int(part) for part in re.findall(r"\d+", value)[:2]]
        if len(numbers) < 2:
            return False
        return (numbers[0], numbers[1]) >= minimum

    def _add_state_payload(self, *, paused: bool, stop_at_metadata: bool) -> dict[str, str]:
        # An API key can only be configured in qBittorrent 5.2+, so it also
        # tells us which add-torrent parameter names the server expects.
        modern = bool(self.api_key) or self._version_at_least(self.version(), (5, 2))
        stopped = paused and not stop_at_metadata
        if modern:
            return {"stopped": str(stopped).lower(), "contentLayout": "Original"}
        return {"paused": str(stopped).lower()}

    def categories(self) -> dict[str, dict[str, Any]]:
        self.login()
        try:
            response = self.client.get("/api/v2/torrents/categories")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(f"Не удалось получить категории qBittorrent: {exc}") from exc
        return dict(payload) if isinstance(payload, dict) else {}

    def tags(self) -> set[str]:
        self.login()
        try:
            response = self.client.get("/api/v2/torrents/tags")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(f"Не удалось получить теги qBittorrent: {exc}") from exc
        if not isinstance(payload, list):
            return set()
        return {str(value).strip() for value in payload if str(value).strip()}

    def remove_tags(self, torrent_hash: str, tags: list[str] | set[str]) -> None:
        clean = [str(tag).strip() for tag in tags if str(tag).strip()]
        if not torrent_hash.strip() or not clean:
            return
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/removeTags",
                data={"hashes": torrent_hash.strip(), "tags": ",".join(clean)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось снять теги qBittorrent: {exc}") from exc

    def delete_tags(self, tags: list[str] | set[str]) -> int:
        clean = [str(tag).strip() for tag in tags if str(tag).strip()]
        if not clean:
            return 0
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/deleteTags",
                data={"tags": ",".join(clean)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось удалить теги qBittorrent: {exc}") from exc
        return len(clean)

    def cleanup_tags(self) -> dict[str, int]:
        """Remove obsolete score tags and every globally unused qBittorrent tag."""
        items = self.torrents(category="")
        removed_score = 0
        used: set[str] = set()
        for item in items:
            item_tags = {
                str(tag).strip()
                for tag in item.raw.get("_tag_set", [])
                if str(tag).strip()
            }
            score_tags = {
                tag for tag in item_tags if tag.casefold().startswith("score:")
            }
            if score_tags:
                self.remove_tags(item.torrent_hash, score_tags)
                removed_score += len(score_tags)
                item_tags -= score_tags
            used.update(item_tags)
        unused = self.tags() - used
        deleted_unused = self.delete_tags(unused)
        return {
            "score_tags_removed": removed_score,
            "unused_tags_deleted": deleted_unused,
        }

    def ensure_category(self, category: str, save_path: Path) -> None:
        category = category.strip()
        if not category:
            return
        categories = self.categories()
        if category in categories:
            return
        try:
            response = self.client.post(
                "/api/v2/torrents/createCategory",
                data={
                    "category": category,
                    "savePath": str(save_path.expanduser()),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(
                f"Не удалось создать категорию qBittorrent '{category}': {exc}"
            ) from exc

    def _torrent_by_hash(self, info_hash: str) -> dict[str, Any] | None:
        value = info_hash.strip().lower()
        if not value:
            return None
        try:
            response = self.client.get(
                "/api/v2/torrents/info",
                params={"hashes": value},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            hashes = {
                str(item.get("hash") or "").lower(),
                str(item.get("infohash_v1") or "").lower(),
                str(item.get("infohash_v2") or "").lower(),
            }
            if value in hashes:
                return dict(item)
        return None

    def torrent_status(self, info_hash: str) -> dict[str, Any] | None:
        """Return fresh qBittorrent stats for one torrent hash."""
        self.login()
        return self._torrent_by_hash(info_hash)

    def _set_category_and_tags(
        self,
        torrent_hash: str,
        *,
        category: str,
        tags: list[str],
    ) -> None:
        torrent_hash = torrent_hash.strip()
        if not torrent_hash:
            return
        try:
            if category.strip():
                response = self.client.post(
                    "/api/v2/torrents/setCategory",
                    data={"hashes": torrent_hash, "category": category.strip()},
                )
                response.raise_for_status()
            clean_tags = [tag.strip() for tag in tags if tag.strip()]
            existing = self._torrent_by_hash(torrent_hash) or {}
            old_tags = [
                tag.strip()
                for tag in str(existing.get("tags") or "").split(",")
                if tag.strip()
            ]
            legacy_app_tags = {value.casefold() for value in LEGACY_APP_SLUGS}
            obsolete = [
                tag
                for tag in old_tags
                if tag.casefold().startswith((
                    "anilist-", "anilist:", "episode-", "anime:", "episode:", "score:"
                ))
                or tag.casefold() in {"batch", "series pack"}
                or tag.casefold() in legacy_app_tags
                or tag.casefold() == APP_SLUG.casefold()
            ]
            if obsolete:
                response = self.client.post(
                    "/api/v2/torrents/removeTags",
                    data={"hashes": torrent_hash, "tags": ",".join(obsolete)},
                )
                response.raise_for_status()
                legacy_ids = [tag for tag in obsolete if tag.casefold().startswith("anilist-")]
                if legacy_ids:
                    # Old numeric AniList tags were created by pudge and are safe
                    # to remove from qBittorrent's global tag list.
                    response = self.client.post(
                        "/api/v2/torrents/deleteTags",
                        data={"tags": ",".join(legacy_ids)},
                    )
                    response.raise_for_status()
            if clean_tags:
                # createTags is idempotent in qBittorrent. Existing tags are ignored.
                response = self.client.post(
                    "/api/v2/torrents/createTags",
                    data={"tags": ",".join(clean_tags)},
                )
                response.raise_for_status()
                response = self.client.post(
                    "/api/v2/torrents/addTags",
                    data={"hashes": torrent_hash, "tags": ",".join(clean_tags)},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(
                f"Торрент добавлен, но не удалось назначить категорию/теги: {exc}"
            ) from exc

    def set_metadata(
        self,
        torrent_hash: str,
        *,
        category: str,
        tags: list[str],
    ) -> None:
        """Attach pudge metadata to an existing qBittorrent item."""
        self.login()
        self._set_category_and_tags(torrent_hash, category=category, tags=tags)

    def set_location(self, torrent_hash: str, location: Path) -> None:
        """Move qBittorrent's save location without touching the torrent files."""
        value = str(torrent_hash or "").strip()
        if not value:
            return
        target = location.expanduser()
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/setLocation",
                data={"hashes": value, "location": str(target)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(
                f"Не удалось обновить путь торрента qBittorrent: {exc}"
            ) from exc

    def recheck(self, torrent_hash: str) -> None:
        value = str(torrent_hash or "").strip()
        if not value:
            return
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/recheck",
                data={"hashes": value},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось перепроверить торрент qBittorrent: {exc}") from exc

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
        self._run_hook()
        self.login()
        target = save_path.expanduser()
        target.mkdir(parents=True, exist_ok=True)
        self.ensure_category(category, target)

        # qBittorrent 5.2 returns 409 when all supplied torrents failed to add.
        # A duplicate torrent is the most common case, so make this operation idempotent.
        existing = self._torrent_by_hash(release.info_hash)
        if existing is not None:
            torrent_hash = str(existing.get("hash") or release.info_hash)
            self._set_category_and_tags(torrent_hash, category=category, tags=tags)
            return torrent_hash

        # qBittorrent 5.2 renamed `paused` to `stopped` and `root_folder` to
        # `contentLayout`. URL/magnet additions do not require multipart upload,
        # so use a regular form request with version-compatible field names.
        payload = {
            "urls": release.magnet,
            "savepath": str(target),
            **self._add_state_payload(paused=paused, stop_at_metadata=stop_at_metadata),
        }
        if stop_at_metadata:
            payload["stopCondition"] = "MetadataReceived"
        try:
            response = self.client.post("/api/v2/torrents/add", data=payload)
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось добавить торрент: {exc}") from exc

        if response.status_code == 409:
            # qBittorrent may report Conflict for an already present torrent.
            # Re-check after the add attempt because magnet addition can race with UI refresh.
            for delay in (0.0, 0.2, 0.6):
                if delay:
                    time.sleep(delay)
                existing = self._torrent_by_hash(release.info_hash)
                if existing is not None:
                    torrent_hash = str(existing.get("hash") or release.info_hash)
                    self._set_category_and_tags(torrent_hash, category=category, tags=tags)
                    return torrent_hash
            detail = response.text.strip()
            suffix = f" Ответ qBittorrent: {detail}" if detail and detail != "Conflict" else ""
            raise QBittorrentError(
                "qBittorrent не добавил торрент (409 Conflict). Торрент не найден среди "
                "существующих; возможны некорректная magnet-ссылка или недоступная папка "
                f"загрузки: {target}.{suffix}"
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            raise QBittorrentError(
                f"Не удалось добавить торрент: HTTP {response.status_code}"
                + (f" — {detail}" if detail else "")
            ) from exc

        torrent_ids: list[str] = []
        if response.text.strip():
            try:
                result = response.json()
            except ValueError:
                result = None
            if isinstance(result, dict):
                torrent_ids = [
                    str(value)
                    for value in (result.get("added_torrent_ids") or [])
                    if value
                ]
                success_count = int(result.get("success_count") or 0)
                pending_count = int(result.get("pending_count") or 0)
                failure_count = int(result.get("failure_count") or 0)
                if success_count == 0 and pending_count == 0 and failure_count > 0:
                    raise QBittorrentError(
                        "qBittorrent принял запрос, но не смог добавить торрент "
                        f"(failure_count={failure_count})"
                    )
            elif response.text.strip() not in {"Ok.", ""}:
                raise QBittorrentError(
                    f"qBittorrent вернул неожиданный ответ: {response.text.strip()}"
                )

        torrent_hash = torrent_ids[0] if torrent_ids else release.info_hash
        if torrent_hash:
            self._set_category_and_tags(torrent_hash, category=category, tags=tags)
        return str(torrent_hash or "")

    def start(self, torrent_hash: str) -> None:
        """Explicitly start/resume a torrent after adding it.

        qBittorrent 5.x renamed the resume endpoint to ``start``. Older
        installations still expose ``resume``, so try the modern endpoint
        first and fall back only when it is unavailable.
        """
        value = str(torrent_hash or "").strip()
        if not value:
            return
        self.login()
        payload = {"hashes": value}
        try:
            response = self.client.post("/api/v2/torrents/start", data=payload)
            if response.status_code in {404, 405}:
                response = self.client.post("/api/v2/torrents/resume", data=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось запустить торрент: {exc}") from exc

    def pause(self, torrent_hash: str) -> None:
        """Stop downloading and seeding while retaining the torrent and files."""
        value = str(torrent_hash or "").strip()
        if not value:
            return
        self.login()
        payload = {"hashes": value}
        try:
            response = self.client.post("/api/v2/torrents/stop", data=payload)
            if response.status_code in {404, 405}:
                response = self.client.post("/api/v2/torrents/pause", data=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось остановить торрент: {exc}") from exc

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        self.login()
        params = {"category": category} if category else None
        try:
            response = self.client.get("/api/v2/torrents/info", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(f"Не удалось получить загрузки qBittorrent: {exc}") from exc
        result: list[DownloadItem] = []
        for item in payload if isinstance(payload, list) else []:
            tags = {value.strip() for value in str(item.get("tags") or "").split(",") if value.strip()}
            media_id = None
            episode = None
            anime_title_tag = ""
            release_score_tag = None
            for tag in tags:
                if tag.startswith("anilist-"):
                    try:
                        media_id = int(tag.removeprefix("anilist-"))
                    except ValueError:
                        pass
                elif tag.casefold().startswith("anilist:"):
                    try:
                        media_id = int(tag.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif tag.startswith("episode-"):
                    try:
                        episode = int(tag.removeprefix("episode-"))
                    except ValueError:
                        pass
                elif tag.casefold().startswith("anime:"):
                    anime_title_tag = tag.split(":", 1)[1].strip()
                elif tag.casefold().startswith("episode:"):
                    try:
                        episode = int(tag.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif tag.casefold().startswith("score:"):
                    try:
                        release_score_tag = float(tag.split(":", 1)[1].strip())
                    except ValueError:
                        pass
            raw = dict(item)
            raw["backend"] = "qbittorrent"
            if anime_title_tag:
                raw["_anime_title_tag"] = anime_title_tag
            if media_id is not None:
                raw["_media_id_source"] = "tag"
            if release_score_tag is not None:
                raw["_release_score_tag"] = release_score_tag
            raw["_tag_set"] = sorted(tags, key=str.casefold)
            result.append(
                DownloadItem(
                    torrent_hash=str(item.get("hash") or ""),
                    name=str(item.get("name") or ""),
                    state=str(item.get("state") or ""),
                    progress=float(item.get("progress") or 0),
                    save_path=str(item.get("save_path") or ""),
                    content_path=str(item.get("content_path") or ""),
                    media_id=media_id,
                    episode=episode,
                    is_batch=any(tag.casefold() in {"batch", "series pack"} for tag in tags),
                    added_on=int(item.get("added_on") or 0),
                    completed_on=int(item.get("completion_on") or 0),
                    raw=raw,
                )
            )
        return result

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        self.login()
        try:
            response = self.client.get("/api/v2/torrents/files", params={"hash": torrent_hash})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(f"Не удалось получить файлы торрента: {exc}") from exc
        return list(payload) if isinstance(payload, list) else []

    def set_file_priority(
        self,
        torrent_hash: str,
        file_ids: list[int],
        priority: int,
    ) -> None:
        """Select or skip individual files in a multi-file torrent."""

        ids = sorted({int(value) for value in file_ids if int(value) >= 0})
        if not str(torrent_hash or "").strip() or not ids:
            return
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/filePrio",
                data={
                    "hash": str(torrent_hash).strip(),
                    "id": "|".join(str(value) for value in ids),
                    "priority": int(priority),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось выбрать файлы торрента: {exc}") from exc

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(f"Не удалось удалить торрент: {exc}") from exc
