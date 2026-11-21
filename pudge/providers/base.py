from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

# A .torrent file is small; a slow or endless response must not hold a search
# or an add for longer than this in total (httpx timeouts are per read).
TORRENT_DOWNLOAD_DEADLINE_SECONDS = 60.0


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    failures: int = 0
    opened_at: float = 0.0

    def allow(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        if self.opened_at <= 0:
            return True
        if current - self.opened_at >= self.recovery_seconds:
            self.failures = max(0, self.failure_threshold - 1)
            self.opened_at = 0.0
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def failure(self, *, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= max(1, int(self.failure_threshold)):
            self.opened_at = time.monotonic() if now is None else float(now)


class ResponseTooLarge(ValueError):
    """A capped download sent more bytes than allowed."""


def read_capped_body(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    deadline_seconds: float = TORRENT_DOWNLOAD_DEADLINE_SECONDS,
) -> bytes:
    """GET ``url`` and return its body, stopping early when it is too big or too slow.

    The whole body used to be read before the size check, so a huge or endless
    response filled memory and a slow one could hang for minutes.
    """
    deadline = time.monotonic() + max(0.0, float(deadline_seconds))
    with client.stream("GET", url) as response:
        response.raise_for_status()
        declared = str(response.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(f"response is larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"response is larger than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout("download took too long", request=response.request)
            chunks.append(chunk)
    return b"".join(chunks)
