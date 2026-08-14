from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def build_mpv_command(
    mpv: str,
    video: Path,
    subtitle: Path | None,
    subtitle_id: int | None,
    extra_args: list[str],
    script: Path | None = None,
    ipc_socket: Path | None = None,
) -> list[str]:
    command = [mpv, *extra_args]
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
    if subtitle is not None:
        command.append(f"--sub-file={subtitle}")
    elif subtitle_id is not None:
        command.append(f"--sid={subtitle_id}")
    command.extend(["--", str(video)])
    return command


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
            )
        except OSError:
            return
        if result.returncode == 0:
            return


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

    env = os.environ.copy()
    if env_overrides:
        env.update({key: str(value) for key, value in env_overrides.items()})

    try:
        if not focus:
            completed = subprocess.run(command, env=env, check=False)
            return completed.returncode
        process = subprocess.Popen(command, env=env)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Не найден mpv: {command[0]}") from exc
    _focus_mpv_process(process.pid)
    return process.wait()
