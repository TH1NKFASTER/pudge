from __future__ import annotations

import ipaddress
import logging
import re
import time
import socket
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .light_novels import LightNovelService
from .subtitle_formats import parse_srt

# Japanese lines before a cue that the translation sees, in the player and the prewarm.
HISTORY_CUES = 16
# The prewarm keeps only this many cues ahead of the playback position translated.
PREWARM_WINDOW_CUES = 8


def llm_is_paid(cfg: Any) -> bool:
    """A remote or paid LLM (API key, or a URL outside this Mac / the local network)."""
    if str(getattr(cfg, "api_key", "") or "").strip():
        return True
    host = (urlparse(str(getattr(cfg, "base_url", "") or "")).hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local)


def subtitle_context(cues: list[tuple[float, float, str]], index: int, secondary: str = "") -> str:
    """The context of cue ``index``, in the format of the player's on-demand request."""
    parts: list[str] = []
    history = [str(item[2] or "").strip() for item in cues[max(0, index - HISTORY_CUES) : index]]
    if history:
        parts += ["Previous Japanese subtitles:", "\n".join(history)]
    if re.search(r"\S", str(secondary or "")):
        parts += ["Corresponding English subtitle (supporting context only):", str(secondary)]
    return "\n".join(parts)


def _text_cues(path: Path) -> list[tuple[float, float, str]]:
    return [cue for cue in parse_srt(path.expanduser().resolve()) if str(cue[2] or "").strip()]


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"<[^>]*>|\{[^}]*\}", "", str(text or "")))


def _visible_cue(cues: list[tuple[float, float, str]], text: str, position: float) -> int | None:
    """The cue showing ``text`` nearest to ``position`` (sub-delay tolerant), or None."""
    wanted = _plain(text)
    if not wanted:
        return None
    matches = [index for index, cue in enumerate(cues) if _plain(cue[2]) == wanted]
    if not matches:
        return None
    return min(matches, key=lambda i: max(0.0, cues[i][0] - position, position - cues[i][1]))


