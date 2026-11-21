"""Explain grammar for paired phones (the light-novel reader and manga bubbles).

The phone sends a sentence with the text before it; the Mac asks its own configured LLM
exactly as its reader's "Explain grammar" and its reading assistant do
(``light_novels.analyze_grammar`` and ``assistant_chat``: the same prompts, which keep the book
text quoted data, the same cache, answers in the Mac's interface language). Nothing is sent
when the LLM is off on the Mac: the phone shows "turn on the LLM in Pudge on the Mac".

An answer can take longer than a phone request waits, and the server is one thread per
connection, so a request starts a job and the phone asks for it again
(``GET media/grammar/jobs/<id>?wait=1``); each request waits up to ``WAIT_SECONDS`` for the
answer, so a quick (cached) one comes back at once. A device has at most
``DEVICE_RUNNING`` questions running and ``WINDOW_REQUESTS`` per ``WINDOW_SECONDS``; a job is
answered only to the device that asked and kept for ``JOB_SECONDS``. The chats are kept on
the phone; the Mac keeps nothing of them.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import grammar_analysis
from .i18n import err, t
from .iphone_media import MediaError, MediaTemporaryError

# A question with its chat (the phone sends at most 20 messages of 4000 characters).
BODY_LIMIT = 384 * 1024
MAX_SENTENCE = grammar_analysis.MAX_SELECTION_CODE_POINTS
MAX_CONTEXT = grammar_analysis.MAX_CONTEXT_CODE_POINTS
MAX_QUESTION = 4000
MAX_MESSAGES = 40
MAX_POINTS = 30
WAIT_SECONDS = 5.0
DEVICE_RUNNING = 2
TOTAL_RUNNING = 4
WINDOW_SECONDS = 600.0
WINDOW_REQUESTS = 30
JOB_SECONDS = 900.0
MAX_JOBS = 64

_REASONS = {
    "empty_selection": "iphone.grammarEmpty",
    "selection_too_long": "iphone.grammarTooLong",
    "llm_disabled": "iphone.grammarLlmOff",
    "llm_unavailable": "iphone.grammarLlmOff",
    "invalid_structure": "iphone.grammarInvalidAnswer",
    "llm_no_json": "iphone.grammarInvalidAnswer",
    "llm_auth": "iphone.grammarLlmKey",
    "llm_rate_limited": "iphone.grammarLlmLimited",
    "llm_service_unavailable": "iphone.grammarLlmDown",
    "empty_message": "iphone.grammarQuestionEmpty",
}


class _Failed(Exception):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass
class _Job:
    id: str
    device: str
    kind: str
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: str = ""
    finished: float = 0.0
    # When the phone last asked for it (started, or polled): a question it stopped asking for
    # holds no place of its own any more (DEVICE_RUNNING).
    seen: float = 0.0


def _clean(value: Any) -> str:
    """Text as the Mac reader sends it: runs of spaces and line breaks are one space."""
    return re.sub(r"\s+", " ", str(value or "")).strip() if isinstance(value, str) else ""


def _reason(reason: Any) -> str:
    key = _REASONS.get(str(reason or ""), "iphone.grammarNoAnswer")
    return err(key, limit=MAX_SENTENCE)


def _language(config: Any) -> str:
    return "ru" if str(getattr(getattr(config, "ui", None), "language", "en")).lower() == "ru" else "en"


def explanation_view(result: dict[str, Any], language: str) -> dict[str, Any]:
    """The grammar analysis for the phone: plain texts only (it renders no HTML), and the
    summary the Mac's assistant gives the LLM as its first answer in a chat."""
    points = []
    for row in result.get("points") or []:
        if not isinstance(row, dict) or not str(row.get("pattern") or "").strip():
            continue
        points.append(
            {
                "pattern": str(row.get("pattern") or "")[:120],
                "meaning": str(row.get("meaning_in_context") or "")[:300],
                "explanation": str(row.get("explanation") or "")[:900],
                "form": str(row.get("form") or "")[:300],
                "certainty": str(row.get("certainty") or "unknown")[:12],
            }
        )
    translation = str(result.get("translation") or "")[:1500]
    if not points:
        summary = (
            t("assistant.translationNoGrammarPoints", lang=language, translation=translation)
            if translation
            else t("reading.noNotableGrammarPoints", lang=language)
        )
    else:
        lines = (
            [t("assistant.translationLine", lang=language, translation=translation)] if translation else []
        )
        lines += [
            f"- {point['pattern']}" + (f" — {point['meaning']}" if point["meaning"] else "")
            for point in points
        ]
        summary = "\n".join(lines)
    return {
        "translation": translation,
        "points": points,
        "partial": result.get("status") == "partial",
        "summary": summary,
    }


