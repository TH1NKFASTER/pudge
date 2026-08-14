from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

JITEN_MPV_INSTALLER_COMMIT = "90e407cc5dd963d5e36babf2a1e2ea4db2b07e6c"
JITEN_MPV_INSTALLER_URL = (
    "https://raw.githubusercontent.com/Sirush/JitenMPV/"
    f"{JITEN_MPV_INSTALLER_COMMIT}/installers/unix.sh"
)


class FirstExperienceError(RuntimeError):
    pass


def _command_path(command: str) -> str:
    value = str(command or "").strip()
    if not value:
        return ""
    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    return str(shutil.which(value) or "")


def _brew_path() -> str:
    for value in (
        shutil.which("brew"),
        "/opt/homebrew/bin/brew",
        "/usr/local/bin/brew",
    ):
        if value and Path(value).is_file():
            return str(Path(value).resolve())
    return ""


def _tool_status(name: str, configured: str, version_arg: str) -> dict[str, Any]:
    path = _command_path(configured) or _command_path(name)
    version = ""
    if path:
        try:
            completed = subprocess.run(
                [path, version_arg],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            output = (completed.stdout or completed.stderr or "").strip().splitlines()
            version = output[0][:240] if output else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"installed": bool(path), "path": path, "version": version}


def _jiten_paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    return (
        home / ".local" / "share" / "jiten-mpv",
        home / ".config" / "mpv" / "scripts" / "jiten-mpv.lua",
        home / ".config" / "jiten-mpv" / "config.json",
    )


def _mpv_script_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".config" / "mpv" / "scripts",
        home / "Library" / "Application Support" / "mpv" / "scripts",
        home / ".local" / "share" / "mpv" / "scripts",
    )


def _looks_like_jpdb_script(path: Path, scripts_root: Path) -> bool:
    """Recognize both named and installer-generated jpdb mpv scripts."""
    try:
        relative = path.relative_to(scripts_root)
    except ValueError:
        relative = path
    if "jpdb" in str(relative).casefold():
        return True
    if path.suffix.casefold() not in {".lua", ".js"}:
        return False
    try:
        # The official installer may leave a generic main.lua/client.lua name.
        # Inspect only a bounded text prefix; credentials are never extracted.
        source = path.read_text(encoding="utf-8", errors="ignore")[:256_000]
    except OSError:
        return False
    return "jpdb" in source.casefold()


def _jpdb_installations() -> list[dict[str, Path]]:
    """Discover the official jpdb plugin plus compatible layout variants."""
    found: list[dict[str, Path]] = []
    seen: set[str] = set()
    for scripts_root in _mpv_script_roots():
        if not scripts_root.is_dir():
            continue
        try:
            entries = list(scripts_root.rglob("*"))[:5000]
        except OSError:
            continue
        lua_files = [
            path for path in entries
            if path.is_file()
            and path.suffix.casefold() in {".lua", ".js"}
            and _looks_like_jpdb_script(path, scripts_root)
            and len(path.relative_to(scripts_root).parts) <= 5
        ]
        for script in lua_files:
            plugin_dir = script.parent
            nearby = [plugin_dir, plugin_dir.parent]
            servers: list[Path] = []
            for directory in nearby:
                try:
                    servers.extend(
                        path for path in directory.iterdir()
                        if path.is_file()
                        and "jpdb" in path.name.casefold()
                        and path.suffix.casefold() not in {".lua", ".json", ".txt", ".md"}
                    )
                except OSError:
                    pass
            preferred = next(
                (path for path in servers if "server" in path.name.casefold()),
                servers[0] if servers else Path(),
            )
            key = str(script.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "dir": plugin_dir,
                    "script": script,
                    "server": preferred,
                    "config": plugin_dir / "config.json",
                }
            )
    return found


def _jpdb_paths() -> tuple[Path, Path, Path]:
    installations = _jpdb_installations()
    if installations:
        selected = max(
            installations,
            key=lambda item: (
                item["server"].is_file()
                and (os.name == "nt" or os.access(item["server"], os.X_OK)),
                item["script"].name.casefold() == "main.lua",
            ),
        )
        return selected["dir"], selected["script"], selected["server"]
    plugin_dir = Path.home() / ".config" / "mpv" / "scripts" / "jpdb-mpv-plugin"
    executable = plugin_dir / ("jpdb-server.exe" if os.name == "nt" else "jpdb-server")
    return plugin_dir, plugin_dir / "main.lua", executable


