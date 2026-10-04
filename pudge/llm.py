from __future__ import annotations

import hashlib
import json
import math
import uuid
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
import statistics

import httpx

from .config import LLMConfig
from .models import AniListAnime, SubtitleCandidate, VideoIdentity
from .logging_utils import configure_logging, timed_step
from .subtitle_formats import parse_srt


# (base_url, model) -> what that model rejected, learned during this process.
_MODEL_ADAPTATIONS: dict[tuple[str, str], dict[str, Any]] = {}
SEMANTIC_CACHE_SCHEMA = "semantic-v5-strict-evidence"
SEMANTIC_CACHE_ACCEPTED_TTL_SECONDS = 30 * 24 * 3600
SEMANTIC_CACHE_REJECTED_TTL_SECONDS = 6 * 3600
OPENAI_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def _openai_reasoning_effort(value: Any) -> str:
    effort = str(value or "low").strip().lower()
    return effort if effort in OPENAI_REASONING_EFFORTS else "low"


def _semantic_evidence_valid(result: Any, total: int) -> bool:
    """Validate the provider's evidence without coercing strings or booleans."""
    def score(value: Any) -> bool:
        return type(value) in {int, float} and 0 <= value <= 1 and math.isfinite(value)

    if not isinstance(result, dict):
        return False
    scores = result.get("sample_scores")
    return bool(
        type(result.get("same_episode")) is bool
        and type(result.get("usable_for_timing")) is bool
        and score(result.get("similarity"))
        and type(result.get("matched_samples")) is int
        and 0 <= result["matched_samples"] <= total
        and type(result.get("total_samples")) is int
        and result["total_samples"] == total
        and isinstance(scores, list)
        and len(scores) == total
        and all(score(value) for value in scores)
    )


def build_chat_payload(config: LLMConfig, system: str, user: str) -> dict[str, Any]:
    return {
        "model": config.model,
        "stream": False,
        "think": config.think,
        "keep_alive": config.keep_alive,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": config.temperature,
            "num_ctx": config.num_ctx,
        },
    }


def build_openai_chat_payload(config: LLMConfig, system: str, user: str) -> dict[str, Any]:
    """Build an OpenAI-compatible chat payload with optional reasoning.

    GPT-5-class models reject sampling controls such as ``temperature`` when
    reasoning is enabled. Pudge defaults OpenAI-compatible providers to ``low``
    reasoning, so only send temperature when reasoning is explicitly ``none``.
    Gateways that do not understand ``reasoning_effort`` are retried once by
    :class:`OllamaClient` without that field.
    """
    effort = _openai_reasoning_effort(getattr(config, "reasoning_effort", "low"))
    payload: dict[str, Any] = {
        "model": config.model,
        "stream": False,
        "reasoning_effort": effort,
        # Every json_chat caller needs a JSON object; ask the API for it instead
        # of relying on prompt wording alone (retried without it by gateways
        # that reject the field).
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if effort == "none":
        payload["temperature"] = config.temperature
    return payload


def _response_error_detail(response: Any) -> str:
    """Return a useful provider error without leaking request credentials."""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or "").strip()
            if message and code:
                return f"{message} (code={code})"
            if message:
                return message
        detail = payload.get("detail")
        if detail:
            return str(detail).strip()
    try:
        text = str(response.text or "").strip()
    except Exception:
        text = ""
    return text[:1200]


def _reasoning_parameter_unsupported(response: Any) -> bool:
    return _parameter_unsupported(response, ("reasoning_effort", "reasoning effort"))


def _response_format_unsupported(response: Any) -> bool:
    names = ("response_format", "response format", "json_object")
    if _parameter_unsupported(response, names):
        return True
    if getattr(response, "status_code", None) not in {400, 404, 422}:
        return False
    detail = _response_error_detail(response).casefold()
    # LM Studio validates this as an enum instead of calling it unsupported.
    return (
        any(name in detail for name in names)
        and "json_schema" in detail and "text" in detail
        and any(marker in detail for marker in ("must be", "should be", "expected", "literal_error"))
    )


def _parameter_unsupported(response: Any, names: tuple[str, ...]) -> bool:
    try:
        status = int(response.status_code)
    except Exception:
        return False
    if status not in {400, 404, 422}:
        return False
    detail = _response_error_detail(response).casefold()
    if not any(name in detail for name in names):
        return False
    return any(
        marker in detail
        for marker in (
            "unsupported",
            "not supported",
            "unknown",
            "unrecognized",
            "unexpected",
            "extra",
            "invalid parameter",
        )
    )


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _openai_url(base_url: str, endpoint: str) -> str:
    """Accept both provider roots and OpenAI-style base URLs ending in /v1."""
    base = str(base_url or "").strip().rstrip("/")
    suffix = str(endpoint or "").strip().lstrip("/")
    if base.casefold().endswith("/v1"):
        return f"{base}/{suffix}"
    return f"{base}/v1/{suffix}"