class IPhoneGrammar:
    def __init__(self, media: Any) -> None:
        self.media = media
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._requests: dict[str, deque[float]] = {}
        self.clock: Callable[[], float] = time.monotonic
        self.spawn: Callable[[Callable[[], None]], None] = lambda work: threading.Thread(
            target=work, name="pudge-iphone-grammar", daemon=True
        ).start()

    # ------------------------------------------------------------------ routes
    def handle(
        self,
        method: str,
        rest: list[str],
        query: dict[str, list[str]],
        body: Callable[[], dict[str, Any]],
        device: str,
    ) -> dict[str, Any]:
        if method == "POST" and rest == ["explain"]:
            return self.explain(body(), device)
        if method == "POST" and rest == ["ask"]:
            return self.ask(body(), device)
        if method == "GET" and len(rest) == 2 and rest[0] == "jobs":
            return self.job(rest[1], device, wait=query.get("wait", ["0"])[0] == "1")
        raise KeyError("Unknown media operation")

    def explain(self, value: dict[str, Any], device: str) -> dict[str, Any]:
        sentence = _clean(value.get("sentence"))
        if not sentence:
            raise MediaError(err("iphone.grammarEmpty"))
        if len(sentence) > MAX_SENTENCE:
            raise MediaError(err("iphone.grammarTooLong", limit=MAX_SENTENCE))
        context = _clean(value.get("context"))[-MAX_CONTEXT:]
        api = self._ready()

        def work() -> dict[str, Any]:
            result = api.light_novels.analyze_grammar(sentence, context, "")
            if not isinstance(result, dict) or result.get("status") in {"error", "unavailable"}:
                reason = result.get("reason") if isinstance(result, dict) else ""
                detail = str(result.get("detail") or "") if isinstance(result, dict) else ""
                raise _Failed(_reason(reason), detail[:300])
            return {"kind": "explain", "explanation": explanation_view(result, _language(api.config))}

        return self._answer(self._start(device, "explain", work))

    def ask(self, value: dict[str, Any], device: str) -> dict[str, Any]:
        sentence = _clean(value.get("sentence"))
        if len(sentence) > MAX_SENTENCE:
            raise MediaError(err("iphone.grammarTooLong", limit=MAX_SENTENCE))
        context = _clean(value.get("context"))[-MAX_CONTEXT:]
        rows = value.get("messages")
        if not isinstance(rows, list) or not rows:
            raise MediaError(err("iphone.grammarQuestionEmpty"))
        if len(rows) > MAX_MESSAGES:
            raise MediaError(err("iphone.grammarChatTooLong"))
        messages: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
                continue
            content = str(row.get("content") or "").strip() if isinstance(row.get("content"), str) else ""
            if content:
                messages.append({"role": str(row["role"]), "content": content})
        if not messages or messages[-1]["role"] != "user":
            raise MediaError(err("iphone.grammarQuestionEmpty"))
        if len(messages[-1]["content"]) > MAX_QUESTION:
            raise MediaError(err("iphone.grammarQuestionTooLong", limit=MAX_QUESTION))
        points = []
        raw_points = value.get("points")
        for row in (raw_points if isinstance(raw_points, list) else [])[:MAX_POINTS]:
            if isinstance(row, dict) and isinstance(row.get("pattern"), str) and row["pattern"].strip():
                meaning = row.get("meaning") if isinstance(row.get("meaning"), str) else ""
                points.append({"pattern": row["pattern"][:120], "meaning_in_context": meaning[:300]})
        api = self._ready()

        def work() -> dict[str, Any]:
            result = api.light_novels.assistant_chat(messages, sentence, context, {"points": points})
            if not isinstance(result, dict) or not result.get("ok"):
                reason = result.get("reason") if isinstance(result, dict) else ""
                detail = str(result.get("detail") or "") if isinstance(result, dict) else ""
                raise _Failed(_reason(reason), detail[:300])
            return {"kind": "ask", "reply": str(result.get("reply") or "")}

        return self._answer(self._start(device, "ask", work))

    def job(self, job_id: str, device: str, *, wait: bool = False) -> dict[str, Any]:
        with self._lock:
            self._prune(self.clock())
            job = self._jobs.get(job_id)
        # Another device's job is not found either: ids are never shared between devices.
        if job is None or job.device != device:
            raise MediaError(err("iphone.grammarJobGone"))
        job.seen = self.clock()
        if wait:
            job.done.wait(WAIT_SECONDS)
            job.seen = self.clock()
        return self._view(job)

    # ------------------------------------------------------------------ jobs
    def _ready(self) -> Any:
        """The Mac's API with its LLM set up (as for the Mac reader); refused (HTTP 422) otherwise."""
        api = self.media.api
        from .mpv_assistant import llm_configured

        config = getattr(api, "config", None)
        if config is None or not llm_configured(config):
            raise MediaError(err("iphone.grammarLlmOff"))
        return api

    def _start(self, device: str, kind: str, work: Callable[[], dict[str, Any]]) -> _Job:
        with self._lock:
            now = self.clock()
            self._prune(now)
            running = [job for job in self._jobs.values() if not job.done.is_set()]
            # A device's place counts only for questions it still asks for (polled within the
            # last wait and 15 s): a phone that gave up on a slow LLM can ask again. All of them
            # still count toward TOTAL_RUNNING (they keep the LLM busy).
            asked = [job for job in running if job.device == device and now - job.seen <= WAIT_SECONDS + 15]
            if len(asked) >= DEVICE_RUNNING or len(running) >= TOTAL_RUNNING:
                raise MediaTemporaryError(err("iphone.grammarBusy"))
            window = self._requests.setdefault(device, deque())
            while window and now - window[0] >= WINDOW_SECONDS:
                window.popleft()
            if len(window) >= WINDOW_REQUESTS:
                raise MediaTemporaryError(err("iphone.grammarRateLimited", minutes=int(WINDOW_SECONDS // 60)))
            window.append(now)
            job = _Job(id=secrets.token_urlsafe(16), device=device, kind=kind, started=now, seen=now)
            self._jobs[job.id] = job
        try:
            self.spawn(lambda: self._run(job, work))
        except Exception as exc:  # noqa: BLE001 - no thread (shutting down, too many threads)
            self._log("IPHONE grammar %s not started: %s: %s", job.kind, type(exc).__name__, str(exc)[:300])
            job.error = err("iphone.grammarNoAnswer")
            job.finished = self.clock()
            job.done.set()
        return job

    def _log(self, message: str, *args: Any) -> None:
        logger = getattr(getattr(self.media.service, "api", None), "logger", None)
        if logger is not None:
            logger.warning(message, *args)

    def _run(self, job: _Job, work: Callable[[], dict[str, Any]]) -> None:
        try:
            job.result = work()
        except _Failed as exc:
            # The provider's own error stays in the Mac's log; the phone gets the readable reason.
            if exc.detail:
                self._log("IPHONE grammar %s failed: %s", job.kind, exc.detail[:300])
            job.error = exc.message
        except MediaError as exc:
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the phone gets a readable reason, the log the cause
            self._log("IPHONE grammar %s failed: %s: %s", job.kind, type(exc).__name__, str(exc)[:300])
            job.error = err("iphone.grammarNoAnswer")
        finally:
            job.finished = self.clock()
            job.done.set()

    def _answer(self, job: _Job) -> dict[str, Any]:
        job.done.wait(WAIT_SECONDS)
        return self._view(job)

    @staticmethod
    def _view(job: _Job) -> dict[str, Any]:
        if not job.done.is_set():
            return {"job": job.id, "state": "running"}
        if job.error:
            return {"job": job.id, "state": "failed", "error": job.error, "detail": ""}
        return {"job": job.id, "state": "done", **(job.result or {})}

    def _prune(self, now: float) -> None:
        """Answers older than JOB_SECONDS go, and the oldest answers past MAX_JOBS (never a running one)."""
        finished = sorted(
            (job for job in self._jobs.values() if job.done.is_set()), key=lambda job: job.finished
        )
        for index, job in enumerate(finished):
            if now - job.finished >= JOB_SECONDS or len(finished) - index > MAX_JOBS:
                self._jobs.pop(job.id, None)
        for device in [
            key for key, window in self._requests.items() if not window or now - window[-1] >= WINDOW_SECONDS
        ]:
            self._requests.pop(device, None)
