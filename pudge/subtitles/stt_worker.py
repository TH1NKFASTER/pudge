from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path


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


# Where the tqdm reporter writes; a serving worker points it at each request's file.
_PROGRESS_TARGET: list[Path | None] = [None]


def _reset_peak_memory() -> None:
    try:
        import mlx.core as mx  # type: ignore[import-not-found]

        reset = getattr(mx, "reset_peak_memory", None)
        if callable(reset):
            reset()
    except Exception:
        pass


def _install_progress_reporter(progress_path: Path | None) -> None:
    """Expose mlx-whisper's tqdm position to the parent without parsing terminal output."""
    import tqdm

    _PROGRESS_TARGET[0] = progress_path
    if getattr(tqdm.tqdm, "_pudge_progress_reporter", False):
        return
    base = tqdm.tqdm

    class ProgressBar(base):  # type: ignore[misc,valid-type]
        _last_reported = -1

        def _report(self) -> None:
            progress_path = _PROGRESS_TARGET[0]
            if progress_path is None:
                return
            total = max(0, int(self.total or 0))
            current = max(0, int(self.n or 0))
            percent = min(100, round(current / total * 100)) if total else 0
            if percent == self._last_reported:
                return
            self._last_reported = percent
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = progress_path.with_suffix(progress_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {"percent": percent, "current": current, "total": total, "memory": _mlx_memory_snapshot()}
                ),
                encoding="utf-8",
            )
            temporary.replace(progress_path)

        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            self._report()

        def update(self, n=1):  # type: ignore[no-untyped-def]
            result = super().update(n)
            self._report()
            return result

    ProgressBar._pudge_progress_reporter = True
    tqdm.tqdm = ProgressBar


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _serve(model: str) -> int:
    """Transcribe one request per stdin line, keeping the model loaded between them.

    A request is ``{"id", "audio", "output", "progress", "words"}``; each answer is
    one JSON line on the original stdout (anything else the libraries print goes
    to stderr). An empty line or ``{"op": "exit"}`` / end of stdin stops the worker.
    """
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError:
        print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
        return 3
    protocol = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _configure_mlx_memory()
    _install_progress_reporter(None)

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
        _install_progress_reporter(progress_path)
        _reset_peak_memory()
        try:
            result = mlx_whisper.transcribe(
                str(audio),
                path_or_hf_repo=model,
                language="ja",
                word_timestamps=bool(request.get("words", True)),
                verbose=False,
            )
        except Exception as exc:
            snapshot = _mlx_memory_snapshot()
            _release_mlx_memory()
            answer({"id": request_id, "ok": False, "error": f"MLX Whisper failed: {exc}", "memory": snapshot})
            continue
        memory = _mlx_memory_snapshot()
        _release_mlx_memory()  # the buffer cache; the model itself stays loaded
        _write_json_atomic(output, result)
        if progress_path is not None:
            _write_json_atomic(progress_path, {"percent": 100, "current": 1, "total": 1, "memory": memory})
        answer({"id": request_id, "ok": True, "memory": memory})
    return 0


def main(argv: list[str] | None = None) -> int:
    from pudge.process_priority import lower_if_requested

    lower_if_requested()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        try:
            import mlx_whisper  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
            return 3
        return 0
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
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError:
        print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
        return 3
    _configure_mlx_memory()
    if progress_path is not None:
        _install_progress_reporter(progress_path)
    try:
        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=model,
            language="ja",
            word_timestamps=word_timestamps,
            verbose=False,
        )
    except Exception as exc:
        snapshot = _mlx_memory_snapshot()
        _release_mlx_memory()
        print(f"MLX Whisper failed: {exc}; memory={snapshot}", file=sys.stderr)
        return 4
    memory = _mlx_memory_snapshot()
    _release_mlx_memory()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if progress_path is not None:
        progress_path.write_text(
            json.dumps({"percent": 100, "current": 1, "total": 1, "memory": memory}),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
