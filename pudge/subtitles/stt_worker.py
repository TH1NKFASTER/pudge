from __future__ import annotations

import json
import sys
from pathlib import Path


def _install_progress_reporter(progress_path: Path) -> None:
    """Expose mlx-whisper's tqdm position to the parent without parsing terminal output."""
    import tqdm

    base = tqdm.tqdm

    class ProgressBar(base):  # type: ignore[misc,valid-type]
        _last_reported = -1

        def _report(self) -> None:
            total = max(0, int(self.total or 0))
            current = max(0, int(self.n or 0))
            percent = min(100, round(current / total * 100)) if total else 0
            if percent == self._last_reported:
                return
            self._last_reported = percent
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = progress_path.with_suffix(progress_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"percent": percent, "current": current, "total": total}),
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

    tqdm.tqdm = ProgressBar


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        try:
            import mlx_whisper  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
            return 3
        return 0
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
        print(f"MLX Whisper failed: {exc}", file=sys.stderr)
        return 4
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if progress_path is not None:
        progress_path.write_text(
            json.dumps({"percent": 100, "current": 1, "total": 1}),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
