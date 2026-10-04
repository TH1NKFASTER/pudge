from __future__ import annotations

import time
import socket
import json
from pathlib import Path
from typing import Any

from .light_novels import LightNovelService
from .subtitle_formats import parse_srt


class SubtitleStudyApi:
    def __init__(self, config: Any, media_id: int | None = None) -> None:
        self.service = LightNovelService(config)
        self.media_id = media_id

    def translate(self, text: str, context: str = "") -> dict[str, Any]:
        return self.service.translate_selection(
            text,
            context,
            media_id=self.media_id,
        )

    def prewarm_file(
        self,
        subtitle_path: Path,
        *,
        start_seconds: float = 0.0,
        delay_seconds: float = 1.25,
        ipc_socket: str | None = None,
        lookahead_seconds: float = 60.0,
        max_cues: int = 8,
    ) -> dict[str, Any]:
        """Lazily fill the exact contextual translation cache for an episode."""
        if not self.service.config.llm.enabled:
            return {"enabled": False, "reason": "local_llm_disabled", "total": 0}
        path = subtitle_path.expanduser().resolve()
        cues = [cue for cue in parse_srt(path) if str(cue[2] or "").strip()]
        if not cues:
            return {"enabled": False, "reason": "no_text_cues", "total": 0}

        start = max(0.0, float(start_seconds or 0.0))
        first = next(
            (
                index
                for index, (_cue_start, cue_end, _text) in enumerate(cues)
                if cue_end >= start
            ),
            0,
        )
        order = [i for i in range(first, min(len(cues), first + max_cues)) if cues[i][0] <= start + lookahead_seconds]
        translated = 0
        cached = 0
        failures = 0
        google_fallbacks = 0
        from .power_policy import PowerPolicy
        import subprocess
        policy = PowerPolicy(manual_enabled=getattr(getattr(self.service.config,"power",None),"manual_energy_saving",False),
                             auto_enabled=getattr(getattr(self.service.config,"power",None),"auto_battery_energy_saving",True),
                             run_command=subprocess.run)
        done = set()
        last_policy_settings = 0.0
        def player_state():
            if not ipc_socket:
                return start, False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(ipc_socket)
                client.sendall(b'{"command":["get_property","time-pos"],"request_id":1}\n{"command":["get_property","pause"],"request_id":2}\n')
                values = {}
                with client.makefile("r") as reader:
                    for _ in range(32):
                        raw = reader.readline()
                        if not raw: raise OSError("Player closed")
                        row = json.loads(raw)
                        if row.get("request_id") in {1,2}:
                            values[row["request_id"]] = row.get("data")
                        if len(values) == 2:
                            return float(values[1] or 0), bool(values[2])
                raise OSError("Player did not return state")
        while True:
            if ipc_socket and time.monotonic()-last_policy_settings>=30:
                last_policy_settings=time.monotonic()
                config_path=getattr(self.service.config,"config_path",None)
                if config_path:
                    from .config import load_config
                    current=load_config(config_path)
                    policy.update_settings(manual_enabled=current.power.manual_energy_saving,
                                           auto_enabled=current.power.auto_battery_energy_saving)
                    if not current.llm.enabled: break
            if not ipc_socket and len(done) >= max_cues: break
            try:
                position, paused = player_state()
            except (OSError, ValueError):
                break
            if paused or policy.background_block_reason():
                if not ipc_socket: break
                time.sleep(2); continue
            order = [i for i in range(len(cues)) if i not in done and cues[i][1] >= position
                     and cues[i][0] <= position + lookahead_seconds][:max_cues]
            if not order:
                if not ipc_socket: break
                time.sleep(2); continue
            index = order[0]
            done.add(index)
            text = str(cues[index][2] or "").strip()
            history = [
                str(item[2] or "").strip()
                for item in cues[max(0, index - 16):index]
            ]
            context = ""
            if history:
                context = "Previous Japanese subtitles:\n" + "\n".join(history)
            try:
                result = self.translate(text, context)
            except Exception:
                failures += 1
                if failures >= 3:
                    break
                continue
            failures = 0
            if bool(result.get("cached")):
                cached += 1
                continue
            translated += 1
            if index > 0 and str(result.get("provider") or "") == "google":
                google_fallbacks += 1
                # If Ollama went away, do not replace a local low-priority job
                # with hundreds of online requests.
                if google_fallbacks >= 3:
                    break
            time.sleep(max(0.25, min(5.0, float(delay_seconds))))
        return {
            "enabled": True,
            "total": len(cues),
            "translated": translated,
            "cached": cached,
            "failures": failures,
            "google_fallbacks": google_fallbacks,
        }
