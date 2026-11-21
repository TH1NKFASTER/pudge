from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..branding import APP_NAME, APP_SLUG, LEGACY_APP_SLUGS
from ..manager_models import DownloadItem, NyaaRelease
from ..i18n import err


_VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts"}
# /api/v2/app/version answers like "v4.6.0" or "v5.1.2" (pre-releases add a suffix).
_QBT_VERSION_RE = re.compile(r"v\d+(?:\.\d+)+[0-9A-Za-z.+-]*")


class QBittorrentError(RuntimeError):
    pass


def connection_failed(exc: BaseException | None) -> bool:
    """Whether *exc* (or what caused it) means the Web UI could not be reached at all."""
    for _ in range(6):
        if exc is None:
            return False
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# Info hashes a metadata probe or an add is working on right now, with the
# time the holder expects to be done. The probe adds a temporary torrent and
# deletes it with its files afterwards; an add of the same release during
# those seconds took the temporary torrent over, and the probe then deleted
# the user's new download.
_busy_hashes: dict[str, float] = {}
_busy_condition = threading.Condition()


@contextmanager
def _hash_reserved(info_hash: str, hold_seconds: float) -> Iterator[None]:
    """Hold *info_hash* for one probe or add; wait (bounded) while another call holds it."""
    key = str(info_hash or "").strip().lower()
    if not key:
        yield
        return
    with _busy_condition:
        while key in _busy_hashes:
            remaining = _busy_hashes[key] - time.monotonic()
            if remaining <= 0:
                raise QBittorrentError(err("qbt.torrentBusy"))
            _busy_condition.wait(remaining)
        _busy_hashes[key] = time.monotonic() + max(1.0, float(hold_seconds))
    try:
        yield
    finally:
        with _busy_condition:
            _busy_hashes.pop(key, None)
            _busy_condition.notify_all()


def same_web_ui_address(first: str, second: str) -> bool:
    """Whether two Web UI addresses are the same server (loopback names are one host)."""
    loopback = {"127.0.0.1", "localhost", "::1"}

    def parts(value: str) -> tuple[str, str, int | None, str] | None:
        parsed = urlparse(QBittorrentClient._normalize_url(str(value or "")))
        name = (parsed.hostname or "").casefold()
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        return (
            parsed.scheme.casefold(),
            "127.0.0.1" if name in loopback else name,
            port,
            (parsed.path or "").rstrip("/"),
        )

    a, b = parts(first), parts(second)
    return a is not None and a == b


