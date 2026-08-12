from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .branding import APP_NAME, APP_SLUG, DATA_DIR, LOG_DIR


GITHUB_REPOSITORY = "TH1NKFASTER/pudge"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"


class UpdateError(RuntimeError):
    pass


def _version_key(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", str(value or ""))
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class AppUpdater:
    """Safe updater for stable release installs and clean development checkouts."""

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self.marker_path = DATA_DIR / "install-source.json"
        self.update_root = DATA_DIR / "updates"
        self.log_path = LOG_DIR / f"{APP_SLUG}-update.log"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "current_version": __version__,
            "release_url": GITHUB_RELEASES_URL,
        }
        self._latest_release_cache: tuple[float, dict[str, Any]] | None = None

    def _log(self, message: str, *args: Any) -> None:
        rendered = message % args if args else message
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {rendered}\n")
        except OSError:
            pass
        if self.logger:
            try:
                self.logger.info(message, *args)
            except Exception:
                pass

    def _source(self) -> dict[str, Any]:
        marker = _safe_json(self.marker_path)
        source = Path(str(marker.get("source_path") or "")).expanduser()
        if marker.get("channel") == "development" and source.is_dir() and (source / ".git").exists():
            marker["source_path"] = str(source.resolve())
            return marker
        return {"channel": "release"}

    @staticmethod
    def _github_headers() -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "User-Agent": APP_SLUG,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_json(self, url: str) -> dict[str, Any]:
        response = httpx.get(url, headers=self._github_headers(), timeout=15, follow_redirects=True)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise UpdateError("GitHub returned an invalid update response")
        return value

    def _latest_release(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._latest_release_cache
        if not force and cached and time.monotonic() - cached[0] < 1800:
            return dict(cached[1])
        value = self._get_json(f"{GITHUB_API}/releases/latest")
        tag = str(value.get("tag_name") or "").lstrip("v")
        if not _version_key(tag):
            raise UpdateError("The latest GitHub release has no valid version")
        value["version"] = tag
        self._latest_release_cache = (time.monotonic(), dict(value))
        return value

    @staticmethod
    def _git(source: Path, *args: str, timeout: int = 20) -> str:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise UpdateError((completed.stderr or completed.stdout or "git failed").strip())
        return completed.stdout.strip()

    @staticmethod
    def _git_optional(source: Path, *args: str, timeout: int = 20) -> str:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    @staticmethod
    def _git_succeeds(source: Path, *args: str, timeout: int = 20) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return completed.returncode == 0

    @staticmethod
    def _official_remote(url: str) -> bool:
        normalized = str(url or "").strip().casefold().removesuffix(".git").rstrip("/")
        return normalized in {
            f"https://github.com/{GITHUB_REPOSITORY}".casefold(),
            f"git@github.com:{GITHUB_REPOSITORY}".casefold(),
            f"ssh://git@github.com/{GITHUB_REPOSITORY}".casefold(),
        }

    def _development_status(self, source: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(source["source_path"]))
        dirty = bool(self._git(path, "status", "--porcelain", "--untracked-files=normal"))
        branch = self._git_optional(path, "symbolic-ref", "--short", "-q", "HEAD")
        head = self._git(path, "rev-parse", "HEAD")
        installed_revision = str(source.get("source_revision") or "")
        remote_url = self._git_optional(path, "remote", "get-url", "origin")
        blocked = ""
        if not self._official_remote(remote_url):
            blocked = "The development checkout origin is not the official Pudge repository"
        elif dirty:
            blocked = "The development checkout has local changes"
        elif not branch:
            blocked = "The development checkout is on a detached HEAD"
        remote_sha = ""
        if branch and not blocked:
            try:
                self._git(path, "fetch", "--quiet", "--no-tags", "origin", branch, timeout=60)
                remote_sha = self._git(path, "rev-parse", "FETCH_HEAD")
            except Exception as exc:
                blocked = blocked or f"Could not check the GitHub branch: {exc}"
        available = False
        if remote_sha and not blocked:
            if remote_sha == head:
                available = bool(installed_revision and installed_revision != head)
            elif self._git_succeeds(path, "merge-base", "--is-ancestor", head, remote_sha):
                available = True
            elif self._git_succeeds(path, "merge-base", "--is-ancestor", remote_sha, head):
                available = bool(installed_revision and installed_revision != head)
            else:
                blocked = "The development branch has diverged from its GitHub branch"
        return {
            "state": "checked",
            "channel": "development",
            "current_version": __version__,
            "branch": branch,
            "available": available,
            "blocked": bool(blocked),
            "detail": blocked,
            "installed_revision": installed_revision,
            "source_revision": head,
            "remote_revision": remote_sha,
            "release_url": f"https://github.com/{GITHUB_REPOSITORY}/tree/{branch or 'main'}",
        }

    def check(self, *, force: bool = True) -> dict[str, Any]:
        try:
            source = self._source()
            if source.get("channel") == "development":
                result = self._development_status(source)
            else:
                release = self._latest_release(force=force)
                latest = str(release["version"])
                result = {
                    "state": "checked",
                    "channel": "release",
                    "current_version": __version__,
                    "latest_version": latest,
                    "available": _version_key(latest) > _version_key(__version__),
                    "blocked": False,
                    "detail": "",
                    "release_url": str(release.get("html_url") or GITHUB_RELEASES_URL),
                }
        except Exception as exc:
            result = {
                "state": "failed",
                "channel": self._source().get("channel", "release"),
                "current_version": __version__,
                "available": False,
                "blocked": True,
                "detail": str(exc),
                "release_url": GITHUB_RELEASES_URL,
            }
        with self._lock:
            self._state = dict(result)
        return dict(result)

    def state(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._state)
            thread = self._thread
        result["running"] = bool(thread and thread.is_alive())
        result["log_path"] = str(self.log_path)
        return result

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                pass
            else:
                source = self._source()
                self._state = {
                    "state": "preparing",
                    "channel": source.get("channel", "release"),
                    "current_version": __version__,
                    "detail": "",
                    "release_url": GITHUB_RELEASES_URL,
                }
                target = self._development_worker if source.get("channel") == "development" else self._release_worker
                self._thread = threading.Thread(
                    target=target,
                    args=(source,),
                    name=f"{APP_SLUG}-update",
                    daemon=True,
                )
                self._thread.start()
        return self.state()

    def _set_state(self, state: str, detail: str = "", **values: Any) -> None:
        with self._lock:
            self._state.update({"state": state, "detail": detail, **values})

    def _development_worker(self, source: dict[str, Any]) -> None:
        try:
            self._log("APP development update started")
            status = self._development_status(source)
            if status.get("blocked"):
                raise UpdateError(str(status.get("detail") or "Development update is blocked"))
            if not status.get("available"):
                self._set_state("current", "The development checkout is already current")
                return
            path = Path(str(source["source_path"])).resolve()
            branch = str(status.get("branch") or "")
            remote_revision = str(status.get("remote_revision") or "")
            if not remote_revision:
                raise UpdateError("GitHub did not return a revision to install")
            self._set_state(
                "installing",
                f"Installing {remote_revision[:10]} from {branch}",
                channel="development",
                available=True,
                branch=branch,
                source_revision=status.get("source_revision"),
                remote_revision=remote_revision,
            )
            self._launch_script(
                path,
                ["git", "-C", str(path), "merge", "--ff-only", remote_revision],
            )
        except Exception as exc:
            self._log("APP update development failed: %s", exc)
            self._set_state("failed", str(exc))

    @staticmethod
    def _asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
        return next(
            (dict(asset) for asset in release.get("assets") or [] if str(asset.get("name") or "") == name),
            None,
        )

    def _download(self, url: str, target: Path) -> str:
        digest = hashlib.sha256()
        with httpx.stream(
            "GET", url, headers=self._github_headers(), timeout=120, follow_redirects=True
        ) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    digest.update(chunk)
        return digest.hexdigest()

    def _expected_digest(self, release: dict[str, Any], asset: dict[str, Any], archive_name: str) -> str:
        digest = str(asset.get("digest") or "")
        if digest.casefold().startswith("sha256:"):
            value = digest.split(":", 1)[1].strip().casefold()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        checksum_asset = self._asset(release, f"{archive_name}.sha256")
        if checksum_asset:
            response = httpx.get(
                str(checksum_asset.get("browser_download_url") or ""),
                headers=self._github_headers(),
                timeout=20,
                follow_redirects=True,
            )
            response.raise_for_status()
            value = response.text.strip().split()[0].casefold()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
        raise UpdateError("This release has no SHA-256 digest; open GitHub Releases and update manually")

    @staticmethod
    def _extract_archive(archive: Path, destination: Path, version: str) -> Path:
        root = destination.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                resolved = (root / entry.filename).resolve()
                if resolved != root and root not in resolved.parents:
                    raise UpdateError("The release archive contains an unsafe path")
            bundle.extractall(root)
        candidates = [path.parent for path in root.glob("*/install.sh") if path.is_file()]
        if len(candidates) != 1:
            raise UpdateError("The release archive does not contain one installer")
        project = candidates[0]
        metadata = (project / "pyproject.toml").read_text(encoding="utf-8")
        if not re.search(rf'^version\s*=\s*"{re.escape(version)}"\s*$', metadata, re.MULTILINE):
            raise UpdateError("The release archive version does not match GitHub")
        return project

    def _release_worker(self, _source: dict[str, Any]) -> None:
        temporary: Path | None = None
        try:
            release = self._latest_release(force=True)
            version = str(release["version"])
            if _version_key(version) <= _version_key(__version__):
                self._set_state("current", "Pudge is already current", latest_version=version)
                return
            archive_name = f"{APP_SLUG}-macos-v{version}.zip"
            asset = self._asset(release, archive_name)
            if not asset:
                raise UpdateError(f"GitHub Release does not contain {archive_name}")
            expected = self._expected_digest(release, asset, archive_name)
            self._log("APP release update v%s started", version)
            self.update_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f"v{version}-", dir=self.update_root))
            archive = temporary / archive_name
            self._set_state("downloading", f"Downloading Pudge {version}", latest_version=version)
            actual = self._download(str(asset.get("browser_download_url") or ""), archive)
            if actual != expected:
                raise UpdateError("The downloaded update failed SHA-256 verification")
            self._log("APP release update v%s SHA-256 verified", version)
            self._set_state("installing", f"Installing Pudge {version}", latest_version=version)
            project = self._extract_archive(archive, temporary / "release", version)
            self._launch_script(project, [])
        except Exception as exc:
            self._log("APP update release failed: %s", exc)
            self._set_state("failed", str(exc))
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    def _launch_script(self, project: Path, prefix: list[str]) -> None:
        project = project.resolve()
        script = project.parent / "run-pudge-update.zsh"
        app_path = Path.home() / "Applications" / (APP_NAME + ".app")
        rollback_path = self.update_root / "rollback" / (APP_NAME + ".app.before-update")
        prefix_line = " ".join(shlex.quote(value) for value in prefix)
        commands = [
            "#!/bin/zsh",
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(str(self.log_path.parent))}",
            f"mkdir -p {shlex.quote(str(rollback_path.parent))}",
            f"exec >> {shlex.quote(str(self.log_path))} 2>&1",
            "unset TCL_LIBRARY TK_LIBRARY TCLLIBPATH PYTHONHOME PYTHONPATH PYTHONEXECUTABLE",
        ]
        if prefix_line:
            commands.append(prefix_line)
        commands.extend(
            [
                f"cd {shlex.quote(str(project))}",
                f"rm -rf {shlex.quote(str(rollback_path))}",
                f"if [[ -d {shlex.quote(str(app_path))} ]]; then /usr/bin/ditto {shlex.quote(str(app_path))} {shlex.quote(str(rollback_path))}; fi",
                f"/usr/bin/pkill -f {shlex.quote(str(app_path / 'Contents' / 'MacOS' / APP_NAME))} >/dev/null 2>&1 || true",
                "/usr/bin/pkill -f 'pudge.app_entry' >/dev/null 2>&1 || true",
                "/bin/sleep 1",
                "if ! ./install.sh --update; then",
                f"  rm -rf {shlex.quote(str(app_path))}",
                f"  if [[ -d {shlex.quote(str(rollback_path))} ]]; then /bin/mv {shlex.quote(str(rollback_path))} {shlex.quote(str(app_path))}; fi",
                f"  if [[ -d {shlex.quote(str(app_path))} ]]; then /usr/bin/open -n {shlex.quote(str(app_path))}; fi",
                "  exit 1",
                "fi",
                f"rm -rf {shlex.quote(str(rollback_path))}",
                f"/usr/bin/open -n {shlex.quote(str(app_path))}",
            ]
        )
        script.write_text("\n".join(commands) + "\n", encoding="utf-8")
        os.chmod(script, 0o700)
        subprocess.Popen(
            ["/bin/zsh", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._log("APP update installer launched from %s", project)
        self._set_state("restarting", "Installer started; Pudge will reopen automatically")
