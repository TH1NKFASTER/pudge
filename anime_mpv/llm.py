from __future__ import annotations

import hashlib
import json
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


SEMANTIC_CACHE_SCHEMA = "semantic-v4"
SEMANTIC_CACHE_ACCEPTED_TTL_SECONDS = 30 * 24 * 3600
SEMANTIC_CACHE_REJECTED_TTL_SECONDS = 6 * 3600


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


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(base_url: str, api_key: str = "", timeout: float = 8.0) -> list[str]:
    response = httpx.get(
        f"{base_url.rstrip('/')}/api/tags",
        headers=_headers(api_key),
        timeout=timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    models = payload.get("models", []) if isinstance(payload, dict) else []
    result = []
    for item in models:
        if isinstance(item, dict) and item.get("name"):
            result.append(str(item["name"]))
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
        self.client = httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers=_headers(config.api_key),
        )

    def close(self) -> None:
        self.client.close()

    def available(self) -> bool:
        try:
            response = self.client.get(f"{self.base_url}/api/tags")
            if not response.is_success:
                return False
            names = {
                str(item.get("name"))
                for item in response.json().get("models", [])
                if isinstance(item, dict) and item.get("name")
            }
            return not names or self.model in names
        except (httpx.HTTPError, TypeError, ValueError):
            return False

    def _json_chat(self, system: str, user: str) -> dict[str, Any] | None:
        try:
            with timed_step(
                self.logger,
                "llm.chat",
                model=self.model,
                request_chars=len(user),
            ):
                response = self.client.post(
                    f"{self.base_url}/api/chat",
                    json=build_chat_payload(self.config, system, user),
                )
                response.raise_for_status()
                content = response.json()["message"]["content"]
                return json.loads(content)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


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
                        if result and age < ttl:
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
        try:
            similarity = max(0.0, min(1.0, float(result.get("similarity", 0.0))))
            same_episode = bool(result.get("same_episode", False))
            usable = bool(result.get("usable_for_timing", False))
            matched = max(0, min(len(samples), int(result.get("matched_samples", 0))))
        except (TypeError, ValueError):
            return {
                "accepted": False,
                "reason": "invalid_llm_response",
                "similarity": 0.0,
                "matched_samples": 0,
                "total_samples": len(samples),
            }
        raw_scores = result.get("sample_scores", [])
        sample_scores: list[float] = []
        if isinstance(raw_scores, list):
            for value in raw_scores[: len(samples)]:
                try:
                    sample_scores.append(max(0.0, min(1.0, float(value))))
                except (TypeError, ValueError):
                    sample_scores.append(0.0)

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
