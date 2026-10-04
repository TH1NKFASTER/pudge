from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .runtime import worker_environment


_MPV_OPTION_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}


def mpv_supports_option(mpv: str, option: str) -> bool:
    """Probe optional flags once per executable instead of assuming a build.

    The option list is cached per executable identity (path, size, mtime), so
    playback does not spawn ``mpv --list-options`` every time. A failing probe
    only means "unsupported": it must never prevent playback.
    """
    resolved = shutil.which(mpv) or mpv
    try:
        stat = os.stat(resolved)
        key = (os.path.realpath(resolved), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        key = None
    options = _MPV_OPTION_CACHE.get(key) if key is not None else None
    if options is None:
        try:
            result = subprocess.run(
                [mpv, "--no-config", "--list-options"],
                capture_output=True, text=True, timeout=3.0, check=False,
            )
        except Exception:  # noqa: BLE001 - optional capability probe
            return False
        if getattr(result, "returncode", 1) != 0:
            return False
        options = frozenset(
            parts[0] for parts in (line.split() for line in str(result.stdout or "").splitlines()) if parts
        )
        if key is not None:
            _MPV_OPTION_CACHE[key] = options
    return f"--{option}" in options


def build_mpv_command(
    mpv: str,
    video: Path,
    subtitle: Path | None,
    subtitle_id: int | None,
    extra_args: list[str],
    script: Path | None = None,
    ipc_socket: Path | None = None,
    extra_scripts: list[Path] | None = None,
) -> list[str]:
    command = [mpv, *extra_args]
    # A companion player belongs to Pudge's existing Dock application.
    if (
        sys.platform == "darwin"
        and not any(arg.startswith("--macos-app-activation-policy") for arg in extra_args)
        and mpv_supports_option(mpv, "macos-app-activation-policy")
    ):
        command.append("--macos-app-activation-policy=accessory")
    # A Pudge playback session owns one video and must finish with it. Global
    # mpv profiles must not leave an idle player/helper after its content closes.
    command.append("--idle=no")
    if not any(arg == "--keep-open" or arg.startswith("--keep-open=") or arg == "--no-keep-open" for arg in extra_args):
        command.append("--keep-open=no")
    # mpv can collapse gaps shorter than 210 ms when sub-fix-timing is enabled
    # in the global mpv.conf. That recreates exact boundaries and makes libass
    # briefly retain both SRT cues. Disable it unless pudge arguments
    # explicitly request another value.
    if not any(arg.startswith("--sub-fix-timing") for arg in extra_args):
        command.append("--sub-fix-timing=no")
    # A secondary bitmap subtitle can remain on screen together with the
    # prepared external SRT, especially during startup. Disable secondary
    # subtitles unless the user explicitly overrides these options.
    if not any(arg.startswith("--secondary-sid") for arg in extra_args):
        command.append("--secondary-sid=no")
    if not any(arg.startswith("--secondary-sub-visibility") for arg in extra_args):
        command.append("--secondary-sub-visibility=no")
    if ipc_socket is not None and not any(arg.startswith("--input-ipc-server=") for arg in extra_args):
        command.append(f"--input-ipc-server={ipc_socket}")
    if script is not None and f"--script={script}" not in extra_args:
        # Other explicitly selected scripts (for example JitenMPV) must not
        # suppress Pudge's playback/tracking script.
        command.append(f"--script={script}")
    # Pudge's own companion scripts (e.g. the OP/ED skip button) are loaded
    # explicitly, so they also run under --load-scripts=no.
    for extra in extra_scripts or ():
        if f"--script={extra}" not in command:
            command.append(f"--script={extra}")
    if subtitle is not None:
        command.append(f"--sub-file={subtitle}")
    elif subtitle_id is not None:
        command.append(f"--sid={subtitle_id}")
    command.extend(["--", str(video)])
    return command


def _write_mpv_start_ack(pid: int) -> Path | None:
    raw = os.getenv("PUDGE_MPV_START_ACK", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"pid": int(pid), "started_at": time.time()}),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path
    except OSError:
        return None


def _focus_mpv_process(pid: int) -> None:
    """Bring the newly opened mpv window to the front on macOS.

    Failure is intentionally ignored: fullscreen playback still works when
    Accessibility permission for System Events is unavailable.
    """
    if sys.platform != "darwin":
        return
    script = (
        'tell application "System Events" to set frontmost of first process '
        f'whose unix id is {int(pid)} to true'
    )
    for delay in (0.15, 0.35, 0.7):
        time.sleep(delay)
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=1.5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if result.returncode == 0:
            return


def _observe_decoder(process, ipc_socket, logger):
    import socket
    import threading
    def sample():
        for _ in range(12):
            if process.poll() is not None:
                return
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(ipc_socket)
                    client.sendall(b'{"command":["get_property","hwdec-current"],"request_id":91}\n{"command":["get_property","video-codec"],"request_id":92}\n')
                    values = {}
                    with client.makefile("r") as reader:
                        for _ in range(32):
                            line = reader.readline()
                            if not line: break
                            row = json.loads(line)
                            if row.get("request_id") in {91,92} and row.get("error") == "success":
                                values[row["request_id"]] = row.get("data")
                            if len(values) == 2: break
                    if values.get(92):
                        logger.info("EVENT mpv.decoder pid=%s hwdec=%s codec=%s", process.pid, values.get(91), values[92])
                        return
            except (OSError, ValueError):
                pass
            time.sleep(.5)
    threading.Thread(target=sample,name="mpv-decoder-probe",daemon=True).start()


def run_mpv(
    command: list[str],
    dry_run: bool = False,
    *,
    env_overrides: dict[str, str] | None = None,
    focus: bool = False,
) -> int:
    print("$ " + " ".join(shlex.quote(arg) for arg in command))
    if dry_run:
        return 0

    env = worker_environment(env_overrides)

    try:
        process = subprocess.Popen(command, env=env)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Не найден mpv: {command[0]}") from exc
    ack_path = _write_mpv_start_ack(process.pid)
    try:
        from .logging_utils import configure_logging

        logger = configure_logging()
        logger.info(
            "EVENT mpv.spawned pid=%s ack=%s command=%s",
            process.pid,
            str(ack_path or ""),
            command[0],
        )
    except Exception:
        logger = None
    if focus:
        _focus_mpv_process(process.pid)
    from .playback_process import wait_playback_process
    ipc_socket = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--input-ipc-server=")), None)
    if logger is None:
        return wait_playback_process(process, ipc_socket=ipc_socket)
    if ipc_socket:
        _observe_decoder(process, ipc_socket, logger)
    code = wait_playback_process(process, ipc_socket=ipc_socket)
    logger.info("EVENT mpv.exit pid=%s exit_code=%s", process.pid, code)
    return code