def _json_secret_configured(path: Path, *keys: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    placeholders = {"", "your_jpdb_api_token_here", "your_api_key_here", "changeme"}
    return any(str(payload.get(key) or "").strip().casefold() not in placeholders for key in keys)


def mpv_study_status(
    *,
    jiten_api_key: str = "",
    jpdb_api_token: str = "",
    selected_plugin: str = "auto",
) -> dict[str, Any]:
    program_dir, script_path, config_path = _jiten_paths()
    jiten_installed = script_path.is_file() and program_dir.is_dir()
    jpdb_dir, jpdb_script, jpdb_server = _jpdb_paths()
    variants = _jpdb_installations()
    jpdb_server_ready = jpdb_server.is_file() and (
        os.name == "nt" or os.access(jpdb_server, os.X_OK)
    )
    # Lua-only forks may talk directly to jpdb. The official plugin additionally
    # ships an executable server, which is verified when present.
    jpdb_installed = jpdb_script.is_file() and (
        jpdb_server_ready or not any(item["server"].is_file() for item in variants)
    )
    jiten_key = bool(str(jiten_api_key or "").strip()) or _json_secret_configured(
        config_path, "api_key"
    )
    jpdb_config = jpdb_dir / "config.json"
    jpdb_key = bool(str(jpdb_api_token or "").strip()) or _json_secret_configured(
        jpdb_config, "apiToken"
    )
    selected = str(selected_plugin or "auto").strip().casefold()
    if selected not in {"auto", "jiten", "jpdb"}:
        selected = "auto"
    jiten_available = bool(jiten_installed and jiten_key)
    # The official jpdb-mpv-plugin authenticates itself. Its account token is
    # deliberately not exported for other applications, so finding a working
    # plugin is sufficient and Pudge must not demand a second token.
    jpdb_available = bool(jpdb_installed)
    effective = selected
    if selected == "auto":
        effective = "jiten" if jiten_available else "jpdb" if jpdb_available else ""
    if effective == "jiten" and not jiten_available:
        effective = ""
    if effective == "jpdb" and not jpdb_available:
        effective = ""
    return {
        "jiten_mpv": {
            "installed": jiten_installed,
            "partial": script_path.is_file() != program_dir.is_dir(),
            "key_configured": jiten_key,
            "available": jiten_available,
            "script_path": str(script_path),
            "config_path": str(config_path),
        },
        "jpdb_mpv": {
            "installed": jpdb_installed,
            "partial": jpdb_dir.is_dir() and not jpdb_installed,
            "key_configured": jpdb_key,
            "auth_managed_by_plugin": jpdb_installed,
            "available": jpdb_available,
            "script_path": str(jpdb_script),
            "config_path": str(jpdb_config),
            "server_path": str(jpdb_server),
            "detected_variants": [str(item["script"]) for item in variants],
        },
        "mpv_study": {
            "selected": selected,
            "effective": effective,
        },
    }


def dependency_status(
    *,
    mpv: str = "mpv",
    ffmpeg: str = "ffmpeg",
    jiten_api_key: str = "",
    jpdb_api_token: str = "",
    selected_plugin: str = "auto",
) -> dict[str, Any]:
    return {
        "mpv": _tool_status("mpv", mpv, "--version"),
        "ffmpeg": _tool_status("ffmpeg", ffmpeg, "-version"),
        "homebrew": {"installed": bool(_brew_path()), "path": _brew_path()},
        **mpv_study_status(
            jiten_api_key=jiten_api_key,
            jpdb_api_token=jpdb_api_token,
            selected_plugin=selected_plugin,
        ),
    }


def install_media_tools(*, mpv: str = "mpv", ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    before = dependency_status(mpv=mpv, ffmpeg=ffmpeg)
    missing = [name for name in ("mpv", "ffmpeg") if not before[name]["installed"]]
    if missing:
        brew = _brew_path()
        if not brew:
            raise FirstExperienceError(
                "Homebrew is required to install mpv and ffmpeg. Install it from brew.sh, then retry."
            )
        try:
            completed = subprocess.run(
                [brew, "install", *missing],
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FirstExperienceError(f"Dependency installation failed: {exc}") from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "Homebrew failed").strip()
            raise FirstExperienceError(detail[-2000:])
    after = dependency_status(mpv=mpv, ffmpeg=ffmpeg)
    if not after["mpv"]["installed"] or not after["ffmpeg"]["installed"]:
        raise FirstExperienceError("mpv or ffmpeg is still unavailable after installation")
    return after


def _write_jiten_api_key(api_key: str) -> None:
    value = str(api_key or "").strip()
    if not value:
        return
    _program_dir, _script_path, config_path = _jiten_paths()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass
    payload["api_key"] = value
    temporary = config_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, config_path)


def _write_jpdb_api_token(api_token: str) -> None:
    value = str(api_token or "").strip()
    if not value:
        return
    plugin_dir, _script_path, _server_path = _jpdb_paths()
    config_path = plugin_dir / "config.json"
    if not plugin_dir.is_dir():
        return
    payload: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass
    payload["apiToken"] = value
    payload.setdefault("miningDeckId", None)
    payload.setdefault("forqOnMine", True)
    payload.setdefault("contextWidth", 1)
    payload.setdefault("serverPort", 9726)
    payload.setdefault("cookiePath", "./jpdb-cookie.txt")
    payload.setdefault("debug", False)
    temporary = config_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, config_path)