class _SessionClient(httpx.Client):
    """httpx client that logs in again once when the Web UI session expired.

    qBittorrent drops an idle SID cookie (403). A long-lived client would
    otherwise fail every call after that until it is recreated.
    """

    def __init__(self, *args: Any, relogin: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._relogin = relogin

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        response = super().send(request, **kwargs)
        if (
            response.status_code != 403
            or self._relogin is None
            or request.url.path.endswith("/api/v2/auth/login")
        ):
            return response
        try:
            content = request.read()
        except Exception:  # noqa: BLE001 - a streamed body cannot be replayed
            return response
        if not self._relogin():
            return response
        response.close()
        retry = self.build_request(
            request.method,
            request.url,
            headers={key: value for key, value in request.headers.items() if key.casefold() != "cookie"},
            content=content,
        )
        return super().send(retry, **kwargs)


class QBittorrentClient:
    # A failed search for a local Web UI (lsof plus a dozen HTTP probes) is
    # remembered per configured address for a few minutes. With qBittorrent
    # closed, status polls and Torrent Off used to repeat it on every call.
    # A call that may launch qBittorrent (adding a torrent) still searches.
    DISCOVERY_RETRY_SECONDS = 180.0
    _discovery_failed_at: dict[str, float] = {}
    _discovery_lock = threading.Lock()

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
        # A shared, long-lived client (one per backend in the manager) ignores
        # callers' close(); start_policy decides per call whether a closed
        # qBittorrent may be launched (adding a torrent: yes; status polls: no).
        self._shared = False
        self.start_policy: Any = None
        # Where the app's category points (the library root), not the first
        # title's folder that happened to create it.
        self.category_save_path: Path | None = None
        self.client = self._new_client(self.base_url)
        self._authenticated = bool(self.api_key)
        self._version: str | None = None
        self._configured_url = self.base_url
        # True once a call failed because the Web UI could not be reached.
        self.unreachable = False
        self._hook_prepared_at = 0.0

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

    def _credentials_allowed(self, base_url: str) -> bool:
        """The login and API key go only to the address in Settings.

        Discovery finds qBittorrent on another local port by looking at it; any
        process (another account's) can listen there and show a "qBittorrent"
        page. ``localhost``/``127.0.0.1``/``[::1]`` with the same scheme and port
        count as the configured address.
        """
        configured = getattr(self, "_configured_url", "") or self.base_url
        return same_web_ui_address(configured, base_url)

    def _headers(self, base_url: str) -> dict[str, str]:
        headers = {"Referer": base_url, "Origin": base_url, "User-Agent": APP_SLUG}
        # qBittorrent >= 5.2 authenticates API keys as an HTTP Bearer token.
        if self.api_key and self._credentials_allowed(base_url):
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _new_client(self, base_url: str) -> httpx.Client:
        return _SessionClient(
            base_url=base_url,
            timeout=self.timeout,
            follow_redirects=True,
            verify=self.verify_tls,
            headers=self._headers(base_url),
            relogin=self._relogin_after_403,
        )

    def _relogin_after_403(self) -> bool:
        if self.api_key or not self._credentials_allowed(self.base_url):
            return False
        self._authenticated = False
        try:
            self.login()
        except QBittorrentError:
            return False
        return True

    def _switch_base_url(self, base_url: str) -> None:
        base_url = self._normalize_url(base_url)
        if base_url == self.base_url:
            return
        self.client.close()
        self.base_url = base_url
        self.client = self._new_client(base_url)
        self._authenticated = bool(self.api_key) and self._credentials_allowed(base_url)
        self._version = None

    def close(self) -> None:
        if self._shared:
            return
        self.client.close()

    def close_shared(self) -> None:
        """Really close a shared client (settings changed or the app quits)."""
        self.client.close()

    def _may_start_app(self) -> bool:
        if not self.auto_start_app:
            return False
        policy = self.start_policy
        return bool(policy()) if callable(policy) else True

    def _run_hook(self) -> None:
        if not self.pre_download_command:
            return
        prepared, self._hook_prepared_at = self._hook_prepared_at, 0.0
        if prepared and time.monotonic() - prepared < 60.0:
            return  # prepare_network_start() has just run it.
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
            raise QBittorrentError(err("manager.vpnCommandFailed", error=detail))

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
        # No API key while probing: a candidate port may be an unrelated local
        # service, and the probe used to send it the Bearer key. Credentials go
        # only to the configured address or one identified as qBittorrent.
        headers = self._headers(base_url)
        try:
            with httpx.Client(
                base_url=base_url,
                timeout=self.timeout,
                follow_redirects=True,
                verify=self.verify_tls,
                headers=headers,
            ) as client:
                response = client.get("/api/v2/app/version", timeout=min(self.timeout, 2.5))
                version = response.text.strip()
                if response.status_code == 200 and _QBT_VERSION_RE.fullmatch(version):
                    return True, version
                if response.status_code != 403:
                    return False, ""
                # 403: authentication is required. Any service can answer 403,
                # which used to count as "qBittorrent found" and then got the
                # login details; elsewhere only qBittorrent's own login page counts.
                if base_url == self._configured_url:
                    return True, ""
                page = client.get("/", timeout=min(self.timeout, 2.5))
        except httpx.HTTPError:
            return False, ""
        return page.status_code == 200 and "qbittorrent" in page.text.casefold(), ""

    def _discover_local_webui(self) -> str | None:
        for candidate in self._candidate_urls():
            reachable, _version = self._probe(candidate)
            if reachable:
                return candidate
        return None

    def _discovery_recently_failed(self) -> bool:
        with self._discovery_lock:
            failed_at = self._discovery_failed_at.get(self._configured_url)
        return failed_at is not None and 0 <= time.monotonic() - failed_at < self.DISCOVERY_RETRY_SECONDS

    def _remember_discovery(self, *, found: bool) -> None:
        with self._discovery_lock:
            if found:
                self._discovery_failed_at.pop(self._configured_url, None)
            else:
                self._discovery_failed_at[self._configured_url] = time.monotonic()

    def _recover_connection(self) -> bool:
        if self._may_start_app():
            subprocess.run(["open", "-gja", "qBittorrent"], check=False)
            # qBittorrent can take several seconds to expose WebUI after launch.
            for delay in (1.0, 1.5, 2.5, 3.0):
                time.sleep(delay)
                detected = self._discover_local_webui()
                if detected:
                    self._remember_discovery(found=True)
                    self._switch_base_url(detected)
                    return True
        elif self._discovery_recently_failed():
            return False
        detected = self._discover_local_webui()
        self._remember_discovery(found=bool(detected))
        if detected:
            self._switch_base_url(detected)
            return True
        return False

    def _connection_error(self, exc: Exception) -> QBittorrentError:
        self.unreachable = True
        return QBittorrentError(err("qbt.webUiNotResponding", url=self.base_url, app=APP_NAME, error=exc))

    def prepare_network_start(self, *, hook: bool) -> None:
        """Run the slow part of a start before it is admitted: the pre-download
        hook (up to 90 s) and reaching, or launching, qBittorrent. The caller then
        takes the shared Torrent Off lock only for the RPC that starts traffic;
        that call's own hook run is skipped.
        """
        if hook and self.pre_download_command:
            self._run_hook()
            self._hook_prepared_at = time.monotonic()
        self.login()

    def _login_elsewhere(self) -> None:
        """A discovered Web UI that is not the configured address gets no credentials.

        It is used only when it answers without a login (qBittorrent's "bypass
        authentication for clients on localhost"); otherwise the user is told to
        put that address in Settings.
        """
        try:
            response = self.client.get("/api/v2/app/version")
        except httpx.HTTPError as exc:
            raise self._connection_error(exc) from exc
        self.unreachable = False
        version = response.text.strip()
        if response.status_code == 200 and _QBT_VERSION_RE.fullmatch(version):
            self._version = version
            self._authenticated = True
            return
        raise QBittorrentError(
            err("qbt.webUiElsewhere", url=self.base_url, configured=self._configured_url, app=APP_NAME)
        )

    def login(self) -> None:
        if self._authenticated:
            return
        if not self._credentials_allowed(self.base_url):
            self._login_elsewhere()
            return
        try:
            response = self.client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
        except httpx.ConnectError as exc:
            if not self._recover_connection():
                raise self._connection_error(exc) from exc
            if not self._credentials_allowed(self.base_url):
                self._login_elsewhere()
                return
            try:
                response = self.client.post(
                    "/api/v2/auth/login",
                    data={"username": self.username, "password": self.password},
                )
            except httpx.HTTPError as second_exc:
                raise self._connection_error(second_exc) from second_exc
        except httpx.HTTPError as exc:
            raise QBittorrentError(err("qbt.apiUnavailable", error=exc)) from exc
        self.unreachable = False
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QBittorrentError(err("qbt.loginRejectedHttp", status=response.status_code)) from exc
        if response.text.strip() != "Ok.":
            raise QBittorrentError(err("qbt.loginRejected", response=response.text.strip()))
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
            raise QBittorrentError(err("qbt.versionFailed", error=exc)) from exc
        if response.status_code == 403 and self.api_key:
            raise QBittorrentError(err("qbt.apiKeyRejected"))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QBittorrentError(err("qbt.versionHttp", status=response.status_code)) from exc
        self._version = response.text.strip()
        return self._version

    @staticmethod
    def _version_at_least(value: str, minimum: tuple[int, int]) -> bool:
        numbers = [int(part) for part in re.findall(r"\d+", value)[:2]]
        if len(numbers) < 2:
            return False
        return (numbers[0], numbers[1]) >= minimum

    @staticmethod
    def _release_source(release: NyaaRelease) -> str:
        """The feed's own magnet when it lists trackers; the one rebuilt from the hash has none."""
        magnets = [
            str(value or "").strip()
            for value in (getattr(release, "torrent_url", ""), release.magnet)
            if str(value or "").strip().casefold().startswith("magnet:?")
        ]
        if not magnets:
            return release.magnet
        return max(magnets, key=lambda value: (value.casefold().count("&tr="), len(value)))

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
            raise QBittorrentError(err("qbt.categoriesFailed", error=exc)) from exc
        return dict(payload) if isinstance(payload, dict) else {}

    def tags(self) -> set[str]:
        self.login()
        try:
            response = self.client.get("/api/v2/torrents/tags")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(err("qbt.tagsFailed", error=exc)) from exc
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
            raise QBittorrentError(err("qbt.untagFailed", error=exc)) from exc

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
            raise QBittorrentError(err("qbt.deleteTagsFailed", error=exc)) from exc
        return len(clean)

    def cleanup_tags(self) -> dict[str, int]:
        """Remove obsolete score tags and every globally unused qBittorrent tag."""
        items = self.torrents(category="")
        removed_score = 0
        used: set[str] = set()
        for item in items:
            item_tags = {str(tag).strip() for tag in item.raw.get("_tag_set", []) if str(tag).strip()}
            score_tags = {tag for tag in item_tags if tag.casefold().startswith("score:")}
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
                    "savePath": str((self.category_save_path or save_path).expanduser()),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(err("qbt.createCategoryFailed", category=category, error=exc)) from exc

    def _torrent_by_hash(self, info_hash: str) -> dict[str, Any] | None:
        try:
            return self._lookup_torrent(info_hash)
        except QBittorrentError:
            return None

    def _lookup_torrent(self, info_hash: str) -> dict[str, Any] | None:
        """The torrent with this hash, or None when qBittorrent says it has none.

        Raises when the lookup itself failed: "could not ask" is not "not there".
        """
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
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(err("qbt.listFailed", error=exc)) from exc
        if not isinstance(payload, list):
            raise QBittorrentError(err("qbt.unexpectedResponse", response=str(payload)[:200]))
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

    def find_torrent(self, info_hash: str) -> dict[str, Any] | None:
        """The torrent with this hash, None when qBittorrent has none; raises when it could not be asked."""
        self.login()
        return self._lookup_torrent(str(info_hash or ""))

    def torrent_status(self, info_hash: str) -> dict[str, Any] | None:
        """Return fresh qBittorrent stats for one torrent hash."""
        self.login()
        return self._torrent_by_hash(info_hash)

    def lookup_torrent(self, info_hash: str) -> dict[str, Any] | None:
        """Like torrent_status, but raises when qBittorrent could not be asked.

        None means qBittorrent answered that it has no such torrent.
        """
        self.login()
        return self._lookup_torrent(info_hash)

    def has_torrent(self, info_hash: str) -> bool:
        """Whether qBittorrent already has this torrent; raises when it could not be asked.

        Called right before add_release: the login may launch qBittorrent, so
        the pre-download hook runs first, as it does there (add_release then
        skips it).
        """
        self.prepare_network_start(hook=True)
        # A running metadata probe's temporary torrent is not the user's.
        with _hash_reserved(info_hash, self.timeout + 10.0):
            return self._lookup_torrent(str(info_hash or "")) is not None

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
            old_tags = [tag.strip() for tag in str(existing.get("tags") or "").split(",") if tag.strip()]
            legacy_app_tags = {value.casefold() for value in LEGACY_APP_SLUGS}
            obsolete = [
                tag
                for tag in old_tags
                if tag.casefold().startswith(
                    ("anilist-", "anilist:", "episode-", "anime:", "episode:", "score:")
                )
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
            raise QBittorrentError(err("qbt.labelFailed", error=exc)) from exc

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
            raise QBittorrentError(err("qbt.setLocationFailed", error=exc)) from exc

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
            raise QBittorrentError(err("qbt.recheckFailed", error=exc)) from exc

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
        return self.add_release_checked(
            release,
            save_path=save_path,
            category=category,
            tags=tags,
            paused=paused,
            stop_at_metadata=stop_at_metadata,
        )[0]

    def add_release_checked(
        self,
        release: NyaaRelease,
        *,
        save_path: Path,
        category: str,
        tags: list[str],
        paused: bool = False,
        stop_at_metadata: bool = False,
        adopt_existing: bool = True,
    ) -> tuple[str, bool]:
        """Add a release: ``(hash, added)``, where ``added`` is False for a torrent already there.

        With ``adopt_existing=False`` a torrent already in qBittorrent (the
        user's own, or one still downloading) keeps its category and tags, and
        a failed lookup raises instead of being taken for "not there".
        """
        self._run_hook()
        self.login()
        target = save_path.expanduser()
        target.mkdir(parents=True, exist_ok=True)
        self.ensure_category(category, target)

        def found(existing: dict[str, Any]) -> tuple[str, bool]:
            torrent_hash = str(existing.get("hash") or release.info_hash)
            if adopt_existing:
                self._set_category_and_tags(torrent_hash, category=category, tags=tags)
            return torrent_hash, False

        lookup = self._torrent_by_hash if adopt_existing else self._lookup_torrent
        # A metadata probe of this release may hold a temporary torrent for a
        # few seconds and deletes it with its files when done. Wait for it
        # instead of taking that torrent over as the user's download.
        with _hash_reserved(release.info_hash, 3 * self.timeout + 10.0):
            # qBittorrent 5.2 returns 409 when all supplied torrents failed to add.
            # A duplicate torrent is the most common case, so make this operation idempotent.
            existing = lookup(release.info_hash)
            if existing is not None:
                return found(existing)

            # qBittorrent 5.2 renamed `paused` to `stopped` and `root_folder` to
            # `contentLayout`. URL/magnet additions do not require multipart upload,
            # so use a regular form request with version-compatible field names.
            payload = {
                "urls": self._release_source(release),
                "savepath": str(target),
                # Automatic torrent management (a qBittorrent default some users
                # pick) ignores savepath and moves the torrent to the category path.
                "autoTMM": "false",
                **self._add_state_payload(paused=paused, stop_at_metadata=stop_at_metadata),
            }
            if stop_at_metadata:
                payload["stopCondition"] = "MetadataReceived"
            try:
                response = self.client.post("/api/v2/torrents/add", data=payload)
            except httpx.HTTPError as exc:
                raise QBittorrentError(err("qbt.addFailed", error=exc)) from exc

            if response.status_code == 409:
                # qBittorrent may report Conflict for an already present torrent.
                # Re-check after the add attempt because magnet addition can race with UI refresh.
                for delay in (0.0, 0.2, 0.6):
                    if delay:
                        time.sleep(delay)
                    existing = self._torrent_by_hash(release.info_hash)
                    if existing is not None:
                        return found(existing)
                detail = response.text.strip()
                suffix = err("qbt.responseSuffix", response=detail) if detail and detail != "Conflict" else ""
                raise QBittorrentError(err("qbt.addConflict", target=target, response=suffix))

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text.strip()
                raise QBittorrentError(
                    err(
                        "qbt.addFailed",
                        error=f"HTTP {response.status_code}" + (f" — {detail}" if detail else ""),
                    )
                ) from exc

            torrent_ids: list[str] = []
            if response.text.strip():
                try:
                    result = response.json()
                except ValueError:
                    result = None
                if isinstance(result, dict):
                    torrent_ids = [str(value) for value in (result.get("added_torrent_ids") or []) if value]
                    success_count = int(result.get("success_count") or 0)
                    pending_count = int(result.get("pending_count") or 0)
                    failure_count = int(result.get("failure_count") or 0)
                    if success_count == 0 and pending_count == 0 and failure_count > 0:
                        raise QBittorrentError(err("qbt.addRejected", failures=failure_count))
                elif response.text.strip() not in {"Ok.", ""}:
                    raise QBittorrentError(err("qbt.unexpectedResponse", response=response.text.strip()))

            torrent_hash = torrent_ids[0] if torrent_ids else release.info_hash
            if torrent_hash:
                self._set_category_and_tags(torrent_hash, category=category, tags=tags)
            return str(torrent_hash or ""), True

    def inspect_release_files(
        self,
        release: NyaaRelease,
        *,
        save_path: Path,
        metadata_timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Read a torrent file list through magnet metadata without downloading payload data.

        Existing user torrents are never modified or deleted.  A temporary magnet
        added by this helper is stopped at metadata and removed again afterwards.
        """
        self.login()
        info_hash = str(release.info_hash or "")
        timeout = max(1.0, float(metadata_timeout))
        with _hash_reserved(info_hash, timeout + 10.0):
            # A failed lookup stops the probe. It used to look like "not in
            # qBittorrent": the probe then added the user's own torrent again
            # and its cleanup deleted that torrent with its files.
            existing = self._lookup_torrent(info_hash)
            if existing is not None:
                return self._wait_for_files(str(existing.get("hash") or info_hash), timeout)

            target = save_path.expanduser()
            target.mkdir(parents=True, exist_ok=True)
            payload = {
                "urls": self._release_source(release),
                "savepath": str(target),
                "autoTMM": "false",
                **self._add_state_payload(paused=True, stop_at_metadata=True),
                "stopCondition": "MetadataReceived",
            }
            added_hash = info_hash
            added_by_us = False
            try:
                response = self.client.post("/api/v2/torrents/add", data=payload)
                if response.status_code != 409:
                    response.raise_for_status()
                body = response.text.strip()
                result: Any = None
                if body and body not in {"Ok.", "Fails."}:
                    try:
                        result = response.json()
                    except ValueError:
                        result = None
                if isinstance(result, dict):
                    ids = [str(value) for value in (result.get("added_torrent_ids") or []) if value]
                    if ids:
                        added_hash = ids[0]
                    added_by_us = bool(ids) or any(
                        int(result.get(name) or 0) > 0 for name in ("success_count", "pending_count")
                    )
                else:
                    added_by_us = response.status_code != 409 and body == "Ok."
                if not added_by_us:
                    # Only a real add is ours to delete afterwards. qBittorrent
                    # before 5.2 answers HTTP 200 "Fails." (5.2+: 409) when the
                    # torrent is already there, e.g. the user just added it.
                    existing = self._lookup_torrent(info_hash)
                    if existing is None:
                        raise QBittorrentError(
                            err("qbt.unexpectedResponse", response=body or f"HTTP {response.status_code}")
                        )
                    return self._wait_for_files(str(existing.get("hash") or info_hash), timeout)
                if not added_hash:
                    raise QBittorrentError(err("qbt.noMetadataHash"))
                return self._wait_for_files(added_hash, timeout)
            except httpx.HTTPError as exc:
                raise QBittorrentError(err("qbt.metadataFailed", error=exc)) from exc
            finally:
                if added_by_us and added_hash:
                    try:
                        # Without its files: stopped at metadata it wrote nothing, but its save
                        # path is the library folder, where "delete files" removed a copy of
                        # this release the person already had there.
                        self.delete(added_hash, delete_files=False)
                    except Exception as exc:  # noqa: BLE001 - metadata probe cleanup is best effort
                        logging.getLogger(__name__).warning(
                            "WARN step=qbittorrent.metadata_probe_cleanup hash=%s error=%r",
                            added_hash,
                            exc,
                        )

    def _wait_for_files(self, torrent_hash: str, timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.files(torrent_hash)
            if rows:
                return rows
            time.sleep(0.25)
        return []

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
            raise QBittorrentError(err("qbt.startFailed", error=exc)) from exc

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
            raise QBittorrentError(err("qbt.stopFailed", error=exc)) from exc

    def prioritize_queue(self, torrent_hashes: list[str]) -> bool:
        """Order torrents in qBittorrent's queue as given (first = most wanted, on top).

        Only matters with qBittorrent's torrent queueing on; without it qBittorrent answers 409
        and nothing changes."""
        hashes = [str(value or "").strip() for value in torrent_hashes if str(value or "").strip()]
        if len(hashes) < 2:
            return False
        self.login()
        try:
            for value in reversed(hashes):
                response = self.client.post("/api/v2/torrents/topPrio", data={"hashes": value})
                if response.status_code == 409:
                    return False
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(str(exc)) from exc
        return True

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        self.login()
        params = {"category": category} if category else None
        try:
            response = self.client.get("/api/v2/torrents/info", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QBittorrentError(err("qbt.listFailed", error=exc)) from exc
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
            tagged_batch = any(tag.casefold() in {"batch", "series pack"} for tag in tags)
            # A single-file torrent (content_path is the video itself) is one episode, whatever it was requested as.
            single_video = Path(str(item.get("content_path") or "")).suffix.casefold() in _VIDEO_SUFFIXES
            if tagged_batch and single_video:
                raw["single_video_file"] = True
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
                    is_batch=tagged_batch and not single_video,
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
            raise QBittorrentError(err("qbt.filesFailed", error=exc)) from exc
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
            raise QBittorrentError(err("qbt.selectFilesFailed", error=exc)) from exc

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.login()
        try:
            response = self.client.post(
                "/api/v2/torrents/delete",
                data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError(err("qbt.deleteFailed", error=exc)) from exc
