from __future__ import annotations

import time
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
        order = [*range(first, len(cues)), *range(0, first)]
        translated = 0
        cached = 0
        failures = 0
        google_fallbacks = 0
        for index in order:
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
