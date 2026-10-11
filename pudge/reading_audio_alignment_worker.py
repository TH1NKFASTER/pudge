"""Light novel <-> audiobook alignment in its own process.

The GUI process hands the inputs over in a JSON file and reads the result from
another one, so the alignment's CPU time and memory peak (tens of seconds and
~180 MB for a long book) stay out of the window process. The anchor chain is
kept in a small side file, so the chapter-start refinement pass (a second run on
the same texts) does not compute it again.

usage: python -m pudge.reading_audio_alignment_worker REQUEST.json RESULT.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _int_keys(value: Any) -> dict[int, Any] | None:
    if not isinstance(value, dict):
        return None
    return {int(key): rows for key, rows in value.items()}


def main(argv: list[str] | None = None) -> int:
    from .process_priority import lower_if_requested

    lower_if_requested()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: reading_audio_alignment_worker REQUEST.json RESULT.json", file=sys.stderr)
        return 2
    request_path, result_path = Path(args[0]), Path(args[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    from . import reading_audio_alignment as alignment_module

    anchor_cache = Path(str(request.get("anchor_cache") or "")) if request.get("anchor_cache") else None
    if anchor_cache is not None and anchor_cache.is_file():
        try:
            alignment_module.load_anchor_cache_rows(json.loads(anchor_cache.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    try:
        result = alignment_module.align_light_novel_to_transcript(
            request["chapters"],
            request["segments"],
            duration=float(request.get("duration") or 0.0),
            model=str(request.get("model") or ""),
            speech_regions=request.get("speech_regions"),
            chapter_start_segments=_int_keys(request.get("chapter_start_segments")),
            chapter_start_reading_hints=_int_keys(request.get("chapter_start_reading_hints")),
        )
        payload: dict[str, Any] = {"ok": True, "alignment": result}
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which shows it on the job
        payload = {"ok": False, "error": str(exc), "type": type(exc).__name__}
    if anchor_cache is not None:
        try:
            temporary = anchor_cache.with_suffix(anchor_cache.suffix + ".tmp")
            temporary.write_text(json.dumps(alignment_module.anchor_cache_rows()), encoding="utf-8")
            temporary.replace(anchor_cache)
        except OSError:
            pass
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