def _decode_json_content(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    if not text:
        return None
    fenced = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced).strip()
    for candidate in (text, fenced):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    left, right = fenced.find("{"), fenced.rfind("}")
    if 0 <= left < right:
        try:
            value = json.loads(fenced[left : right + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def list_models(
    base_url: str,
    api_key: str = "",
    timeout: float = 8.0,
    provider: str = "ollama",
) -> list[str]:
    kind = str(provider or "ollama").strip().lower()
    endpoint = _openai_url(base_url, "models") if kind == "openai" else f"{base_url.rstrip('/')}/api/tags"
    response = httpx.get(
        endpoint,
        headers=_headers(api_key),
        timeout=timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    models = (
        payload.get("data", [])
        if kind == "openai" and isinstance(payload, dict)
        else payload.get("models", []) if isinstance(payload, dict) else []
    )
    result = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("id") if kind == "openai" else item.get("name")
        if name:
            result.append(str(name))
    return sorted(set(result), key=str.casefold)


def build_subtitle_semantic_samples(
    japanese_path: Path,
    english_path: Path,
    *,
    sample_count: int = 6,
    phrases_per_sample: int = 4,
    alignment_mode: str = "timestamp",
) -> list[dict[str, Any]]:
    """Build small bilingual excerpts for semantic episode verification.

    ``timestamp`` expects a roughly aligned Japanese file and samples both
    languages around the same clock positions. ``relative`` is a fallback for
    cases where audio alignment failed: it samples matching relative cue
    positions, preserving narrative order without trusting broken timestamps.
    """
    japanese = parse_srt(japanese_path)
    english = parse_srt(english_path)
    if not japanese or not english:
        return []

    sample_count = min(20, max(2, int(sample_count)))
    phrases_per_sample = min(8, max(1, int(phrases_per_sample)))
    mode = alignment_mode.casefold().strip()
    if mode not in {"timestamp", "relative"}:
        mode = "timestamp"

    # Semantic verification should focus on dialogue-bearing interior scenes.
    # Very early title cards/recaps and late credits are valid timing cues but
    # poor evidence for deciding whether two translated tracks match.
    fractions = (
        [0.12, 0.88]
        if sample_count == 2
        else [0.10 + 0.80 * index / (sample_count - 1) for index in range(sample_count)]
    )

    def nearest_time(
        cues: list[tuple[float, float, str]], center: float
    ) -> list[tuple[float, float, str]]:
        ranked = sorted(
            cues,
            key=lambda cue: (
                0.0
                if cue[0] <= center <= cue[1]
                else min(abs(center - cue[0]), abs(center - cue[1])),
                abs(((cue[0] + cue[1]) / 2.0) - center),
            ),
        )[:phrases_per_sample]
        return sorted(ranked, key=lambda cue: cue[0])

    def nearest_relative(
        cues: list[tuple[float, float, str]], fraction: float
    ) -> list[tuple[float, float, str]]:
        if not cues:
            return []
        center = int(round((len(cues) - 1) * fraction))
        left = max(0, center - phrases_per_sample // 2)
        right = min(len(cues), left + phrases_per_sample)
        left = max(0, right - phrases_per_sample)
        return cues[left:right]

    samples: list[dict[str, Any]] = []
    seen: set[tuple[tuple[float, str], tuple[float, str]]] = set()
    if mode == "timestamp":
        duration = min(max(item[1] for item in japanese), max(item[1] for item in english))
        if duration <= 0:
            return []
        selectors = [(fraction, duration * fraction) for fraction in fractions]
    else:
        selectors = [(fraction, None) for fraction in fractions]

    for index, (fraction, center) in enumerate(selectors, start=1):
        if mode == "relative":
            ja = nearest_relative(japanese, fraction)
            en = nearest_relative(english, fraction)
        else:
            assert center is not None
            ja = nearest_time(japanese, center)
            en = nearest_time(english, center)
        if not ja or not en:
            continue
        signature = (
            tuple((round(item[0], 1), item[2]) for item in ja),
            tuple((round(item[0], 1), item[2]) for item in en),
        )
        if signature in seen:
            continue
        seen.add(signature)
        samples.append(
            {
                "sample": index,
                "alignment_mode": mode,
                "relative_position": round(fraction, 4),
                "time_seconds": round(center, 1) if center is not None else None,
                "japanese": [item[2][:400] for item in ja],
                "english": [item[2][:400] for item in en],
            }
        )
    return samples


class OllamaClient:
    def __init__(self, config: LLMConfig, cache_dir: Path | None = None) -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.logger = configure_logging()
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.last_error = ""
        self.client = httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers=_headers(config.api_key),
        )

    def close(self) -> None:
        self.client.close()

    def available(self) -> bool:
        try:
            provider = str(getattr(self.config, "provider", "ollama") or "ollama").lower()
            endpoint = _openai_url(self.base_url, "models") if provider == "openai" else f"{self.base_url}/api/tags"
            response = self.client.get(endpoint)
            if not response.is_success:
                # /v1/models is common but not mandatory for compatible gateways.
                # A reachable 404/405 should not disable chat completions globally.
                if provider == "openai" and int(response.status_code) in {404, 405}:
                    return bool(self.model)
                return False
            payload = response.json()
            rows = payload.get("data", []) if provider == "openai" else payload.get("models", [])
            field = "id" if provider == "openai" else "name"
            names = {str(item.get(field)) for item in rows if isinstance(item, dict) and item.get(field)}
            return not names or self.model in names
        except (httpx.HTTPError, TypeError, ValueError):
            return False

    # Optional per-call overrides (set by callers such as the reading assistant).
    effort_override: str = ""
    operation: str = ""

    def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
    ) -> tuple[str, dict[str, Any]]:
        """One logical LLM call -> (content, meta). Raises httpx/KeyError etc."""
        provider = str(getattr(self.config, "provider", "ollama") or "ollama").lower()
        meta: dict[str, Any] = {"provider": provider, "attempts": 0, "finish_reason": "", "refusal": ""}
        if provider == "openai":
            endpoint = _openai_url(self.base_url, "chat/completions")
            payload = build_openai_chat_payload(self.config, "", "")
            payload["messages"] = messages
            effort = _openai_reasoning_effort(self.effort_override) if self.effort_override else ""
            if effort:
                payload["reasoning_effort"] = effort
                if effort == "none":
                    payload["temperature"] = self.config.temperature
                else:
                    payload.pop("temperature", None)
            if not json_mode:
                payload.pop("response_format", None)
            # Route requests that share a prompt prefix (same operation) to the
            # same cache shard; OpenAI caches prompts >= 1024 tokens.
            payload["prompt_cache_key"] = f"pudge-{self.operation or 'json'}"
            response = self._post_openai_with_fallbacks(endpoint, payload, meta)
        else:
            payload = build_chat_payload(self.config, "", "")
            payload["messages"] = messages
            if not json_mode:
                payload.pop("format", None)
            meta["attempts"] = 1
            response = self.client.post(f"{self.base_url}/api/chat", json=payload)
        meta["http_status"] = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", None) or {}
        try:
            meta["request_id"] = str(headers.get("x-request-id") or headers.get("request-id") or "")
        except Exception:
            meta["request_id"] = ""
        response.raise_for_status()
        body = response.json()
        if provider == "openai":
            choice = body["choices"][0]
            meta["finish_reason"] = str(choice.get("finish_reason") or "") if isinstance(choice, dict) else ""
            message = choice["message"]
            meta["refusal"] = str(message.get("refusal") or "") if isinstance(message, dict) else ""
            content = message["content"]
            usage = body.get("usage") if isinstance(body, dict) else None
            if isinstance(usage, dict):
                meta["prompt_tokens"] = usage.get("prompt_tokens")
                meta["completion_tokens"] = usage.get("completion_tokens")
                details = usage.get("completion_tokens_details") or {}
                if isinstance(details, dict):
                    meta["reasoning_tokens"] = details.get("reasoning_tokens")
                prompt_details = usage.get("prompt_tokens_details") or {}
                if isinstance(prompt_details, dict):
                    meta["cached_tokens"] = prompt_details.get("cached_tokens")
        else:
            content = body["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content or ""), meta

    def _log_call(self, call_id: str, started: float, meta: dict[str, Any], *, ok: bool, request_chars: int) -> None:
        fields = " ".join(
            f"{key}={meta.get(key)}"
            for key in ("provider", "attempts", "effort", "http_status", "finish_reason", "prompt_tokens",
                        "completion_tokens", "reasoning_tokens", "cached_tokens", "request_id")
            if meta.get(key) not in (None, "")
        )
        self.logger.info(
            "RESULT step=llm.call id=%s operation=%s model=%s ok=%s total_ms=%.1f request_chars=%s %s",
            call_id, self.operation or "json", self.model, ok, (time.perf_counter() - started) * 1000.0, request_chars, fields,
        )

    def _json_chat(self, system: str, user: str) -> dict[str, Any] | None:
        call_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        meta: dict[str, Any] = {}
        try:
            with timed_step(
                self.logger,
                "llm.chat",
                model=self.model,
                request_chars=len(user),
                call_id=call_id,
                operation=self.operation or "json",
            ):
                content, meta = self._complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    json_mode=True,
                )
                parse_started = time.perf_counter()
                decoded = _decode_json_content(content)
                meta["parse_ms"] = round((time.perf_counter() - parse_started) * 1000.0, 1)
                self.last_error = ""
                finish_reason = str(meta.get("finish_reason") or "")
                if decoded is None:
                    if meta.get("refusal"):
                        self.last_error = f"LLM refused: {str(meta['refusal'])[:200]}"
                    elif not content.strip():
                        self.last_error = f"LLM returned empty content (finish_reason={finish_reason or 'unknown'})"
                    else:
                        self.last_error = (
                            "LLM returned non-JSON content"
                            + (f" (finish_reason={finish_reason})" if finish_reason and finish_reason != "stop" else "")
                        )
                    # The HTTP call succeeded; make the decode failure visible
                    # instead of a bare DONE step=llm.chat.
                    self.logger.warning(
                        "FAIL step=llm.decode id=%s model=%s finish_reason=%s error=%r content_prefix=%r",
                        call_id, self.model, finish_reason, self.last_error, content[:600],
                    )
                self._log_call(call_id, started, meta, ok=decoded is not None, request_chars=len(user))
                return decoded
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                detail = _response_error_detail(exc.response)
                status = getattr(exc.response, "status_code", None)
                self.last_error = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
            else:
                self.last_error = str(exc).strip() or exc.__class__.__name__
            self._log_call(call_id, started, {**meta, "error": self.last_error}, ok=False, request_chars=len(user))
            return None

    def chat_text(self, messages: list[dict[str, str]]) -> str | None:
        """Free-text chat completion (reading assistant). None on failure; see last_error."""
        call_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        meta: dict[str, Any] = {}
        try:
            content, meta = self._complete(messages, json_mode=False)
            self.last_error = ""
            if not content.strip():
                self.last_error = f"LLM returned empty content (finish_reason={meta.get('finish_reason') or 'unknown'})"
            self._log_call(call_id, started, meta, ok=bool(content.strip()), request_chars=chars)
            return content if content.strip() else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                detail = _response_error_detail(exc.response)
                status = getattr(exc.response, "status_code", None)
                self.last_error = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
            else:
                self.last_error = str(exc).strip() or exc.__class__.__name__
            self._log_call(call_id, started, {**meta, "error": self.last_error}, ok=False, request_chars=chars)
            return None

    _EFFORT_LADDER = ("max", "xhigh", "high", "medium", "low")

    def _post_openai_with_fallbacks(self, endpoint: str, payload: dict[str, Any], meta: dict[str, Any] | None = None) -> Any:
        """POST, adapting to what this model/gateway rejects (bounded retries).

        * ``response_format`` unsupported -> drop it;
        * an unsupported ``reasoning_effort`` *value* (e.g. ``max``) -> step
          down the effort ladder instead of dropping reasoning;
        * ``reasoning_effort`` unsupported as a parameter -> drop it and use
          the configured temperature (legacy gateways);
        * ``temperature`` unsupported (reasoning models accept only the
          default) -> drop it.

        What a model rejected is remembered per (base URL, model) for the
        process, so later calls do not repeat the same failing requests.
        """
        meta = meta if meta is not None else {}
        memory_key = (self.base_url, self.model)
        learned = _MODEL_ADAPTATIONS.setdefault(memory_key, {"drop": set(), "effort": ""})
        payload = dict(payload)
        # Copy the set atomically before iteration: concurrent requests can
        # learn another rejected parameter while this payload is prepared.
        for name in learned["drop"].copy():
            payload.pop(name, None)
        if learned["effort"] and "reasoning_effort" in payload:
            ladder = self._EFFORT_LADDER
            current = str(payload.get("reasoning_effort") or "")
            if current in ladder and ladder.index(current) < ladder.index(learned["effort"]):
                payload["reasoning_effort"] = learned["effort"]
        attempts = 1
        response = self.client.post(endpoint, json=payload)
        for _attempt in range(6):
            detail = _response_error_detail(response).casefold() if int(getattr(response, "status_code", 200) or 200) in {400, 404, 422} else ""
            if not detail:
                break
            if "prompt_cache_key" in payload and _parameter_unsupported(response, ("prompt_cache_key",)):
                payload.pop("prompt_cache_key", None)
                learned["drop"].add("prompt_cache_key")
                self.logger.info("FALLBACK step=llm.prompt_cache_key model=%s reason=unsupported", self.model)
            elif "response_format" in payload and _response_format_unsupported(response):
                payload.pop("response_format", None)
                learned["drop"].add("response_format")
                self.logger.info("FALLBACK step=llm.response_format model=%s reason=unsupported", self.model)
            elif "temperature" in payload and _parameter_unsupported(response, ("temperature",)):
                payload.pop("temperature", None)
                learned["drop"].add("temperature")
                self.logger.info("FALLBACK step=llm.temperature model=%s reason=unsupported", self.model)
            elif "reasoning_effort" in payload and _reasoning_parameter_unsupported(response):
                effort = str(payload.get("reasoning_effort") or "")
                if "value" in detail and effort in self._EFFORT_LADDER and effort != "low":
                    lower = self._EFFORT_LADDER[self._EFFORT_LADDER.index(effort) + 1]
                    payload["reasoning_effort"] = lower
                    learned["effort"] = lower
                    self.logger.info("FALLBACK step=llm.reasoning_effort model=%s from=%s to=%s", self.model, effort, lower)
                else:
                    payload.pop("reasoning_effort", None)
                    learned["drop"].add("reasoning_effort")
                    if "temperature" not in learned["drop"]:
                        payload.setdefault("temperature", self.config.temperature)
                    self.logger.info("FALLBACK step=llm.reasoning_effort model=%s reason=unsupported_parameter", self.model)
            else:
                break
            attempts += 1
            response = self.client.post(endpoint, json=payload)
        meta["attempts"] = attempts
        meta["effort"] = str(payload.get("reasoning_effort") or "")
        return response

    def json_chat(self, system: str, user: str) -> dict[str, Any] | None:
        return self._json_chat(system, user)


    def compare_subtitle_semantics(
        self,
        japanese_path: Path,
        english_path: Path,
        *,
        sample_count: int | None = None,
        phrases_per_sample: int | None = None,
        min_similarity: float | None = None,
        alignment_mode: str = "timestamp",
        force: bool = False,
    ) -> dict[str, Any]:
        """Check that an embedded translation and Japanese subtitle cover the same scenes."""
        effective_sample_count = sample_count or self.config.embedded_reference_sample_count
        effective_phrases = phrases_per_sample or self.config.embedded_reference_phrases_per_sample
        effective_threshold = float(
            self.config.embedded_reference_min_similarity
            if min_similarity is None
            else min_similarity
        )
        cache_path: Path | None = None
        if self.cache_dir is not None:
            try:
                ja_stat = japanese_path.stat()
                en_stat = english_path.stat()
                raw = (
                    f"{SEMANTIC_CACHE_SCHEMA}:{self.model}:{self.config.think}:{self.config.temperature}:"
                    f"{getattr(self.config, 'reasoning_effort', 'low')}:"
                    f"{self.config.num_ctx}:{effective_sample_count}:{effective_phrases}:"
                    f"{effective_threshold}:{alignment_mode}:"
                    f"{japanese_path.resolve()}:{ja_stat.st_size}:{ja_stat.st_mtime_ns}:"
                    f"{english_path.resolve()}:{en_stat.st_size}:{en_stat.st_mtime_ns}"
                )
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
                cache_path = self.cache_dir / "llm-semantic" / f"{digest}.json"
                if not force and cache_path.is_file():
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        result = dict(payload.get("result") or {})
                        ttl = (
                            SEMANTIC_CACHE_ACCEPTED_TTL_SECONDS
                            if bool(result.get("accepted"))
                            else SEMANTIC_CACHE_REJECTED_TTL_SECONDS
                        )
                        age = time.time() - float(payload.get("cached_at") or 0)
                        total = result.get("total_samples")
                        cached_scores = result.get("sample_scores")
                        cache_valid = (
                            type(total) is int and total >= 2
                            and _semantic_evidence_valid(result, total)
                            and type(result.get("accepted")) is bool
                            and (not result["accepted"] or (
                                sum(score >= 0.55 for score in cached_scores) >= total - int(total >= 5)
                                and statistics.median(sorted(cached_scores, reverse=True)[:total - int(total >= 5)]) >= effective_threshold
                                and result["similarity"] >= effective_threshold
                            ))
                        )
                        if cache_valid and age < ttl:
                            result["cached"] = True
                            self.logger.info(
                                "RESULT step=llm.semantic cache=hit japanese=%s english=%s accepted=%s",
                                japanese_path.name,
                                english_path.name,
                                result.get("accepted"),
                            )
                            return result
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                cache_path = None

        samples = build_subtitle_semantic_samples(
            japanese_path,
            english_path,
            sample_count=effective_sample_count,
            phrases_per_sample=effective_phrases,
            alignment_mode=alignment_mode,
        )
        threshold = effective_threshold
        if len(samples) < 2:
            return {
                "accepted": False,
                "reason": "insufficient_samples",
                "similarity": 0.0,
                "matched_samples": 0,
                "total_samples": len(samples),
            }

        sampling_description = (
            "matching relative positions in narrative order"
            if alignment_mode.casefold().strip() == "relative"
            else "matching timestamps"
        )
        system = (
            f"Compare Japanese and English subtitle excerpts sampled at {sampling_description}. "
            "Judge each paired sample independently by concrete speakers, actions, names, and dialogue meaning. "
            "Never identify, guess, or mention a series/movie title or franchise in the reasoning; title guesses are "
            "not evidence. Decide only whether each Japanese excerpt can plausibly translate the paired non-Japanese "
            "excerpt at the same scene, then aggregate those pairwise judgements. Decide whether the tracks belong "
            "to the same episode and describe the "
            "same scenes/dialogue. Normal translation differences, omitted honorifics, nearby "
            "context, sound-effect labels, songs, signs, or a few extra accessibility cues are "
            "acceptable when the core scene is the same. Reject unrelated episodes, recaps, "
            "commentary tracks, or references whose dialogue is mostly about different events. "
            "A single mismatching title card, song, sign, recap caption, or credits sample must be treated as an "
            "outlier when all other samples strongly match; do not reject the whole track for that alone. "
            "Return strict JSON with keys: same_episode (boolean), usable_for_timing (boolean), "
            "similarity (0..1), matched_samples (integer), total_samples (integer), "
            "sample_scores (array containing exactly one 0..1 score for every input sample, in order), "
            "reason (short string)."
        )
        result = self._json_chat(
            system,
            json.dumps({"samples": samples}, ensure_ascii=False),
        )
        if not result:
            return {
                "accepted": False,
                "reason": "llm_request_failed",
                "similarity": 0.0,
                "matched_samples": 0,
                "total_samples": len(samples),
            }
        if not _semantic_evidence_valid(result, len(samples)):
            return {
                "accepted": False,
                "reason": "invalid_llm_response",
                "similarity": 0.0,
                "matched_samples": 0,
                "total_samples": len(samples),
            }
        similarity = float(result["similarity"])
        same_episode = result["same_episode"]
        usable = result["usable_for_timing"]
        matched = result["matched_samples"]
        sample_scores = [float(value) for value in result["sample_scores"]]

        # Treat the per-sample score vector as the auditable evidence. Some local
        # models produce internally contradictory aggregate fields (for example,
        # reason="samples 2-6 strongly align" while reporting similarity=0.15 and
        # matched_samples=2/6). A complete score vector lets us reconcile that
        # contradiction deterministically; timing activity remains a separate gate.
        robust_accepted = False
        robust_similarity = 0.0
        robust_matches = 0
        required_matches = len(samples)
        consensus_kind = ""
        if len(sample_scores) == len(samples):
            robust_matches = sum(score >= 0.55 for score in sample_scores)
            if len(samples) >= 6:
                required_matches = len(samples) - 1
            elif len(samples) >= 5:
                required_matches = len(samples) - 1
            retained = sorted(sample_scores, reverse=True)[:required_matches]
            robust_similarity = statistics.median(retained) if retained else 0.0
            robust_accepted = (
                robust_matches >= required_matches
                and robust_similarity >= max(threshold, 0.70)
                and max(sample_scores, default=0.0) >= 0.80
            )
            if robust_accepted:
                consensus_kind = (
                    "unanimous"
                    if robust_matches == len(samples)
                    else "near_unanimous"
                    if robust_matches >= len(samples) - 1
                    else "strong_majority"
                )

        strict_accepted = same_episode and usable and similarity >= threshold
        if strict_accepted and (matched < required_matches or robust_matches < required_matches or robust_similarity < threshold):
            return {
                "accepted": False, "reason": "invalid_llm_response", "similarity": 0.0,
                "matched_samples": 0, "total_samples": len(samples),
            }
        accepted = strict_accepted or robust_accepted
        reason = str(result.get("reason") or ("accepted" if accepted else "semantic_mismatch"))
        if robust_accepted:
            matched = max(matched, robust_matches)
            similarity = max(similarity, robust_similarity)
        if robust_accepted and not strict_accepted:
            if robust_matches == len(samples) - 1:
                reason = f"accepted_with_one_semantic_outlier: {reason}"
            else:
                reason = f"accepted_from_sample_scores:{consensus_kind}: {reason}"

        final_result = {
            "accepted": accepted,
            "reason": reason,
            "similarity": round(similarity, 4),
            "same_episode": same_episode,
            "usable_for_timing": usable,
            "matched_samples": matched,
            "total_samples": len(samples),
            "sample_scores": sample_scores,
            "min_similarity": threshold,
            "alignment_mode": alignment_mode,
            "strict_semantic_acceptance": strict_accepted,
            "robust_semantic_acceptance": robust_accepted,
            "robust_similarity": round(robust_similarity, 4),
            "robust_matches": robust_matches,
            "robust_required_matches": required_matches,
            "semantic_consensus_kind": consensus_kind,
            "cached": False,
        }
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        {"cached_at": time.time(), "result": final_result},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                temporary.replace(cache_path)
            except OSError:
                pass
        self.logger.info(
            "RESULT step=llm.semantic cache=miss japanese=%s english=%s accepted=%s similarity=%s",
            japanese_path.name,
            english_path.name,
            accepted,
            final_result.get("similarity"),
        )
        return final_result


    def match_subtitle_anchor_regions(
        self,
        regions: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Match clearly equivalent JP/EN dialogue groups inside small time windows.

        The caller has already established that both subtitle tracks belong to
        the same episode. This method does not estimate timing itself; it only
        identifies which ordered subtitle groups translate each other so the
        exact embedded-English timestamps can be used as clock anchors.
        """
        normalized_regions: list[dict[str, Any]] = []
        for region in regions[:6]:
            if not isinstance(region, dict):
                continue
            name = str(region.get("name") or "").strip()
            japanese = region.get("japanese")
            english = region.get("english")
            if not name or not isinstance(japanese, list) or not isinstance(english, list):
                continue
            normalized_regions.append(
                {
                    "name": name,
                    "japanese": japanese[:14],
                    "english": english[:28],
                }
            )
        if not normalized_regions:
            return {"accepted": False, "reason": "no_regions", "regions": []}

        cache_path: Path | None = None
        if self.cache_dir is not None:
            try:
                raw = json.dumps(normalized_regions, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(
                    (
                        f"anchor-regions-v1:{self.model}:{self.config.think}:"
                        f"{self.config.temperature}:{getattr(self.config, 'reasoning_effort', 'low')}:"
                        f"{self.config.num_ctx}:{raw}"
                    ).encode("utf-8")
                ).hexdigest()[:32]
                cache_path = self.cache_dir / "llm-anchor-alignment" / f"{digest}.json"
                if not force and cache_path.is_file():
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        age = time.time() - float(payload.get("cached_at") or 0.0)
                        result = payload.get("result")
                        ttl = (
                            SEMANTIC_CACHE_ACCEPTED_TTL_SECONDS
                            if isinstance(result, dict) and bool(result.get("accepted"))
                            else SEMANTIC_CACHE_REJECTED_TTL_SECONDS
                        )
                        if isinstance(result, dict) and age < ttl:
                            cached = dict(result)
                            cached["cached"] = True
                            return cached
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                cache_path = None

        system = (
            "The Japanese and English subtitle tracks are already verified to belong to the same episode. "
            "For each named region, match only subtitle groups that clearly express the same spoken dialogue. "
            "Use meaning, speaker intent, names and actions; timestamps are only a loose search window and must "
            "not decide a match. The embedded English track may contain opening/ending song lyrics, accessibility "
            "captions, signs, or other lines completely absent from the Japanese track. Leave those English rows "
            "unmatched. Japanese and English may split one spoken sentence differently, so one-to-many and "
            "many-to-one groups of up to 3 rows are allowed. Preserve narrative order and never reuse a row. "
            "Return strict JSON: {\"regions\":[{\"name\":string,\"matches\":["
            "{\"japanese\":[integer,...],\"english\":[integer,...],\"confidence\":0..1}]}]}. "
            "Indices are the zero-based index fields supplied in each region. Omit uncertain matches."
        )
        result = self._json_chat(
            system,
            json.dumps({"regions": normalized_regions}, ensure_ascii=False),
        )
        if not isinstance(result, dict):
            return {
                "accepted": False,
                "reason": "llm_request_failed",
                "regions": [],
                "cached": False,
            }

        allowed_names = {str(item["name"]) for item in normalized_regions}
        parsed_regions: list[dict[str, Any]] = []
        total_matches = 0
        raw_regions = result.get("regions")
        if isinstance(raw_regions, list):
            for raw_region in raw_regions:
                if not isinstance(raw_region, dict):
                    continue
                name = str(raw_region.get("name") or "").strip()
                if name not in allowed_names:
                    continue
                parsed_matches: list[dict[str, Any]] = []
                raw_matches = raw_region.get("matches")
                if isinstance(raw_matches, list):
                    for raw_match in raw_matches:
                        if not isinstance(raw_match, dict):
                            continue

                        def parse_indices(value: object) -> list[int]:
                            source = value if isinstance(value, list) else [value]
                            output: list[int] = []
                            for item in source:
                                try:
                                    output.append(int(item))
                                except (TypeError, ValueError):
                                    pass
                            return sorted(set(output))

                        japanese = parse_indices(raw_match.get("japanese"))
                        english = parse_indices(raw_match.get("english"))
                        try:
                            confidence = max(
                                0.0,
                                min(1.0, float(raw_match.get("confidence") or 0.0)),
                            )
                        except (TypeError, ValueError):
                            confidence = 0.0
                        if (
                            not japanese
                            or not english
                            or len(japanese) > 3
                            or len(english) > 3
                            or confidence < 0.50
                        ):
                            continue
                        parsed_matches.append(
                            {
                                "japanese": japanese,
                                "english": english,
                                "confidence": round(confidence, 4),
                            }
                        )
                total_matches += len(parsed_matches)
                parsed_regions.append({"name": name, "matches": parsed_matches})

        final = {
            "accepted": total_matches >= 2,
            "reason": "matched" if total_matches >= 2 else "too_few_matches",
            "regions": parsed_regions,
            "total_matches": total_matches,
            "cached": False,
        }
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {"cached_at": time.time(), "result": final},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return final


    def improve_identity(self, identity: VideoIdentity) -> VideoIdentity:
        system = (
            "Parse anime release filenames. Return strict JSON with keys title, episode, season, year. "
            "Use null when unknown. Do not translate the anime title."
        )
        result = self._json_chat(system, f"Filename: {identity.raw_name}")
        if not result:
            return identity
        title = str(result.get("title") or identity.title).strip()
        return VideoIdentity(
            title=title or identity.title,
            episode=_optional_int(result.get("episode"), identity.episode),
            season=_optional_int(result.get("season"), identity.season),
            year=_optional_int(result.get("year"), identity.year),
            raw_name=identity.raw_name,
        )

    def select_subtitle(self, identity: VideoIdentity, candidates: list[SubtitleCandidate]) -> int | None:
        rows = [
            {"index": index, "name": item.name, "source": item.source, "score": round(item.score, 2)}
            for index, item in enumerate(candidates[:12])
        ]
        system = (
            "Choose the Japanese subtitle file matching the anime title and exact episode. "
            "Return JSON: {\"index\": integer|null, \"confidence\": 0..1}. "
            "Reject files for another episode, signs-only, songs-only, or another season. "
            "When otherwise equally suitable, prefer SRT over ASS/SSA."
        )
        result = self._json_chat(
            system,
            json.dumps({"video": asdict(identity), "candidates": rows}, ensure_ascii=False),
        )
        return _safe_index(result, len(rows))

    def select_anilist(self, identity: VideoIdentity, candidates: list[AniListAnime]) -> int | None:
        rows = [
            {
                "index": index,
                "id": item.id,
                "titles": item.titles,
                "synonyms": item.synonyms[:5],
                "year": item.season_year,
                "episodes": item.episodes,
                "format": item.format,
                "score": round(item.score, 2),
            }
            for index, item in enumerate(candidates[:10])
        ]
        system = (
            "Choose the AniList anime corresponding to the release filename. "
            "Return JSON: {\"index\": integer|null, \"confidence\": 0..1}. "
            "Pay attention to sequel/season numbering and episode range."
        )
        result = self._json_chat(
            system,
            json.dumps({"video": asdict(identity), "candidates": rows}, ensure_ascii=False),
        )
        return _safe_index(result, len(rows))


def _optional_int(value: Any, fallback: int | None) -> int | None:
    try:
        return int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _safe_index(result: dict[str, Any] | None, length: int) -> int | None:
    if not result:
        return None
    try:
        index = int(result.get("index"))
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < length and confidence >= 0.55 else None