class SubtitleStudyApi:
    def __init__(self, config: Any, media_id: int | None = None) -> None:
        self.service = LightNovelService(config)
        self.media_id = media_id
        from .logging_utils import configure_logging

        self.logger = configure_logging()

    def translate(self, text: str, context: str = "") -> dict[str, Any]:
        return self.service.translate_selection(
            text,
            context,
            media_id=self.media_id,
        )

    def translate_visible(
        self,
        text: str,
        context: str = "",
        *,
        subtitle_path: Path | None = None,
        position: float | None = None,
        secondary: str = "",
    ) -> dict[str, Any]:
        """The player's on-demand translation.

        A line of the prepared subtitle file is translated with exactly the text and
        context the prewarm used, so its cached translation is found. Other text
        (another track, overlapping lines) keeps the player's own context.
        """
        if subtitle_path is not None and position is not None:
            try:
                cues = _text_cues(Path(subtitle_path))
                index = _visible_cue(cues, text, float(position))
            except (OSError, ValueError):
                index = None
            if index is not None:
                return self.translate(
                    str(cues[index][2] or "").strip(), subtitle_context(cues, index, secondary)
                )
        return self.translate(text, context)

    def _remote_prewarm_allowed(self) -> bool:
        settings = getattr(self.service, "settings", None)
        try:
            return (
                bool(getattr(settings(), "translation_prewarm_remote", False))
                if callable(settings)
                else False
            )
        except Exception:
            return False

    def prewarm_file(
        self,
        subtitle_path: Path,
        *,
        start_seconds: float = 0.0,
        delay_seconds: float = 1.25,
        ipc_socket: str | None = None,
        window_cues: int = PREWARM_WINDOW_CUES,
        idle_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Translate the next few cues ahead of playback into the on-demand translation cache.

        Only while playing (paused or energy saving: no requests), never with Google,
        with a remote or paid LLM only when the user turned that on. Ends with the
        episode (another file, mpv closed) and logs one summary line.
        """
        logger = getattr(self, "logger", None) or logging.getLogger("pudge")
        llm = self.service.config.llm
        if not llm.enabled:
            return {"enabled": False, "reason": "local_llm_disabled", "total": 0}
        if llm_is_paid(llm) and not self._remote_prewarm_allowed():
            logger.info("SKIP step=subtitle.prewarm reason=remote_llm file=%s", Path(subtitle_path).name)
            return {"enabled": False, "reason": "remote_llm", "total": 0}
        path = subtitle_path.expanduser().resolve()
        cues = _text_cues(path)
        if not cues:
            return {"enabled": False, "reason": "no_text_cues", "total": 0}

        start = max(0.0, float(start_seconds or 0.0))
        window_cues = max(1, int(window_cues))
        translated = 0
        cached = 0
        failures = 0
        from .power_policy import PowerPolicy
        import subprocess

        policy = PowerPolicy(
            manual_enabled=getattr(
                getattr(self.service.config, "power", None), "manual_energy_saving", False
            ),
            auto_enabled=getattr(
                getattr(self.service.config, "power", None), "auto_battery_energy_saving", True
            ),
            run_command=subprocess.run,
        )
        done = set()
        last_policy_settings = 0.0
        config_stamp = None
        episode_path: list[str] = []
        # On-demand translations of this episode, counted by the player script.
        on_demand = {"hits": 0, "misses": 0}
        stop = "window_done"
        names = (
            "time-pos",
            "pause",
            "path",
            "user-data/pudge/translation-hits",
            "user-data/pudge/translation-misses",
        )

        def player_state() -> tuple[float, bool, bool]:
            """(position, paused, same episode) from mpv; OSError when mpv is gone."""
            if not ipc_socket:
                return start, False, True
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(ipc_socket)
                client.sendall(
                    "".join(
                        json.dumps({"command": ["get_property", name], "request_id": number}) + "\n"
                        for number, name in enumerate(names, 1)
                    ).encode("utf-8")
                )
                values: dict[int, Any] = {}
                with client.makefile("r") as reader:
                    for _ in range(64):
                        raw = reader.readline()
                        if not raw:
                            if 1 in values and 2 in values:
                                break
                            raise OSError("Player closed")
                        row = json.loads(raw)
                        if row.get("request_id") in range(1, len(names) + 1):
                            values[row["request_id"]] = row.get("data")
                        if len(values) == len(names):
                            break
                    else:
                        raise OSError("Player did not return state")
            current = values.get(3)
            same = True
            if isinstance(current, str) and current:
                if not episode_path:
                    episode_path.append(current)
                same = current == episode_path[0]
            elif 3 in values and episode_path:
                same = False  # no file any more (idle player)
            if same:
                for number, key in ((4, "hits"), (5, "misses")):
                    try:
                        on_demand[key] = max(on_demand[key], int(values.get(number) or 0))
                    except (TypeError, ValueError):
                        pass
            return float(values.get(1) or 0), bool(values.get(2)), same

        while True:
            if ipc_socket and time.monotonic() - last_policy_settings >= 30:
                last_policy_settings = time.monotonic()
                config_path = getattr(self.service.config, "config_path", None)
                try:
                    stamp = Path(config_path).stat().st_mtime_ns if config_path else None
                except OSError:
                    stamp = None
                # The power and LLM switches live in the config file: reload (keychain
                # reads included) only when it changed.
                if config_path and (stamp is None or stamp != config_stamp):
                    config_stamp = stamp
                    from .config import load_config

                    current = load_config(config_path)
                    policy.update_settings(
                        manual_enabled=current.power.manual_energy_saving,
                        auto_enabled=current.power.auto_battery_energy_saving,
                    )
                    if not current.llm.enabled:
                        stop = "llm_disabled"
                        break
                    if llm_is_paid(current.llm) and not self._remote_prewarm_allowed():
                        stop = "remote_llm"
                        break
            try:
                position, paused, same_episode = player_state()
            except (OSError, ValueError):
                stop = "player_closed"
                break
            if not same_episode:
                stop = "episode_changed"
                break
            if paused or policy.background_block_reason():
                if not ipc_socket:
                    stop = "paused"
                    break
                time.sleep(idle_seconds)
                continue
            first = next((i for i, cue in enumerate(cues) if cue[1] >= position), len(cues))
            todo = [i for i in range(first, min(len(cues), first + window_cues)) if i not in done]
            if not todo:
                if not ipc_socket:
                    break
                time.sleep(idle_seconds)
                continue
            index = todo[0]
            done.add(index)
            text = str(cues[index][2] or "").strip()
            try:
                # The same text and context as the player's on-demand request; never Google.
                result = self.service.translate_selection(
                    text, subtitle_context(cues, index), media_id=self.media_id, google_fallback=False
                )
            except Exception:
                failures += 1
                if failures >= 3:
                    stop = "failures"
                    break
                continue
            failures = 0
            if bool(result.get("cached")):
                cached += 1
                continue
            translated += 1
            time.sleep(max(0.25, min(5.0, float(delay_seconds))))
        logger.info(
            "EVENT subtitle.prewarm_summary file=%s cues=%s prewarmed=%s already_cached=%s "
            "on_demand_hits=%s on_demand_misses=%s stop=%s",
            path.name,
            len(cues),
            translated,
            cached,
            on_demand["hits"],
            on_demand["misses"],
            stop,
        )
        return {
            "enabled": True,
            "total": len(cues),
            "translated": translated,
            "cached": cached,
            "failures": failures,
            "on_demand_hits": on_demand["hits"],
            "on_demand_misses": on_demand["misses"],
            "stop": stop,
        }