def configure_mpv_study_keys(*, jiten_api_key: str = "", jpdb_api_token: str = "") -> None:
    _write_jiten_api_key(jiten_api_key)
    _write_jpdb_api_token(jpdb_api_token)


def mpv_study_script_plan(
    selected_plugin: str,
    *,
    jiten_api_key: str = "",
    jpdb_api_token: str = "",
) -> dict[str, Any]:
    """Return scripts to load while suppressing the competing integration.

    mpv normally auto-loads every script in its config directory. When either
    study integration is installed, Pudge disables that auto-load for its own
    playback and explicitly restores all ordinary user scripts plus at most one
    study plugin. Files on disk are never renamed or deleted.
    """

    status = mpv_study_status(
        jiten_api_key=jiten_api_key,
        jpdb_api_token=jpdb_api_token,
        selected_plugin=selected_plugin,
    )
    jiten_path = Path(str(status["jiten_mpv"]["script_path"]))
    jpdb_path = Path(str(status["jpdb_mpv"]["script_path"]))
    jpdb_variants = [Path(value) for value in status["jpdb_mpv"].get("detected_variants", [])]
    integration_paths = {
        path.resolve(strict=False)
        for path in (jiten_path, jpdb_path, *jpdb_variants)
        if path.is_file()
    }
    if not integration_paths:
        return {"exclusive": False, "selected": "", "scripts": []}

    discovered: list[Path] = []
    for scripts_dir in _mpv_script_roots():
        try:
            entries = sorted(scripts_dir.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            entries = []
        for entry in entries:
            candidate = entry / "main.lua" if entry.is_dir() else entry
            if candidate.is_file() and candidate.suffix.casefold() in {".lua", ".js", ".so", ".dll"}:
                discovered.append(candidate)

    ordinary = [
        path for path in discovered if path.resolve(strict=False) not in integration_paths
    ]
    effective = str(status["mpv_study"]["effective"] or "")
    selected_path = jiten_path if effective == "jiten" else jpdb_path if effective == "jpdb" else None
    if selected_path is not None and selected_path.is_file():
        ordinary.append(selected_path)
    unique: list[str] = []
    seen: set[str] = set()
    for path in ordinary:
        value = str(path)
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return {"exclusive": True, "selected": effective, "scripts": unique}


def install_jiten_mpv(
    api_key: str = "", *, mpv: str = "mpv", ffmpeg: str = "ffmpeg"
) -> dict[str, Any]:
    status = dependency_status(mpv=mpv, ffmpeg=ffmpeg)
    if not status["mpv"]["installed"]:
        raise FirstExperienceError("Install mpv before JitenMPV")
    if not status["jiten_mpv"]["installed"]:
        try:
            response = httpx.get(JITEN_MPV_INSTALLER_URL, timeout=30, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FirstExperienceError(f"Could not download the JitenMPV installer: {exc}") from exc
        source = response.text
        required_markers = ("Sirush/JitenMPV", "sha256", "JITEN_MPV_MPV_CONFIG_DIR")
        if len(source) < 1000 or not all(marker in source for marker in required_markers):
            raise FirstExperienceError("The downloaded JitenMPV installer was not recognized")
        with tempfile.TemporaryDirectory(prefix="pudge-jiten-mpv-") as directory:
            installer = Path(directory) / "install.sh"
            installer.write_text(source, encoding="utf-8")
            env = dict(os.environ)
            env["JITEN_MPV_MPV_CONFIG_DIR"] = str(Path.home() / ".config" / "mpv")
            try:
                completed = subprocess.run(
                    ["/bin/sh", str(installer)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise FirstExperienceError(f"JitenMPV installation failed: {exc}") from exc
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "JitenMPV installer failed").strip()
                raise FirstExperienceError(detail[-2000:])
    _write_jiten_api_key(api_key)
    result = dependency_status(mpv=mpv, ffmpeg=ffmpeg)
    if not result["jiten_mpv"]["installed"]:
        raise FirstExperienceError("JitenMPV did not finish installing")
    return result
