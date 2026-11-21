"""A fake Jiten API on a fake clock, for request-budget tests (no network).

It models what Jiten's source (Sirush/Jiten) does to Pudge's calls:

* ``ParseThrottleService``: 200,000 parsed characters a minute per user, at least 1000
  charged per call, a fixed window that starts with the first call after the last one
  ended; over budget: 429 with ``Retry-After`` = the seconds left in the window;
* ``reader/lookup-vocabulary``, ``srs/overview-version``, ``known-ids/amount``,
  ``reader/version``, ``srs/study-batch`` and ``srs/review-history`` with simple answers.

``install(monkeypatch)`` routes ``httpx.get``/``httpx.post`` (which Pudge's Jiten client
uses when they are replaced) here and gives Pudge's Jiten client and the modules that
pace parses this clock.
"""

from __future__ import annotations

import threading
import time as real_time
from types import SimpleNamespace
from typing import Any

import httpx

PARSE_BUDGET = 200_000
PARSE_MINIMUM = 1000
WINDOW = 60.0


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)
        self.lock = threading.Lock()
        self.sleeps: list[float] = []

    def time(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(float(seconds))
            self.now += max(0.0, float(seconds))
        real_time.sleep(0)  # let other threads run

    def module(self) -> SimpleNamespace:
        """A stand-in for the ``time`` module of the code under test."""
        return SimpleNamespace(
            time=self.time,
            monotonic=self.time,
            sleep=self.sleep,
            perf_counter=real_time.perf_counter,
            time_ns=real_time.time_ns,
            strftime=real_time.strftime,
            localtime=real_time.localtime,
            gmtime=real_time.gmtime,
            struct_time=real_time.struct_time,
        )


class FakeJiten:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.log: list[dict[str, Any]] = []
        self.window_start: float | None = None
        self.window_left = PARSE_BUDGET
        self.overview_version = 1
        self.parse_version = "pv-1"
        self.mature = 1500
        self.states: dict[tuple[int, int], list[int]] = {}
        self.fail: dict[str, list[Any]] = {}  # path suffix -> answers/exceptions consumed first
        self.due_cards: list[dict[str, Any]] = []

    # ----------------------------------------------------------- statistics
    def requests(self, path: str = "") -> list[dict[str, Any]]:
        with self.lock:
            return [row for row in self.log if path in row["path"]]

    def count(self, path: str = "") -> int:
        return len(self.requests(path))

    def statuses(self, path: str = "") -> list[int]:
        return [row["status"] for row in self.requests(path)]

    def max_per_window(self, *, chars: bool = False) -> float:
        rows = sorted(self.requests(), key=lambda row: row["at"])
        best = 0.0
        for index, row in enumerate(rows):
            inside = [other for other in rows[index:] if other["at"] < row["at"] + WINDOW]
            value = sum(other["chars"] for other in inside) if chars else len(inside)
            best = max(best, value)
        return best

    def use_parse_budget(self, chars: int) -> None:
        """Another client of the same account (the Jiten site, JitenMPV) parses now."""
        with self.lock:
            self._roll()
            self.window_left -= chars

    # ------------------------------------------------------------ transport
    def _roll(self) -> None:
        now = self.clock.time()
        if self.window_start is None or now - self.window_start >= WINDOW:
            self.window_start = now
            self.window_left = PARSE_BUDGET

    def handle(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        path = str(httpx.URL(url).path).removeprefix("/api/")
        body = kwargs.get("json") or {}
        texts = body.get("text") if isinstance(body, dict) else None
        chars = max(PARSE_MINIMUM, len("".join(texts or []))) if path == "reader/parse" else 0
        with self.lock:
            entry = {"at": self.clock.time(), "method": method, "path": path, "chars": 0, "status": 0}
            self.log.append(entry)
            planned = None
            for suffix, answers in self.fail.items():
                if path.endswith(suffix) and answers:
                    planned = answers.pop(0)
                    break
        if isinstance(planned, Exception):
            entry["status"] = -1
            raise planned
        if isinstance(planned, int):
            entry["status"] = planned
            return httpx.Response(planned, json={}, request=request)
        if path == "reader/parse":
            with self.lock:
                self._roll()
                if self.window_left < chars:
                    left = self.window_start + WINDOW - self.clock.time()
                    entry["status"] = 429
                    return httpx.Response(
                        429, headers={"Retry-After": str(int(left + 0.999))}, json={}, request=request
                    )
                self.window_left -= chars
                entry["chars"] = chars
            entry["status"] = 200
            tokens = [
                [{"wordId": 100 + n % 50, "readingIndex": 0, "start": 0, "end": min(1, len(text))}]
                if text
                else []
                for n, text in enumerate(texts or [])
            ]
            vocabulary = [
                {"wordId": 100 + n, "readingIndex": 0, "spelling": f"w{n}", "knownState": [0]}
                for n in range(50)
            ]
            return httpx.Response(
                200,
                json={"tokens": tokens, "vocabulary": vocabulary, "parseVersion": self.parse_version},
                request=request,
            )
        entry["status"] = 200
        if path == "reader/lookup-vocabulary":
            words = body.get("words") or []
            return httpx.Response(
                200,
                json={
                    "result": [self.states.get((int(w), int(r)), [1]) for w, r in words],
                    "decks": [[7] for _ in words],
                },
                request=request,
            )
        if path == "srs/overview-version":
            return httpx.Response(200, json={"version": self.overview_version}, request=request)
        if path == "user/vocabulary/known-ids/amount":
            return httpx.Response(200, json={"mature": self.mature}, request=request)
        if path == "reader/version":
            return httpx.Response(200, json={"parseVersion": self.parse_version}, request=request)
        if path == "srs/study-batch":
            return httpx.Response(
                200,
                json={"cards": list(self.due_cards), "reviewsRemaining": 0, "sessionId": "s"},
                request=request,
            )
        if path.startswith("srs/review-history/"):
            return httpx.Response(
                200, json={"reviews": [{"rating": 3}], "card": {"state": 1, "due": "x"}}, request=request
            )
        if path.startswith("vocabulary/") and path.endswith("/info"):
            return httpx.Response(
                200,
                json={"readings": [{"reading": "ねこ", "readingIndex": 0}], "meanings": ["cat"]},
                request=request,
            )
        entry["status"] = 404
        return httpx.Response(404, json={}, request=request)


def install(monkeypatch, *, start: float = 1_000_000.0) -> tuple[FakeClock, FakeJiten]:
    """Route Pudge's Jiten requests to a fake Jiten on a fake clock."""
    from pudge import jiten_client, light_novels

    clock = FakeClock(start)
    server = FakeJiten(clock)
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: server.handle("POST", str(url), **kwargs))
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: server.handle("GET", str(url), **kwargs))
    monkeypatch.setattr(jiten_client, "CLIENT", jiten_client.JitenClient(clock=clock.time, sleep=clock.sleep))
    monkeypatch.setattr(light_novels, "time", clock.module())
    return clock, server
