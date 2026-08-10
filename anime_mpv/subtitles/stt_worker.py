from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        try:
            import mlx_whisper  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
            return 3
        return 0
    if len(args) != 3:
        print("usage: stt_worker AUDIO OUTPUT MODEL", file=sys.stderr)
        return 2
    audio, output, model = Path(args[0]), Path(args[1]), args[2]
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError:
        print("mlx-whisper is not installed; STT fallback remains disabled", file=sys.stderr)
        return 3
    try:
        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=model,
            language="ja",
            word_timestamps=False,
            verbose=False,
        )
    except Exception as exc:
        print(f"MLX Whisper failed: {exc}", file=sys.stderr)
        return 4
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
