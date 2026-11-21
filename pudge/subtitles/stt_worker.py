from __future__ import annotations

import gc
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pudge.i18n import err
from pudge.fsutil import atomic_write_json


def _mlx_memory_snapshot() -> dict[str, int]:
    try:
        import mlx.core as mx  # type: ignore[import-not-found]
    except Exception:
        return {}
    result: dict[str, int] = {}
    for key, name in (
        ("active_bytes", "get_active_memory"),
        ("cache_bytes", "get_cache_memory"),
        ("peak_bytes", "get_peak_memory"),
    ):
        fn = getattr(mx, name, None)
        if callable(fn):
            try:
                result[key] = int(fn())
            except Exception:
                pass
    return result


def _configure_mlx_memory() -> None:
    try:
        import mlx.core as mx  # type: ignore[import-not-found]
    except Exception:
        return
    for env_name, fn_name in (
        ("PUDGE_MLX_CACHE_LIMIT_BYTES", "set_cache_limit"),
        ("PUDGE_MLX_MEMORY_LIMIT_BYTES", "set_memory_limit"),
    ):
        fn = getattr(mx, fn_name, None)
        if not callable(fn):
            continue
        try:
            value = int(os.getenv(env_name, "0") or 0)
            if value > 0:
                fn(value)
        except Exception:
            pass
    reset = getattr(mx, "reset_peak_memory", None)
    if callable(reset):
        try:
            reset()
        except Exception:
            pass


def _release_mlx_memory() -> None:
    gc.collect()
    try:
        import mlx.core as mx  # type: ignore[import-not-found]

        clear = getattr(mx, "clear_cache", None)
        if callable(clear):
            clear()
    except Exception:
        pass
    gc.collect()


def _reset_peak_memory() -> None:
    try:
        import mlx.core as mx  # type: ignore[import-not-found]

        reset = getattr(mx, "reset_peak_memory", None)
        if callable(reset):
            reset()
    except Exception:
        pass


def _progress_reporter(progress_path: Path | None) -> Callable[[int, int], None] | None:
    """Whisper's progress (frames done of total) as a JSON file the parent reads.

    It used to patch tqdm.tqdm, whose bar mlx-whisper updated; the vendored Whisper calls
    this callback at the same points with the same running count.
    """
    if progress_path is None:
        return None
    last_reported = [-1]

    def report(current_frames: int, total_frames: int) -> None:
        total = max(0, int(total_frames or 0))
        current = max(0, int(current_frames or 0))
        percent = min(100, round(current / total * 100)) if total else 0
        if percent == last_reported[0]:
            return
        last_reported[0] = percent
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            progress_path,
            {"percent": percent, "current": current, "total": total, "memory": _mlx_memory_snapshot()},
        )

    return report


def _load_transcribe() -> Callable[..., Any] | None:
    """The vendored MLX Whisper, or None when mlx or tiktoken is missing (exit code 3)."""
    try:
        from pudge.whisper_mlx import transcribe
    except ImportError as exc:
        print(f"MLX Whisper needs mlx and tiktoken ({exc}); STT fallback remains disabled", file=sys.stderr)
        return None
    return transcribe


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, ensure_ascii=False)


def _serve(model: str) -> int:
    """Transcribe one request per stdin line, keeping the model loaded between them.

    A request is ``{"id", "audio", "output", "progress", "words"}``; each answer is
    one JSON line on the original stdout (anything else the libraries print goes
    to stderr). An empty line or ``{"op": "exit"}`` / end of stdin stops the worker.
    """
    transcribe = _load_transcribe()
    if transcribe is None:
        return 3
    protocol = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _configure_mlx_memory()

    def answer(payload: dict[str, object]) -> None:
        protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
        protocol.flush()

    answer({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        if request.get("op") == "exit":
            break
        request_id = request.get("id")
        try:
            audio, output = Path(str(request["audio"])), Path(str(request["output"]))
        except KeyError:
            answer({"id": request_id, "ok": False, "error": "bad request"})
            continue
        progress_path = Path(str(request["progress"])) if request.get("progress") else None
        _reset_peak_memory()
        try:
            result = transcribe(
                str(audio),
                path_or_hf_repo=model,
                language="ja",
                word_timestamps=bool(request.get("words", True)),
                verbose=False,
                progress_callback=_progress_reporter(progress_path),
            )
        except Exception as exc:
            snapshot = _mlx_memory_snapshot()
            _release_mlx_memory()
            answer(
                {
                    "id": request_id,
                    "ok": False,
                    "error": err("stt.whisperFailed", error=exc),
                    "memory": snapshot,
                }
            )
            continue
        memory = _mlx_memory_snapshot()
        _release_mlx_memory()  # the buffer cache; the model itself stays loaded
        try:
            _write_json_atomic(output, result)
            if progress_path is not None:
                _write_json_atomic(
                    progress_path, {"percent": 100, "current": 1, "total": 1, "memory": memory}
                )
        except (OSError, TypeError, ValueError) as exc:
            # A full disk (or an unserializable value) fails this request only: the worker
            # used to die here without an answer, and the model was loaded again.
            answer({"id": request_id, "ok": False, "error": f"write failed: {exc}", "memory": memory})
            continue
        answer({"id": request_id, "ok": True, "memory": memory})
    return 0


def main(argv: list[str] | None = None) -> int:
    from pudge.process_priority import lower_if_requested

    lower_if_requested()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        # Loads the vendored Whisper with mlx and tiktoken, as mlx-whisper's import did.
        return 0 if _load_transcribe() is not None else 3
    if len(args) == 2 and args[0] == "--serve":
        return _serve(args[1])
    word_timestamps = bool(args and args[0] == "--words")
    if word_timestamps:
        args = args[1:]
    if len(args) not in {3, 4}:
        print("usage: stt_worker [--words] AUDIO OUTPUT MODEL [PROGRESS]", file=sys.stderr)
        return 2
    audio, output, model = Path(args[0]), Path(args[1]), args[2]
    progress_path = Path(args[3]) if len(args) == 4 else None
    transcribe = _load_transcribe()
    if transcribe is None:
        return 3
    _configure_mlx_memory()
    try:
        result = transcribe(
            str(audio),
            path_or_hf_repo=model,
            language="ja",
            word_timestamps=word_timestamps,
            verbose=False,
            progress_callback=_progress_reporter(progress_path),
        )
    except Exception as exc:
        snapshot = _mlx_memory_snapshot()
        _release_mlx_memory()
        print(f"{err('stt.whisperFailed', error=exc)}; memory={snapshot}", file=sys.stderr)
        return 4
    memory = _mlx_memory_snapshot()
    _release_mlx_memory()
    # Atomic, as in --serve: the parent's timeout can kill this process mid-write, and a
    # half-written transcript must never be read as a result.
    _write_json_atomic(output, result)
    if progress_path is not None:
        _write_json_atomic(progress_path, {"percent": 100, "current": 1, "total": 1, "memory": memory})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
