"""MLX Whisper speech recognition, vendored from mlx-examples whisper 0.4.3 (MIT, Apple; see LICENSE).

Only the STT worker (pudge.subtitles.stt_worker) uses it. Importing this package needs
nothing: ``transcribe`` loads mlx and tiktoken on first use, so the app's own process never
imports MLX, and a Mac without MLX gets an ImportError there and not here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["transcribe"]


def __getattr__(name: str) -> Any:
    if name == "transcribe":
        from .transcribe import transcribe as function

        # Importing the submodule set the package's "transcribe" to that module; the
        # function is what callers want.
        globals()["transcribe"] = function
        return function
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
