#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email.utils
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from pudge.config import DEFAULT_CONFIG_PATH, load_config
from pudge.database import Database
from pudge.filename import normalize_title, parse_anime_filename, title_similarity
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.media import TEXT_CODECS, find_embedded_japanese_subtitles
from pudge.providers.anilist import AniListClient, AniListError
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.providers.nyaa import (
    NyaaClient,
    NyaaError,
    release_episode,
    release_episode_range,
    release_identity_mismatch_reason,
    release_is_safe_batch_candidate,
    release_title_is_plausible,
    score_release,
    search_ranked,
)
from pudge.providers.qbittorrent import QBittorrentClient, QBittorrentError
from pudge.subtitles.benchmark import aggregate_oracle_reports, evaluate_subtitle_files, write_json
from pudge.subtitles.benchmark_corpus import (
    VIDEO_EXTENSIONS,
    SubtitleBenchmarkError,
    collect_case_reports,
    collect_diagnostic_reports,
    create_case_from_video,
    create_diagnostic_case_from_video,
    replay_stored_benchmark_case,
)


TOP_ANIME_QUERY = """
query ($page: Int!, $sort: [MediaSort]) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: $sort, isAdult: false) {
      id
      title { romaji english native userPreferred }
      synonyms
      episodes
      format
      seasonYear
      status
      duration
      popularity
      meanScore
      relations {
        edges {
          relationType
          node { id format title { romaji english native userPreferred } }
        }
      }
    }
  }
}
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and evaluate the Pudge subtitle oracle benchmark corpus."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser("evaluate", help="compare one aligned SRT with an embedded-JP oracle")
    evaluate.add_argument("--oracle", type=Path, required=True)
    evaluate.add_argument("--candidate", type=Path, required=True)
    evaluate.add_argument("--output", type=Path)
    evaluate.add_argument("--ngram", type=int, default=7)

    ingest = sub.add_parser("ingest", help="build one compact benchmark case from a local video")
    ingest.add_argument("--video", type=Path, required=True)
    ingest.add_argument("--corpus", type=Path, required=True)
    ingest.add_argument("--media-id", type=int)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--episode", type=int)
    ingest.add_argument("--no-jimaku", action="store_true")
    ingest.add_argument("--no-replay", action="store_true")

    aggregate = sub.add_parser("aggregate", help="aggregate current-Pudge oracle reports in a corpus")
    aggregate.add_argument("--corpus", type=Path, required=True)
    aggregate.add_argument("--output", type=Path)

    plan = sub.add_parser("plan-top", help="build a resumable AniList top-anime episode plan")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--limit", type=int, default=1000)
    plan.add_argument("--sort", choices=["popularity", "score"], default="popularity")
    plan.add_argument("--sample-episodes", type=int, default=0)
    plan.add_argument("--seed", type=int, default=20260827)
    plan.add_argument("--cache-dir", type=Path)
    plan.add_argument("--anilist-retries", type=int, default=6)
    plan.add_argument("--retry-base-seconds", type=float, default=2.0)

    acquire = sub.add_parser(
        "acquire-plan",
        help="download plan episodes through aria2/qBittorrent, compact, benchmark, delete originals",
    )
    acquire.add_argument("--plan", type=Path, required=True)
    acquire.add_argument("--corpus", type=Path, required=True)
    acquire.add_argument("--resolution", default="720p")
    acquire.add_argument("--backend", choices=["auto", "aria2", "qbittorrent"], default="auto")
    acquire.add_argument(
        "--source-strategy",
        choices=["erai-jp-feed", "plan-search"],
        default="erai-jp-feed",
        help=(
            "erai-jp-feed = strict global Erai JP-tag and Netflix feeds with no generic fallback; "
            "plan-search = legacy per-plan-title discovery (default: erai-jp-feed)"
        ),
    )
    acquire.add_argument(
        "--feed-query",
        action="append",
        default=[],
        help=(
            "Nyaa query for erai-jp-feed; repeat to merge queries "
            "(default: Erai [ENG] [JP/JPN] 480 plus Erai NF 720)"
        ),
    )
    acquire.add_argument(
        "--feed-category",
        default="1_0",
        help="Nyaa category for erai-jp-feed (default: 1_0, all anime)",
    )
    acquire.add_argument("--max-release-attempts", type=int, default=8)
    acquire.add_argument(
        "--episode-batch-size",
        type=int,
        default=3,
        help="download up to this many planned episodes from one multi-file pack (default: 3)",
    )
    acquire.add_argument("--min-source-seeders", type=int, default=1)
    acquire.add_argument("--max-download-gb", type=float, default=2.0)
    acquire.add_argument(
        "--max-unverified-probe-mib",
        type=float,
        default=700.0,
        help="maximum first-episode probe size for packs without explicit JP/JPN evidence (default: 700 MiB)",
    )
    acquire.add_argument("--download-timeout-minutes", type=float, default=30.0)
    acquire.add_argument("--metadata-timeout-seconds", type=float, default=120.0)
    acquire.add_argument("--stall-timeout-seconds", type=float, default=10.0)
    acquire.add_argument("--progress-interval-seconds", type=float, default=10.0)
    acquire.add_argument("--limit", type=int, default=0, help="0 = no GOLD target; otherwise keep scanning until this many new GOLD cases")
    acquire.add_argument("--start-index", type=int, default=0)

    stress = sub.add_parser(
        "stress-random",
        help=(
            "randomly sample popular AniList anime/episodes, verify exact download "
            "selection, run the production subtitle pipeline, and stop at a byte budget"
        ),
    )
    stress.add_argument("--corpus", type=Path, required=True)
    stress.add_argument("--target-gb", type=float, default=100.0)
    stress.add_argument("--anime-limit", type=int, default=1000)
    stress.add_argument("--sort", choices=["popularity", "score"], default="popularity")
    stress.add_argument("--seed", type=int, default=20260831)
    stress.add_argument("--resolution", default="720p")
    stress.add_argument("--backend", choices=["auto", "aria2", "qbittorrent"], default="auto")
    stress.add_argument("--max-release-attempts", type=int, default=5)
    stress.add_argument("--min-source-seeders", type=int, default=2)
    stress.add_argument("--max-download-gb", type=float, default=2.0)
    stress.add_argument("--download-timeout-minutes", type=float, default=30.0)
    stress.add_argument("--metadata-timeout-seconds", type=float, default=120.0)
    stress.add_argument("--stall-timeout-seconds", type=float, default=60.0)
    stress.add_argument("--progress-interval-seconds", type=float, default=10.0)
    stress.add_argument("--cache-dir", type=Path)
    stress.add_argument("--anilist-retries", type=int, default=6)
    stress.add_argument("--retry-base-seconds", type=float, default=2.0)
    stress.add_argument(
        "--keep-videos",
        action="store_true",
        help="keep downloaded source video instead of deleting it after compacting the case",
    )

    repair_stress = sub.add_parser(
        "repair-stress-library",
        help=(
            "repair/normalize Library registrations for videos already downloaded by "
            "stress-random using the AniList identities stored in stress-plan/events"
        ),
    )
    repair_stress.add_argument("--corpus", type=Path, required=True)

    replay_stress = sub.add_parser(
        "replay-stress",
        help=(
            "force-replay current Pudge on already materialized stress benchmark "
            "media/candidates without downloading or refetching anything"
        ),
    )
    replay_stress.add_argument("--corpus", type=Path, required=True)
    replay_stress.add_argument("--start-index", type=int, default=0)
    replay_stress.add_argument("--limit", type=int, default=0, help="0 = replay all valid stored reports")
    return parser


def _preferred_title(media: dict[str, Any]) -> str:
    titles = media.get("title") if isinstance(media.get("title"), dict) else {}
    for key in ("userPreferred", "romaji", "english", "native"):
        value = str(titles.get(key) or "").strip()
        if value:
            return value
    return f"AniList {media.get('id')}"


def _title_variants(media: dict[str, Any]) -> list[str]:
    titles = media.get("title") if isinstance(media.get("title"), dict) else {}
    values = [str(titles.get(key) or "").strip() for key in ("userPreferred", "romaji", "english", "native")]
    values.extend(str(value).strip() for value in media.get("synonyms") or [])
    return list(dict.fromkeys(value for value in values if value))


def _relation_rows(media: dict[str, Any]) -> list[dict[str, object]]:
    relations = media.get("relations") if isinstance(media.get("relations"), dict) else {}
    rows: list[dict[str, object]] = []
    for edge in relations.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") if isinstance(edge.get("node"), dict) else {}
        try:
            media_id = int(node.get("id"))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "relation_type": str(edge.get("relationType") or ""),
                "media_id": media_id,
                "format": str(node.get("format") or ""),
                "title": _preferred_title(node),
            }
        )
    return rows


def _anilist_cache_has_relations(payload: dict[str, Any]) -> bool:
    page = (payload.get("data") or {}).get("Page") if isinstance(payload.get("data"), dict) else None
    rows = page.get("media") if isinstance(page, dict) else None
    if not isinstance(rows, list):
        return False
    return all(not isinstance(row, dict) or "relations" in row for row in rows)


def _anilist_cache_path(cache_dir: Path, sort_value: str, page: int) -> Path:
    return Path(cache_dir) / f"{sort_value.casefold()}-page-{int(page):04d}.json"


def _load_cached_anilist_page(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("data"):
        return None
    # v73 needs relation metadata for hard season validation. Older AniList
    # page caches are intentionally refreshed once instead of silently losing
    # the distinction between a first season and a sequel with a generic title.
    return payload if _anilist_cache_has_relations(payload) else None


def _store_anilist_page(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _anilist_retry_delay(response: Any | None, attempt: int, base_seconds: float) -> float:
    delay = min(120.0, max(0.1, float(base_seconds)) * (2 ** max(0, int(attempt))))
    headers = getattr(response, "headers", {}) or {}
    retry_after = str(headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                delay = max(delay, parsed.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    reset = str(headers.get("X-RateLimit-Reset") or headers.get("X-RateLimit-Reset-At") or "").strip()
    if reset:
        try:
            delay = max(delay, float(reset) - time.time())
        except ValueError:
            pass
    return min(180.0, max(0.0, delay))


def _fetch_anilist_page(
    client: httpx.Client,
    endpoint: str,
    *,
    page: int,
    sort_value: str,
    max_retries: int,
    retry_base_seconds: float,
) -> dict[str, Any]:
    retries = max(0, int(max_retries))
    retryable = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response = None
        try:
            response = client.post(
                endpoint,
                json={"query": TOP_ANIME_QUERY, "variables": {"page": page, "sort": [sort_value]}},
            )
            status = int(getattr(response, "status_code", 200) or 200)
            if status in retryable:
                if attempt >= retries:
                    response.raise_for_status()
                time.sleep(_anilist_retry_delay(response, attempt, retry_base_seconds))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("AniList returned a non-object response")
            if payload.get("errors"):
                raise RuntimeError(f"AniList returned errors: {payload['errors']}")
            return payload
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(_anilist_retry_delay(response, attempt, retry_base_seconds))
    if last_error is not None:
        raise last_error
    raise RuntimeError("AniList request failed without a response")


def fetch_top_anime_plan(
    endpoint: str,
    *,
    limit: int,
    sort: str,
    sample_episodes: int,
    seed: int,
    cache_dir: Path | None = None,
    max_retries: int = 6,
    retry_base_seconds: float = 2.0,
) -> dict[str, object]:
    target = max(1, int(limit))
    media_rows: list[dict[str, Any]] = []
    sort_value = "POPULARITY_DESC" if sort == "popularity" else "SCORE_DESC"
    headers = {"User-Agent": "pudge-subtitle-benchmark"}
    with httpx.Client(timeout=45, headers=headers) as client:
        page = 1
        while len(media_rows) < target:
            payload = None
            cache_path = _anilist_cache_path(cache_dir, sort_value, page) if cache_dir else None
            if cache_path is not None and cache_path.is_file():
                payload = _load_cached_anilist_page(cache_path)
            if payload is None:
                payload = _fetch_anilist_page(
                    client,
                    endpoint,
                    page=page,
                    sort_value=sort_value,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                )
                if cache_path is not None:
                    _store_anilist_page(cache_path, payload)
            page_payload = (payload.get("data") or {}).get("Page") or {}
            rows = [row for row in page_payload.get("media") or [] if isinstance(row, dict)]
            if not rows:
                break
            media_rows.extend(rows)
            if not bool((page_payload.get("pageInfo") or {}).get("hasNextPage")):
                break
            page += 1
    media_rows = media_rows[:target]

    plan_rows: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for rank, media in enumerate(media_rows, start=1):
        episodes_raw = media.get("episodes")
        try:
            episodes = int(episodes_raw) if episodes_raw is not None else None
        except (TypeError, ValueError):
            episodes = None
        base = {
            "rank": rank,
            "media_id": int(media["id"]),
            "title": _preferred_title(media),
            "titles": _title_variants(media),
            "synonyms": [str(value) for value in media.get("synonyms") or []],
            "episodes": episodes,
            "format": media.get("format"),
            "season_year": media.get("seasonYear"),
            "status": media.get("status"),
            "duration": media.get("duration"),
            "popularity": media.get("popularity"),
            "mean_score": media.get("meanScore"),
            "relations": _relation_rows(media),
        }
        if episodes is None or episodes <= 0:
            unresolved.append(base)
            continue
        for episode in range(1, episodes + 1):
            plan_rows.append({**base, "episode": episode})

    if sample_episodes > 0:
        sampled: list[dict[str, object]] = []
        by_media: dict[int, list[dict[str, object]]] = {}
        for row in plan_rows:
            by_media.setdefault(int(row["media_id"]), []).append(row)
        for media_id, media_plan in by_media.items():
            count = min(max(1, int(sample_episodes)), len(media_plan))
            if count >= len(media_plan):
                chosen = list(media_plan)
            else:
                # Stable per-anime sample: changing the top-N limit does not
                # reshuffle episodes already chosen for earlier anime.
                rng = random.Random(int(seed) ^ int(media_id))
                chosen = rng.sample(media_plan, count)
            sampled.extend(chosen)
        plan_rows = sorted(sampled, key=lambda row: (int(row["rank"]), int(row["episode"])))

    return {
        "schema": "pudge-subtitle-benchmark-plan-v1",
        "created_at": time.time(),
        "sort": sort,
        "anime_limit": target,
        "anime_count": len(media_rows),
        "episode_count": len(plan_rows),
        "sample_episodes": int(sample_episodes),
        "seed": int(seed),
        "unresolved_anime": unresolved,
        "episodes": plan_rows,
    }


def _aggregate_corpus(corpus: Path) -> dict[str, object]:
    cases = collect_case_reports(corpus)
    diagnostics = collect_diagnostic_reports(corpus)
    source_rejections = _collect_source_rejections(corpus)
    production_reports: list[dict[str, Any]] = []
    candidate_reports: list[dict[str, Any]] = []
    false_accepts = 0
    false_rejects = 0
    candidate_misses = 0
    selection_failures = 0
    alignment_failures = 0
    with_candidates = 0
    same_episode_available = 0
    for case in cases:
        candidates = [row for row in case.get("candidates") or [] if isinstance(row, dict)]
        if candidates:
            with_candidates += 1
        case_has_same = False
        for row in candidates:
            oracle = row.get("oracle")
            if isinstance(oracle, dict):
                candidate_reports.append(oracle)
                if bool(oracle.get("same_episode")):
                    case_has_same = True
        if case_has_same:
            same_episode_available += 1
        else:
            candidate_misses += 1
        replay = case.get("current_pudge")
        if isinstance(replay, dict):
            if bool(replay.get("false_accept")):
                false_accepts += 1
            if bool(replay.get("false_reject")):
                false_rejects += 1
            if bool(replay.get("selection_failure")):
                selection_failures += 1
            if bool(replay.get("alignment_failure")):
                alignment_failures += 1
            oracle = replay.get("oracle")
            if isinstance(oracle, dict):
                production_reports.append(oracle)

    production = aggregate_oracle_reports(production_reports)
    candidate = aggregate_oracle_reports(candidate_reports)
    diagnostic_reasons: dict[str, int] = {}
    adversarial_reasons: dict[str, int] = {}
    adversarial_examples: list[dict[str, object]] = []
    diagnostic_count = 0
    adversarial_count = 0
    for row in diagnostics:
        reasons = _diagnostic_adversarial_reasons(row)
        if reasons:
            adversarial_count += 1
            for reason in reasons:
                adversarial_reasons[reason] = adversarial_reasons.get(reason, 0) + 1
            if len(adversarial_examples) < 20:
                adversarial_examples.append(
                    {
                        "media_id": row.get("media_id"),
                        "episode": row.get("episode"),
                        "title": row.get("title"),
                        "source_release_name": row.get("source_release_name"),
                        "reasons": reasons,
                    }
                )
            continue
        diagnostic_count += 1
        reason = str(row.get("reason") or "unknown")
        diagnostic_reasons[reason] = diagnostic_reasons.get(reason, 0) + 1

    source_rejection_reasons: dict[str, int] = {}
    for row in source_rejections:
        reason = str(row.get("reason") or "unknown")
        source_rejection_reasons[reason] = source_rejection_reasons.get(reason, 0) + 1

    return {
        "schema": "pudge-subtitle-benchmark-summary-v1",
        "cases": len(cases),
        "records_total": len(cases) + len(diagnostics),
        "tiers": {
            "gold": len(cases),
            "silver": 0,
            "diagnostic": diagnostic_count,
            "adversarial": adversarial_count,
        },
        "diagnostic_reason_counts": diagnostic_reasons,
        "adversarial_reason_counts": adversarial_reasons,
        "adversarial_examples": adversarial_examples,
        "source_rejections": len(source_rejections),
        "source_rejection_reason_counts": source_rejection_reasons,
        "cases_with_candidates": with_candidates,
        "cases_with_same_episode_candidate": same_episode_available,
        "same_episode_candidate_ratio": round(same_episode_available / max(1, len(cases)), 4),
        "false_accepts": false_accepts,
        "false_accept_ratio": round(false_accepts / max(1, len(cases)), 4),
        "false_rejects": false_rejects,
        "false_reject_ratio": round(false_rejects / max(1, len(cases)), 4),
        "candidate_misses": candidate_misses,
        "candidate_miss_ratio": round(candidate_misses / max(1, len(cases)), 4),
        "selection_failures": selection_failures,
        "selection_failure_ratio": round(selection_failures / max(1, len(cases)), 4),
        "alignment_failures": alignment_failures,
        "alignment_failure_ratio": round(alignment_failures / max(1, len(cases)), 4),
        "current_pudge": production,
        "raw_candidates": candidate,
    }


def _write_corpus_summary(corpus: Path) -> dict[str, object]:
    summary = _aggregate_corpus(corpus)
    write_json(Path(corpus) / "summary.json", summary)
    return summary


def _read_plan(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("plan does not contain an episodes list")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _plan_negative_titles_by_media(
    rows: Iterable[dict[str, Any]],
) -> dict[int, tuple[str, ...]]:
    """Build franchise-negative titles from the plan's AniList relation graph.

    Plan rows repeat sampled episodes, so collapse them by media ID first.
    Direct relation IDs from all top-anime rows are enough to reconstruct the
    connected component whenever intermediate franchise entries are also in the
    plan.  The benchmark only supplies these titles; the production Nyaa helper
    decides whether a release conflicts with them.
    """
    titles: dict[int, list[str]] = {}
    adjacency: dict[int, set[int]] = {}

    def add_title(media_id: int, value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        bucket = titles.setdefault(media_id, [])
        if text not in bucket:
            bucket.append(text)

    for row in rows:
        try:
            media_id = int(row.get("media_id"))
        except (TypeError, ValueError):
            continue
        adjacency.setdefault(media_id, set())
        add_title(media_id, row.get("title"))
        for value in row.get("titles") or []:
            add_title(media_id, value)
        for value in row.get("synonyms") or []:
            add_title(media_id, value)
        for relation in row.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            try:
                related_id = int(relation.get("media_id"))
            except (TypeError, ValueError):
                continue
            if related_id == media_id:
                continue
            adjacency.setdefault(related_id, set())
            adjacency[media_id].add(related_id)
            adjacency[related_id].add(media_id)
            add_title(related_id, relation.get("title"))

    result: dict[int, tuple[str, ...]] = {}
    unseen = set(adjacency)
    while unseen:
        start = unseen.pop()
        component = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbour in adjacency.get(current, set()):
                if neighbour in component:
                    continue
                component.add(neighbour)
                unseen.discard(neighbour)
                stack.append(neighbour)
        component_titles: list[str] = []
        for media_id in component:
            for value in titles.get(media_id, []):
                if value not in component_titles:
                    component_titles.append(value)
        for media_id in component:
            own = set(titles.get(media_id, []))
            result[media_id] = tuple(value for value in component_titles if value not in own)

    # Backward-compatible fallback for old plans that predate relation metadata.
    # Distinct AniList entries whose title is a strict token extension of the
    # target title are still valid negative evidence (for example Kimetsu no
    # Yaiba -> Kimetsu no Yaiba: Hashira Geiko-hen).  The shared production
    # provider still owns the actual release-conflict decision.
    media_ids = list(titles)
    normalized = {
        media_id: [normalize_title(value).split() for value in values if normalize_title(value)]
        for media_id, values in titles.items()
    }
    for media_id in media_ids:
        extras = list(result.get(media_id, ()))
        roots = normalized.get(media_id, [])
        for other_id in media_ids:
            if other_id == media_id:
                continue
            for raw_value, candidate_tokens in zip(
                titles.get(other_id, []), normalized.get(other_id, [])
            ):
                if any(
                    len(candidate_tokens) > len(root_tokens)
                    and candidate_tokens[: len(root_tokens)] == root_tokens
                    for root_tokens in roots
                    if root_tokens
                ) and raw_value not in extras:
                    extras.append(raw_value)
        result[media_id] = tuple(extras)
    return result


def _completed_episode_keys(corpus: Path) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for case in collect_case_reports(corpus):
        try:
            media_id = int(case.get("media_id"))
            episode = int(case.get("episode"))
        except (TypeError, ValueError):
            continue
        result.add((media_id, episode))
    return result


def _diagnostic_release_keys(corpus: Path) -> set[tuple[int, int, str]]:
    result: set[tuple[int, int, str]] = set()
    for case in collect_diagnostic_reports(corpus):
        try:
            media_id = int(case.get("media_id"))
            episode = int(case.get("episode"))
        except (TypeError, ValueError):
            continue
        source = case.get("source_release") if isinstance(case.get("source_release"), dict) else {}
        release_key = str(source.get("info_hash") or case.get("source_release_name") or "").strip().casefold()
        if release_key:
            result.add((media_id, episode, release_key))
    return result


def _diagnostic_media_release_keys(corpus: Path) -> set[tuple[int, str]]:
    """Pack-wide no-JP evidence: one probed episode is enough to avoid redownloading the same pack."""
    result: set[tuple[int, str]] = set()
    for case in collect_diagnostic_reports(corpus):
        try:
            media_id = int(case.get("media_id"))
        except (TypeError, ValueError):
            continue
        if str(case.get("reason") or "") != "no_embedded_japanese_text_subtitle":
            continue
        source = case.get("source_release") if isinstance(case.get("source_release"), dict) else {}
        if not bool(source.get("is_batch")):
            continue
        release_key = str(source.get("info_hash") or case.get("source_release_name") or "").strip().casefold()
        if release_key:
            result.add((media_id, release_key))
    return result



def _diagnostic_media_release_family_keys(corpus: Path) -> set[tuple[int, str]]:
    result: set[tuple[int, str]] = set()
    for case in collect_diagnostic_reports(corpus):
        try:
            media_id = int(case.get("media_id"))
        except (TypeError, ValueError):
            continue
        if str(case.get("reason") or "") != "no_embedded_japanese_text_subtitle":
            continue
        source = case.get("source_release") if isinstance(case.get("source_release"), dict) else {}
        if not bool(source.get("is_batch")):
            continue
        family = str(source.get("release_family_key") or "") or _benchmark_release_family_key(source)
        if family:
            result.add((media_id, family))
    return result


def _release_diagnostic_key(row: dict[str, Any], release: Any) -> tuple[int, int, str]:
    release_key = str(
        getattr(release, "info_hash", "")
        or getattr(release, "title", "")
        or getattr(release, "torrent_url", "")
    ).strip().casefold()
    return int(row["media_id"]), int(row["episode"]), release_key


def _release_media_diagnostic_key(row: dict[str, Any], release: Any) -> tuple[int, str]:
    release_key = str(
        getattr(release, "info_hash", "")
        or getattr(release, "title", "")
        or getattr(release, "torrent_url", "")
    ).strip().casefold()
    return int(row["media_id"]), release_key


def _qbit_client(config) -> QBittorrentClient:
    return QBittorrentClient(
        config.qbittorrent.base_url,
        config.qbittorrent.username,
        config.qbittorrent.password,
        config.qbittorrent.api_key,
        verify_tls=config.qbittorrent.verify_tls,
        pre_download_command=config.qbittorrent.pre_download_command,
        auto_start_app=config.qbittorrent.auto_start_app,
    )


def _aria2_client(config) -> Aria2Client:
    aria = config.aria2
    return Aria2Client(
        enabled=aria.enabled,
        binary=aria.binary,
        rpc_port=aria.rpc_port,
        pre_download_command=config.qbittorrent.pre_download_command,
        paused_on_add=aria.paused_on_add,
        auto_start=aria.auto_start,
        source_proxy_mode=config.nyaa.proxy_mode,
        source_proxy_url=config.nyaa.proxy_url,
        seed_mode=aria.seed_mode,
        seed_ratio=aria.seed_ratio,
        seed_time_minutes=aria.seed_time_minutes,
        upload_limit_kib=aria.upload_limit_kib,
        vpn_interface=aria.vpn_interface,
        vpn_kill_switch=aria.vpn_kill_switch,
    )


def _download_client(config, backend: str) -> tuple[str, Any]:
    requested = str(backend or "auto").casefold()
    if requested not in {"auto", "aria2", "qbittorrent"}:
        raise RuntimeError(f"unsupported benchmark download backend: {backend}")

    if requested in {"auto", "aria2"} and config.aria2.enabled:
        client = _aria2_client(config)
        try:
            client.ensure_running()
            return "aria2", client
        except (Aria2Error, OSError) as exc:
            client.close()
            if requested == "aria2":
                raise RuntimeError(f"aria2 backend is enabled but unavailable: {exc}") from exc

    if requested in {"auto", "qbittorrent"} and config.qbittorrent.enabled:
        client = _qbit_client(config)
        try:
            client.login()
            return "qbittorrent", client
        except (QBittorrentError, OSError) as exc:
            client.close()
            if requested == "qbittorrent":
                raise RuntimeError(f"qBittorrent backend is enabled but unavailable: {exc}") from exc

    if requested == "aria2":
        raise RuntimeError("aria2 backend must be configured/enabled for acquire-plan")
    if requested == "qbittorrent":
        raise RuntimeError("qBittorrent backend must be configured/enabled for acquire-plan")
    raise RuntimeError("aria2 or qBittorrent backend must be configured/enabled for acquire-plan")


def _video_file_choice(files: list[dict[str, Any]], episode: int) -> tuple[int, dict[str, Any]] | None:
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    exact_candidates: list[tuple[float, int, dict[str, Any]]] = []
    for fallback_index, row in enumerate(files):
        name = str(row.get("name") or "")
        path = Path(name)
        if path.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        parsed_episode = release_episode(path.name)
        if parsed_episode is None:
            parsed_episode = parse_anime_filename(path.name).episode
        size = int(row.get("size") or 0)
        score = math.log10(max(1, size))
        if parsed_episode == episode:
            score += 100.0
        elif parsed_episode is not None:
            score -= 100.0
        index = int(row.get("index") if row.get("index") is not None else fallback_index)
        item = (score, index, row)
        candidates.append(item)
        if parsed_episode == episode:
            exact_candidates.append(item)
    if exact_candidates:
        _score, index, row = max(exact_candidates, key=lambda item: item[0])
        return index, row
    if not candidates:
        return None
    # Multi-file packs must contain an explicitly identifiable target episode.
    # Picking the largest unrelated video would silently poison the oracle corpus.
    if len(candidates) > 1:
        return None
    _score, index, row = candidates[0]
    return index, row


def _video_file_choices(
    files: list[dict[str, Any]],
    episodes: Iterable[int],
) -> dict[int, tuple[int, dict[str, Any]]]:
    """Select exact episode files from a pack without reusing one file twice.

    The first requested episode may still use the historical single-file
    fallback via ``_video_file_choice``. Extra episodes must be explicit exact
    matches; otherwise one ambiguous file could silently serve several oracle
    cases.
    """
    requested = list(dict.fromkeys(int(value) for value in episodes if int(value) > 0))
    if not requested:
        return {}
    chosen: dict[int, tuple[int, dict[str, Any]]] = {}
    used_indexes: set[int] = set()
    for offset, episode in enumerate(requested):
        choice = _video_file_choice(files, episode)
        if choice is None:
            continue
        index, row = choice
        if index in used_indexes:
            continue
        name = Path(str(row.get("name") or "")).name
        parsed_episode = release_episode(name)
        if parsed_episode is None:
            parsed_episode = parse_anime_filename(name).episode
        if offset > 0 and parsed_episode != episode:
            continue
        chosen[episode] = (index, row)
        used_indexes.add(index)
    return chosen


def _wait_for_files(
    client: Any,
    torrent_hash: str,
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(10.0, timeout_seconds)
    while time.monotonic() < deadline:
        files = client.files(torrent_hash)
        if files:
            return files
        time.sleep(2.0)
    raise QBittorrentError("torrent metadata did not arrive before timeout")


def _human_bytes(value: float) -> str:
    amount = max(0.0, float(value))
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units[:-1]:
        if amount < 1024.0:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} GiB"


def _wait_for_download_many(
    client: Any,
    torrent_hash: str,
    target_indexes: Iterable[int],
    *,
    timeout_seconds: float,
    stall_timeout_seconds: float = 90.0,
    progress_interval_seconds: float = 10.0,
) -> dict[int, dict[str, Any]]:
    wanted = set(int(value) for value in target_indexes)
    if not wanted:
        raise QBittorrentError("benchmark download has no selected files")
    started_at = time.monotonic()
    deadline = started_at + max(60.0, timeout_seconds)
    stall_timeout = max(1.0, float(stall_timeout_seconds))
    report_interval = max(2.0, float(progress_interval_seconds))
    last_progress_at = time.monotonic()
    last_report_at = 0.0
    best_completed = -1
    initial_completed: int | None = None

    while time.monotonic() < deadline:
        files = client.files(torrent_hash)
        targets: dict[int, dict[str, Any]] = {}
        for fallback_index, row in enumerate(files):
            index = int(row.get("index") if row.get("index") is not None else fallback_index)
            if index in wanted:
                targets[index] = row
        if set(targets) != wanted:
            raise QBittorrentError("selected benchmark file disappeared from torrent metadata")

        total_size = sum(max(0, int(row.get("size") or 0)) for row in targets.values())
        completed = sum(
            int(max(0, int(row.get("size") or 0)) * max(0.0, min(1.0, float(row.get("progress") or 0.0))))
            for row in targets.values()
        )
        all_complete = all(float(row.get("progress") or 0.0) >= 0.999 for row in targets.values())
        if all_complete:
            return targets

        status = client.torrent_status(torrent_hash)
        if status is None:
            raise QBittorrentError("benchmark torrent disappeared during download")
        speed = max(0, int(status.get("dlspeed") or 0))
        now = time.monotonic()
        if initial_completed is None:
            initial_completed = completed
        if completed > best_completed:
            best_completed = completed
            last_progress_at = now

        if now - last_report_at >= report_interval:
            progress = completed / max(1, total_size)
            eta = "--"
            if speed > 0 and total_size > completed:
                eta_seconds = int((total_size - completed) / speed)
                eta = f"{eta_seconds // 60}m{eta_seconds % 60:02d}s"
            peers = int(status.get("connections") or 0)
            seeders = int(status.get("num_seeders") or 0)
            print(
                f"  download {progress * 100:5.1f}% "
                f"{_human_bytes(completed)} / {_human_bytes(total_size)} "
                f"{_human_bytes(speed)}/s ETA {eta} peers={peers} seeders={seeders} "
                f"files={len(wanted)}",
                flush=True,
            )
            last_report_at = now

        if now - last_progress_at >= stall_timeout:
            progress = completed / max(1, total_size)
            raise QBittorrentError(
                f"benchmark download stalled for {int(stall_timeout)}s "
                f"without selected-file progress at {progress * 100:.1f}% "
                f"(backend aggregate reports {_human_bytes(speed)}/s)"
            )

        # Listed seeders and even aria2's instantaneous speed can stay non-zero
        # for a swarm that would occupy this mass benchmark for hours. Once the
        # normal stall grace has elapsed, require the observed transfer rate to
        # project completion inside the caller's hard timeout. Give the backend
        # half of its reported rate as a warm-up allowance, while still grounding
        # the decision in actual completed bytes.
        elapsed = now - started_at
        remaining_bytes = max(0, total_size - completed)
        remaining_timeout = max(0.0, deadline - now)
        transferred = max(0, completed - (initial_completed or 0))
        if elapsed >= stall_timeout and remaining_bytes > 0 and remaining_timeout > 0:
            observed_speed = transferred / max(1.0, elapsed)
            projected_speed = max(observed_speed, speed * 0.5)
            if projected_speed > 0:
                projected_seconds = remaining_bytes / projected_speed
                if projected_seconds > remaining_timeout:
                    raise QBittorrentError(
                        f"benchmark download too slow after {int(elapsed)}s: "
                        f"sustained {_human_bytes(projected_speed)}/s, projected ETA "
                        f"{math.ceil(projected_seconds / 60.0)}m exceeds "
                        f"{math.ceil(remaining_timeout / 60.0)}m remaining timeout"
                    )
        time.sleep(min(5.0, report_interval))
    raise QBittorrentError("benchmark episode download timed out")


def _wait_for_download(
    client: Any,
    torrent_hash: str,
    target_index: int,
    *,
    timeout_seconds: float,
    stall_timeout_seconds: float = 90.0,
    progress_interval_seconds: float = 10.0,
) -> dict[str, Any]:
    return _wait_for_download_many(
        client,
        torrent_hash,
        [target_index],
        timeout_seconds=timeout_seconds,
        stall_timeout_seconds=stall_timeout_seconds,
        progress_interval_seconds=progress_interval_seconds,
    )[int(target_index)]


def _video_has_embedded_japanese_text(
    video: Path,
    *,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
) -> bool:
    try:
        candidates = find_embedded_japanese_subtitles(video, ffprobe_path, ffmpeg_path)
    except Exception as exc:
        raise QBittorrentError(f"Japanese subtitle probe failed: {exc}") from exc
    return any(candidate.codec in TEXT_CODECS for candidate in candidates)


def _download_release_videos(
    client: Any,
    release,
    *,
    episodes: Iterable[int],
    root: Path,
    metadata_timeout_seconds: float,
    download_timeout_seconds: float,
    max_target_bytes: int | None = None,
    stall_timeout_seconds: float = 90.0,
    progress_interval_seconds: float = 10.0,
    identity_row: dict[str, Any] | None = None,
    probe_first: bool = False,
    probe_ffprobe_path: str = "ffprobe",
    probe_ffmpeg_path: str = "ffmpeg",
    max_unverified_probe_bytes: int | None = None,
) -> tuple[str, dict[int, Path]]:
    requested = list(dict.fromkeys(int(value) for value in episodes if int(value) > 0))
    if not requested:
        raise QBittorrentError("benchmark release has no requested episodes")
    wanted_hash = str(release.info_hash or "").strip().casefold()
    torrent_hash = ""
    resumed_existing = False
    if wanted_hash:
        for item in client.torrents():
            if str(getattr(item, "torrent_hash", "") or "").strip().casefold() != wanted_hash:
                continue
            existing_root = Path(str(getattr(item, "save_path", "") or "")).expanduser()
            try:
                benchmark_owned = existing_root.resolve() == root.resolve()
            except OSError:
                benchmark_owned = False
            if not benchmark_owned:
                raise QBittorrentError(
                    "release already exists in the configured backend; refusing to retag/delete user torrent"
                )
            torrent_hash = wanted_hash
            resumed_existing = True
            print("  resume benchmark-owned torrent", flush=True)
            break

    if not torrent_hash:
        category = "pudge-benchmark"
        tags = ["pudge-benchmark"]
        if identity_row is not None:
            media_id = identity_row.get("media_id")
            if media_id is not None:
                tags.append(f"anilist:{int(media_id)}")
            anime_title = str(identity_row.get("title") or "").strip()
            if anime_title:
                tags.append(f"anime:{anime_title}")
            tags.append(f"episode:{requested[0]}")
        if _benchmark_batch_release(release):
            tags.append("batch")
        torrent_hash = client.add_release(
            release,
            save_path=root,
            category=category,
            tags=tags,
            paused=True,
            stop_at_metadata=True,
        )
    try:
        files = _wait_for_files(client, torrent_hash, timeout_seconds=metadata_timeout_seconds)
        choices = _video_file_choices(files, requested)
        first_episode = requested[0]
        if first_episode not in choices:
            raise QBittorrentError("torrent contains no usable video file for requested episode")

        if identity_row is not None:
            for episode, (_index, target) in list(choices.items()):
                mismatch = _source_identity_mismatch_reason(
                    identity_row, str(target.get("name") or "")
                )
                if mismatch:
                    if episode == first_episode:
                        raise QBittorrentError(
                            f"selected file identity mismatch ({mismatch}): {target.get('name') or ''}"
                        )
                    del choices[episode]

        # Singles cannot satisfy several planned episodes. Packs may download as
        # many exact matches as we found, all in one torrent session.
        if not _benchmark_batch_release(release):
            choices = {first_episode: choices[first_episode]}

        for episode, (_index, target) in list(choices.items()):
            target_size = max(0, int(target.get("size") or 0))
            if max_target_bytes is not None and target_size > max(1, int(max_target_bytes)):
                if episode == first_episode:
                    raise QBittorrentError(
                        f"selected episode is too large for benchmark: {_human_bytes(target_size)} "
                        f"> {_human_bytes(max_target_bytes)}"
                    )
                # Extra episode is optional; don't throw away a usable current
                # target because another sampled episode is unusually large.
                del choices[episode]
        if first_episode not in choices:
            raise QBittorrentError("requested episode was removed by benchmark size limit")

        all_indexes = [
            int(row.get("index") if row.get("index") is not None else index)
            for index, row in enumerate(files)
        ]

        def resolve_paths(
            selected_choices: dict[int, tuple[int, dict[str, Any]]],
            downloaded_rows: dict[int, dict[str, Any]],
        ) -> dict[int, Path]:
            status = client.torrent_status(torrent_hash) or {}
            save_path = Path(str(status.get("save_path") or root))
            result: dict[int, Path] = {}
            for episode, (index, target) in selected_choices.items():
                row = downloaded_rows[index]
                relative = Path(str(row.get("name") or target.get("name") or ""))
                path = root / relative
                if not path.is_file():
                    path = save_path / relative
                if not path.is_file():
                    raise QBittorrentError(f"download completed but video file is missing: {path}")
                result[episode] = path
            return result

        staged_probe = bool(probe_first and _benchmark_batch_release(release))
        if staged_probe:
            # Probe the smallest exact planned episode first. One no-JP result is
            # enough to avoid paying for the other sampled files from this pack.
            probe_episode, (probe_index, probe_target) = min(
                choices.items(),
                key=lambda item: max(0, int(item[1][1].get("size") or 0)),
            )
            probe_size = max(0, int(probe_target.get("size") or 0))
            if (
                max_unverified_probe_bytes is not None
                and probe_size > max(1, int(max_unverified_probe_bytes))
                and not resumed_existing
            ):
                raise QBittorrentError(
                    "unverified pack probe episode is too large: "
                    f"{_human_bytes(probe_size)} > {_human_bytes(max_unverified_probe_bytes)}"
                )

            skipped = [index for index in all_indexes if index != probe_index]
            if skipped:
                client.set_file_priority(torrent_hash, skipped, 0)
            client.set_file_priority(torrent_hash, [probe_index], 1)
            remaining_labels = ", ".join(
                f"E{episode}" for episode in sorted(choices) if episode != probe_episode
            )
            suffix = f" before {remaining_labels}" if remaining_labels else ""
            print(
                f"  pack probe: E{probe_episode} {_human_bytes(probe_size)}{suffix}",
                flush=True,
            )
            client.start(torrent_hash)
            probe_rows = _wait_for_download_many(
                client,
                torrent_hash,
                [probe_index],
                timeout_seconds=download_timeout_seconds,
                stall_timeout_seconds=stall_timeout_seconds,
                progress_interval_seconds=progress_interval_seconds,
            )
            probe_paths = resolve_paths(
                {probe_episode: (probe_index, probe_target)}, probe_rows
            )
            probe_video = probe_paths[probe_episode]
            if not _video_has_embedded_japanese_text(
                probe_video,
                ffprobe_path=probe_ffprobe_path,
                ffmpeg_path=probe_ffmpeg_path,
            ):
                print(
                    "  pack probe: no embedded Japanese text; "
                    "skip remaining target episodes",
                    flush=True,
                )
                return torrent_hash, probe_paths

            remaining = {
                episode: choice
                for episode, choice in choices.items()
                if episode != probe_episode
            }
            if not remaining:
                print("  pack probe: Japanese text confirmed", flush=True)
                return torrent_hash, probe_paths
            print(
                "  pack probe: Japanese text confirmed; adding "
                + ", ".join(f"E{episode}" for episode in sorted(remaining)),
                flush=True,
            )
            remaining_indexes = sorted(index for index, _row in remaining.values())
            client.set_file_priority(torrent_hash, remaining_indexes, 1)
            client.start(torrent_hash)
            selected_indexes = {index for index, _row in choices.values()}
            downloaded = _wait_for_download_many(
                client,
                torrent_hash,
                selected_indexes,
                timeout_seconds=download_timeout_seconds,
                stall_timeout_seconds=stall_timeout_seconds,
                progress_interval_seconds=progress_interval_seconds,
            )
            return torrent_hash, resolve_paths(choices, downloaded)

        selected_indexes = {index for index, _row in choices.values()}
        skip = [index for index in all_indexes if index not in selected_indexes]
        if skip:
            client.set_file_priority(torrent_hash, skip, 0)
        client.set_file_priority(torrent_hash, sorted(selected_indexes), 1)
        if len(choices) > 1:
            labels = ", ".join(f"E{episode}" for episode in sorted(choices))
            print(f"  pack targets: {labels}", flush=True)
        client.start(torrent_hash)
        downloaded = _wait_for_download_many(
            client,
            torrent_hash,
            selected_indexes,
            timeout_seconds=download_timeout_seconds,
            stall_timeout_seconds=stall_timeout_seconds,
            progress_interval_seconds=progress_interval_seconds,
        )
        return torrent_hash, resolve_paths(choices, downloaded)
    except Exception:
        try:
            client.delete(torrent_hash, delete_files=True)
        except Exception:
            pass
        raise

def _download_release_video(
    client: Any,
    release,
    *,
    episode: int,
    root: Path,
    metadata_timeout_seconds: float,
    download_timeout_seconds: float,
    max_target_bytes: int | None = None,
    stall_timeout_seconds: float = 90.0,
    progress_interval_seconds: float = 10.0,
) -> tuple[str, Path]:
    torrent_hash, paths = _download_release_videos(
        client,
        release,
        episodes=[episode],
        root=root,
        metadata_timeout_seconds=metadata_timeout_seconds,
        download_timeout_seconds=download_timeout_seconds,
        max_target_bytes=max_target_bytes,
        stall_timeout_seconds=stall_timeout_seconds,
        progress_interval_seconds=progress_interval_seconds,
    )
    return torrent_hash, paths[int(episode)]


_BENCHMARK_RESOLUTION_RE = re.compile(r"(?<!\d)(2160|1440|1080|720|576|540|480|360)p(?!\d)", re.IGNORECASE)


def _benchmark_resolution_height(title: str) -> int | None:
    match = _BENCHMARK_RESOLUTION_RE.search(str(title or ""))
    return int(match.group(1)) if match else None


def _benchmark_resolution_limit(value: str) -> int | None:
    text = str(value or "").casefold().strip()
    if text in {"any", "highest", "best", "max", "higher"}:
        return None
    match = re.search(r"(2160|1440|1080|720|576|540|480|360)", text)
    return int(match.group(1)) if match else None


def _filter_benchmark_resolution(releases: list[Any], resolution: str) -> list[Any]:
    maximum = _benchmark_resolution_limit(resolution)
    if maximum is None:
        return list(releases)
    # Benchmark media never needs pixels above the requested ceiling. Unknown
    # resolutions are rejected as well, otherwise a hidden 1080p/4K release can
    # silently defeat the bandwidth cap. Preserve Nyaa's score order afterwards.
    return [
        release
        for release in releases
        if (height := _benchmark_resolution_height(getattr(release, "title", ""))) is not None
        and height <= maximum
    ]


_PART_NUMBER_RE = re.compile(r"(?i)\bpart[ ._-]*0*(\d{1,2})\b")


def _benchmark_anime_from_row(row: dict[str, Any]) -> LibraryAnime:
    media_id = int(row.get("media_id") or 0)
    return LibraryAnime(
        media_id=media_id,
        title=str(row.get("title") or ""),
        titles=[str(value) for value in row.get("titles") or []],
        synonyms=[str(value) for value in row.get("synonyms") or []],
        site_url=f"https://anilist.co/anime/{media_id}" if media_id > 0 else "",
        episodes=int(row["episodes"]) if row.get("episodes") else None,
        format=str(row.get("format") or "") or None,
        season_year=int(row["season_year"]) if row.get("season_year") else None,
        media_status=str(row.get("status") or "") or None,
        mean_score=int(row["mean_score"]) if row.get("mean_score") is not None else None,
        duration=int(row["duration"]) if row.get("duration") is not None else None,
        relations=[dict(item) for item in row.get("relations") or [] if isinstance(item, dict)],
    )


def _target_identity_titles(row: dict[str, Any]) -> list[str]:
    values = [str(row.get("title") or "")]
    values.extend(str(value) for value in row.get("titles") or [])
    values.extend(str(value) for value in row.get("synonyms") or [])
    return [value for value in dict.fromkeys(value.strip() for value in values) if value]


def _source_identity_mismatch_reason(row: dict[str, Any], source_title: str) -> str | None:
    """Thin benchmark wrapper over the production Nyaa identity contract."""
    anime = _benchmark_anime_from_row(row)
    if release_identity_mismatch_reason(anime, str(source_title or "")) is not None:
        return "cross_season_source_mismatch"

    # Part identity is benchmark-specific because the plan may explicitly target
    # a named cour/part even when AniList titles do not expose a numeric season.
    targets = _target_identity_titles(row)
    target_parts = {
        int(match.group(1))
        for value in targets
        for match in [_PART_NUMBER_RE.search(value)]
        if match
    }
    source_part_match = _PART_NUMBER_RE.search(str(source_title or ""))
    source_part = int(source_part_match.group(1)) if source_part_match else None
    if target_parts and source_part is not None and source_part not in target_parts:
        return "cross_part_source_mismatch"
    return None


def _diagnostic_adversarial_reasons(row: dict[str, Any]) -> list[str]:
    source = row.get("source_release") if isinstance(row.get("source_release"), dict) else {}
    source_title = str(source.get("title") or row.get("source_release_name") or "")
    target = {
        "title": str(row.get("title") or ""),
        "titles": [str(row.get("title") or "")],
        # Old v72 diagnostic payloads do not contain relation metadata. Final
        # Season / explicit target contradictions are still safely detectable.
    }
    mismatch = _source_identity_mismatch_reason(target, source_title)
    reasons: list[str] = []
    if mismatch:
        reasons.append(mismatch)
        replay = row.get("current_pudge") if isinstance(row.get("current_pudge"), dict) else {}
        if bool(replay.get("accepted")):
            reasons.append("false_accept_wrong_media")
    return reasons


def _source_special_mismatch(row: dict[str, Any], title: str) -> bool:
    format_value = str(row.get("format") or "").upper()
    if format_value in {"OVA", "ONA", "SPECIAL", "MOVIE", "MUSIC"}:
        return False
    return bool(re.search(r"(?i)\b(?:special|ova|oad|ncop|nced|extra)\b", str(title or "")))


def _benchmark_batch_release(release: Any) -> bool:
    return bool(getattr(release, "is_batch", False) or release_episode_range(str(getattr(release, "title", ""))))


_NETFLIX_SOURCE_RE = re.compile(
    r"(?i)(?:\bnetflix\b|\bNF[ ._-]?(?:WEB[- .]?DL|WEBRIP)\b|\bNF\b(?=.*\bWEB))"
)
_ERAI_RE = re.compile(r"(?i)\bErai[- ._]?raws\b")
_MULTISUB_RE = re.compile(r"(?i)\b(?:multisub|multi[ ._-]?sub|multiple[ ._-]?subtitles?)\b")
_WEB_SOURCE_RE = re.compile(r"(?i)\b(?:WEB[- .]?DL|WEBRIP|WEB)\b")
_JP_SUB_HINT_RE = re.compile(r"(?i)\b(?:jpn?|japanese)[ ._-]?(?:sub|subs|subtitle|subtitles)\b")
_JP_BRACKET_TAG_RE = re.compile(r"(?i)\[(?:JP|JPN|JAPANESE)\]")
_ENGLISH_BRACKET_TAG_RE = re.compile(r"(?i)\[(?:EN|ENG|ENGLISH)\]")
_JP_TOKEN_RE = re.compile(r"(?i)(?<![A-Z])(?:JP|JPN)(?![A-Z])")

_DEFAULT_BENCHMARK_FEED_QUERIES = (
    "Erai [ENG] [JP] 480",
    "Erai [ENG] [JPN] 480",
    "Erai NF 720",
)
_BENCHMARK_FEED_ANILIST_CACHE_SCHEMA = "pudge-subtitle-benchmark-feed-anilist-cache-v1"


def _benchmark_has_explicit_jp_signal(title: str) -> bool:
    """True only when the release title itself explicitly advertises Japanese subtitles."""
    value = str(title or "")
    return bool(
        _JP_BRACKET_TAG_RE.search(value)
        or _JP_SUB_HINT_RE.search(value)
        or (_MULTISUB_RE.search(value) and _JP_TOKEN_RE.search(value))
    )



_SUBTITLE_LANGUAGE_TAG_RE = re.compile(r"\[([A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?)\]")
_SUBTITLE_LANGUAGE_CODES = {
    "ARA", "CES", "CHI", "DAN", "DEU", "DUT", "ENG", "FIN", "FRA", "FRE",
    "GER", "GRE", "HUN", "ITA", "JPN", "KOR", "NOB", "POL", "POR", "POR-BR",
    "ROM", "RUM", "RUS", "SLO", "SPA", "SPA-LA", "SWE", "TUR", "UKR",
    "JA", "JP", "EN", "FR", "DE", "ES", "IT", "KO", "ZH",
}
_JAPANESE_LANGUAGE_CODES = {"JA", "JP", "JPN"}


def _benchmark_declared_subtitle_languages(title: str) -> tuple[str, ...]:
    """Parse an explicit MultiSub language list from release-title tags."""
    value = str(title or "")
    if not _MULTISUB_RE.search(value):
        return ()
    tags = {
        match.group(1).upper()
        for match in _SUBTITLE_LANGUAGE_TAG_RE.finditer(value)
        if match.group(1).upper() in _SUBTITLE_LANGUAGE_CODES
    }
    # One short tag can be a release/codec marker. Two language tags (or an
    # explicit JP tag) are enough to treat the list as publisher-declared.
    if len(tags) < 2 and not tags.intersection(_JAPANESE_LANGUAGE_CODES):
        return ()
    return tuple(sorted(tags))


def _benchmark_declares_no_japanese_subtitles(title: str) -> bool:
    languages = _benchmark_declared_subtitle_languages(title)
    return bool(languages and not set(languages).intersection(_JAPANESE_LANGUAGE_CODES))


def _benchmark_release_family_key(release_or_source: Any) -> str:
    """Collapse 480p/720p encodes when group, episode range and language list match."""
    if isinstance(release_or_source, dict):
        title = str(release_or_source.get("title") or "")
        group = str(release_or_source.get("group") or "")
    else:
        title = str(getattr(release_or_source, "title", "") or "")
        group = str(getattr(release_or_source, "group", "") or "")
    languages = _benchmark_declared_subtitle_languages(title)
    if not languages:
        return ""
    if not group:
        group_match = re.match(r"^\s*\[([^\]]+)\]", title)
        group = group_match.group(1) if group_match else ""
    core = re.sub(r"^\s*\[[^\]]+\]\s*", " ", title)
    core = re.sub(r"\[[^\]]*\]", " ", core)
    core = _BENCHMARK_RESOLUTION_RE.sub(" ", core)
    core = re.sub(r"(?i)\b(?:batch|complete)\b", " ", core)
    core = re.sub(r"(?i)\.(?:mkv|mp4|m4v|avi|webm)$", " ", core.strip())
    core = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", " ", core, flags=re.UNICODE)
    core = " ".join(core.casefold().split())
    group_key = re.sub(r"[^a-z0-9]+", "", group.casefold())
    return f"{group_key}|{core}|{','.join(languages)}"


def _collect_source_rejections(corpus: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = Path(corpus) / "source_rejections.jsonl"
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _source_rejection_family_keys(corpus: Path) -> set[tuple[int, str]]:
    result: set[tuple[int, str]] = set()
    for row in _collect_source_rejections(corpus):
        try:
            media_id = int(row.get("media_id"))
            family = str(row.get("release_family_key") or "")
        except (TypeError, ValueError):
            continue
        if family:
            result.add((media_id, family))
    return result


def _record_source_rejection(
    corpus: Path,
    row: dict[str, Any],
    release: Any,
    *,
    reason: str,
) -> str:
    path = Path(corpus) / "source_rejections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    family = _benchmark_release_family_key(release)
    payload = {
        "schema": "pudge-subtitle-benchmark-source-rejection-v1",
        "observed_at": time.time(),
        "media_id": int(row["media_id"]),
        "episode": int(row["episode"]),
        "reason": str(reason),
        "release_family_key": family,
        "declared_subtitle_languages": list(
            _benchmark_declared_subtitle_languages(str(getattr(release, "title", "") or ""))
        ),
        "source_release": {
            "title": str(getattr(release, "title", "") or ""),
            "info_hash": str(getattr(release, "info_hash", "") or ""),
            "seeders": int(getattr(release, "seeders", 0) or 0),
            "is_batch": bool(_benchmark_batch_release(release)),
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return family


def _benchmark_source_affinity(title: str) -> tuple[int, str]:
    """Estimate how likely a release is to contain a Japanese text track.

    Explicit JP/JPN evidence is a separate top tier. Netflix is useful but still
    requires a one-episode probe when JP is not advertised. Erai MultiSub without
    JP is intentionally weak: the uploader/group alone must not justify a huge pack.
    """
    value = str(title or "")
    if _JP_BRACKET_TAG_RE.search(value):
        return 8, "jp-tag"
    if _JP_SUB_HINT_RE.search(value):
        return 7, "jp-sub-hint"
    if _MULTISUB_RE.search(value) and _JP_TOKEN_RE.search(value):
        return 6, "multisub-jp"
    if _NETFLIX_SOURCE_RE.search(value):
        return 4, "netflix"
    if _MULTISUB_RE.search(value) and _WEB_SOURCE_RE.search(value):
        return 2, "web-multisub"
    if _ERAI_RE.search(value) and _MULTISUB_RE.search(value):
        return 1, "erai-multisub-no-jp"
    return 0, "generic"


def _benchmark_resolution_preference(title: str) -> int:
    # 480p is intentionally preferred for corpus acquisition: pixels are
    # discarded after audio/subtitle extraction, so smaller video is cheaper.
    height = _benchmark_resolution_height(title)
    if height == 480:
        return 3
    if height in {540, 576}:
        return 2
    if height == 720:
        return 1
    return 0


def _benchmark_source_sort_key(release: Any) -> tuple[float, ...]:
    title = str(getattr(release, "title", ""))
    affinity, _label = _benchmark_source_affinity(title)
    multisub = bool(_MULTISUB_RE.search(title))
    seeders = max(0, int(getattr(release, "seeders", 0) or 0))
    batch = _benchmark_batch_release(release)
    score = float(getattr(release, "score", 0.0) or 0.0)
    size = max(0, int(getattr(release, "size_bytes", 0) or 0))
    # Oracle-likelihood first, then cheap 480p, then swarm quality. This avoids
    # spending hundreds of MiB on a release that is unlikely to contain a JP
    # text track. Seeders still dominate within the same source tier.
    return (
        float(affinity),
        float(_benchmark_resolution_preference(title)),
        1.0 if multisub else 0.0,
        float(min(seeders, 100)),
        1.0 if batch else 0.0,
        score,
        -float(size if not batch else 0),
    )


def _score_benchmark_extra_release(
    release: Any,
    anime: LibraryAnime,
    config: Any,
    *,
    episode: int,
    resolution: str,
) -> Any:
    batch = _benchmark_batch_release(release)
    return score_release(
        release,
        anime,
        episode=episode,
        batch=batch,
        trusted_groups=config.nyaa.trusted_groups,
        preferred_groups=config.nyaa.preferred_groups,
        blocked_groups=config.nyaa.blocked_groups,
        preferred_resolution="480p" if _benchmark_resolution_limit(resolution) and _benchmark_resolution_limit(resolution) >= 480 else resolution,
        min_seeders=max(1, int(config.nyaa.min_seeders)),
        target_episode_min_bytes=max(40 * 1024 * 1024, config.nyaa.episode_min_size_mb * 1024 * 1024),
        target_episode_max_bytes=config.nyaa.episode_max_size_mb * 1024 * 1024,
        preferred_video_codecs=config.nyaa.preferred_video_codecs,
        preferred_sources=config.nyaa.preferred_sources,
        require_japanese_audio=config.nyaa.require_japanese_audio,
        avoid_upscaled=config.nyaa.avoid_upscaled,
    )


def _benchmark_source_queries(row: dict[str, Any]) -> list[str]:
    aliases = [str(row.get("title") or "")]
    aliases.extend(str(value) for value in row.get("titles") or [])
    aliases.extend(str(value) for value in row.get("synonyms") or [])
    clean = list(dict.fromkeys(value.strip() for value in aliases if value.strip()))[:2]
    queries: list[str] = []
    for alias in clean:
        queries.extend(
            [
                f"{alias} JPN",
                f"{alias} JP",
                f"{alias} Japanese Subtitle",
                f"{alias} Multiple Subtitle JPN",
                f"{alias} Netflix",
                f"{alias} NF WEB-DL",
                f"{alias} Erai-raws",
            ]
        )
    return list(dict.fromkeys(queries))


def _benchmark_feed_release_is_eligible(
    release: Any,
    *,
    resolution: str,
    minimum_seeders: int,
) -> bool:
    """Fail-closed contract for the cheap Erai JP-tag and Netflix feeds."""
    title = str(getattr(release, "title", "") or "")
    height = _benchmark_resolution_height(title)
    maximum = _benchmark_resolution_limit(resolution)
    explicit_jp = bool(
        _ENGLISH_BRACKET_TAG_RE.search(title) and _JP_BRACKET_TAG_RE.search(title)
    )
    netflix = bool(_NETFLIX_SOURCE_RE.search(title))
    return bool(
        _ERAI_RE.search(title)
        and (explicit_jp or netflix)
        and not _benchmark_batch_release(release)
        and release_episode(title) is not None
        and height is not None
        and (maximum is None or height <= maximum)
        and max(0, int(getattr(release, "seeders", 0) or 0)) >= max(1, int(minimum_seeders))
        and int(getattr(release, "size_bytes", 0) or 0) > 0
    )


def _normalized_phrase_width(alias: str, release_title: str) -> int:
    alias_tokens = normalize_title(alias).split()
    release_tokens = normalize_title(release_title).split()
    if not alias_tokens or len(alias_tokens) > len(release_tokens):
        return 0
    width = len(alias_tokens)
    return width if any(
        release_tokens[index:index + width] == alias_tokens
        for index in range(len(release_tokens) - width + 1)
    ) else 0


def _match_benchmark_feed_release(
    release: Any,
    plan_rows: Iterable[dict[str, Any]],
    *,
    negative_titles_by_media: dict[int, tuple[str, ...]],
) -> tuple[dict[str, Any] | None, str]:
    """Map one feed release to exactly one AniList plan entry, or fail closed."""
    title = str(getattr(release, "title", "") or "")
    episode = release_episode(title)
    if episode is None:
        return None, "episode-missing"

    representatives: dict[int, dict[str, Any]] = {}
    for raw in plan_rows:
        try:
            media_id = int(raw.get("media_id"))
        except (TypeError, ValueError):
            continue
        representatives.setdefault(media_id, raw)

    matches: list[tuple[int, float, int, dict[str, Any]]] = []
    for media_id, raw in representatives.items():
        anime = _benchmark_anime_from_row(raw)
        if not release_title_is_plausible(
            anime,
            title,
            negative_titles=negative_titles_by_media.get(media_id, ()),
        ):
            continue
        aliases = _target_identity_titles(raw)
        phrase_width = max(
            (_normalized_phrase_width(alias, title) for alias in aliases),
            default=0,
        )
        # A global feed must be stricter than a title-specific Nyaa result. A
        # complete plan alias has to occur in the release title; fuzzy-only
        # matches are rejected instead of risking a wrong anime benchmark.
        if phrase_width <= 0:
            continue
        similarity = max((title_similarity(alias, title) for alias in aliases), default=0.0)
        total_episodes = int(raw.get("episodes") or 0)
        if total_episodes > 0 and int(episode) > total_episodes:
            continue
        matches.append((phrase_width, similarity, media_id, raw))

    if not matches:
        return None, "no-exact-plan-title"
    matches.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
    best = matches[0]
    if len(matches) > 1:
        second = matches[1]
        if best[0] == second[0] and abs(best[1] - second[1]) < 1.0:
            return None, "ambiguous-plan-title"

    row = dict(best[3])
    row["episode"] = int(episode)
    row["feed_release_title"] = title
    return row, "matched"


def _benchmark_feed_identity(release: Any) -> Any:
    """Parse the publisher/tags away and retain the actual series + episode."""
    raw_title = str(getattr(release, "title", "") or "")
    # Feed entries are display names rather than filesystem paths. Protect a
    # legitimate title slash (for example ``Ranma 1/2``) from ``Path.name`` in
    # the general filename parser, then restore it in the parsed identity.
    slash_marker = "PUDGEFEEDSLASH"
    identity = parse_anime_filename(raw_title.replace("/", slash_marker))
    identity.title = identity.title.replace(slash_marker, "/")
    identity.raw_name = raw_title
    return identity


def _benchmark_feed_anilist_row(
    identity: Any,
    candidates: Iterable[Any],
    *,
    release_title: str,
) -> tuple[dict[str, Any] | None, str]:
    """Select a unique AniList identity for one global-feed filename."""
    identity_title = str(getattr(identity, "title", "") or "").strip()
    identity_norm = normalize_title(identity_title)
    episode = getattr(identity, "episode", None)
    if not identity_norm or episode is None:
        return None, "feed-identity-missing"

    ranked: list[tuple[int, float, float, int, Any]] = []
    for candidate in candidates:
        try:
            media_id = int(getattr(candidate, "id"))
        except (TypeError, ValueError):
            continue
        total_episodes = int(getattr(candidate, "episodes", 0) or 0)
        if total_episodes > 0 and int(episode) > total_episodes:
            continue
        names = [
            str(value).strip()
            for value in [
                *(getattr(candidate, "titles", []) or []),
                *(getattr(candidate, "synonyms", []) or []),
            ]
            if str(value).strip()
        ]
        if not names:
            continue
        exact = int(any(normalize_title(name) == identity_norm for name in names))
        similarity = max(title_similarity(identity_title, name) for name in names)
        # AniList search ordering is useful only after our own strict title
        # evidence. This prevents a popular franchise root from winning over a
        # less popular but exact sequel/cour entry.
        ranked.append(
            (
                exact,
                similarity,
                float(getattr(candidate, "score", 0.0) or 0.0),
                -media_id,
                candidate,
            )
        )

    if not ranked:
        return None, "anilist-no-candidate"
    ranked.sort(key=lambda item: item[:4], reverse=True)
    best = ranked[0]
    if not best[0] and best[1] < 95.0:
        return None, "anilist-no-exact-title"
    if len(ranked) > 1:
        second = ranked[1]
        if best[0] == second[0] and abs(best[1] - second[1]) < 2.0:
            return None, "anilist-ambiguous-title"

    candidate = best[4]
    titles = [str(value) for value in (getattr(candidate, "titles", []) or []) if value]
    synonyms = [
        str(value) for value in (getattr(candidate, "synonyms", []) or []) if value
    ]
    media_id = int(getattr(candidate, "id"))
    row = {
        "rank": None,
        "media_id": media_id,
        "title": titles[0] if titles else identity_title,
        "titles": list(dict.fromkeys(titles)),
        "synonyms": list(dict.fromkeys(synonyms)),
        "episodes": int(getattr(candidate, "episodes", 0) or 0) or None,
        "format": getattr(candidate, "format", None),
        "season_year": getattr(candidate, "season_year", None),
        "relations": [],
        "episode": int(episode),
        "feed_release_title": str(release_title),
        "feed_identity_source": "anilist-search",
    }
    return row, "matched"


def _load_benchmark_feed_anilist_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not Path(path).is_file():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != _BENCHMARK_FEED_ANILIST_CACHE_SCHEMA:
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in entries.items()
        if isinstance(value, dict) and value.get("media_id")
    }


def _store_benchmark_feed_anilist_cache(
    path: Path | None,
    entries: dict[str, dict[str, Any]],
) -> None:
    if path is None:
        return
    write_json(
        Path(path),
        {
            "schema": _BENCHMARK_FEED_ANILIST_CACHE_SCHEMA,
            "updated_at": time.time(),
            "entries": entries,
        },
    )


def _benchmark_feed_cached_row(
    identity: Any,
    release_title: str,
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    key = normalize_title(str(getattr(identity, "title", "") or ""))
    episode = getattr(identity, "episode", None)
    cached = entries.get(key)
    if not key or episode is None or not isinstance(cached, dict):
        return None
    total_episodes = int(cached.get("episodes") or 0)
    if total_episodes > 0 and int(episode) > total_episodes:
        return None
    row = dict(cached)
    row["episode"] = int(episode)
    row["feed_release_title"] = str(release_title)
    row["feed_identity_source"] = "anilist-cache"
    return row


def _benchmark_feed_published_timestamp(release: Any) -> float:
    value = str(getattr(release, "published", "") or "").strip()
    if not value:
        return 0.0
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return float(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _benchmark_feed_release_sort_key(release: Any) -> tuple[float, ...]:
    """Prefer active swarms, then recent releases within the same seed tier."""
    seeders = max(0, int(getattr(release, "seeders", 0) or 0))
    leechers = max(0, int(getattr(release, "leechers", 0) or 0))
    downloads = max(0, int(getattr(release, "downloads", 0) or 0))
    size = max(0, int(getattr(release, "size_bytes", 0) or 0))
    return (
        float(seeders),
        1.0 if leechers > 0 else 0.0,
        _benchmark_feed_published_timestamp(release),
        float(min(leechers, 100)),
        float(min(downloads, 100_000)),
        -float(size),
    )


def _discover_benchmark_feed(
    plan_rows: list[dict[str, Any]],
    config: Any,
    *,
    queries: Iterable[str],
    category: str,
    resolution: str,
    minimum_seeders: int,
    negative_titles_by_media: dict[int, tuple[str, ...]],
    anilist_cache_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[tuple[int, int], list[Any]], dict[str, int]]:
    """Search the strict Erai JP/NF feeds once, then map episodes to AniList rows."""
    clean_queries = list(
        dict.fromkeys(str(value).strip() for value in queries if str(value).strip())
    )
    if not clean_queries:
        clean_queries = list(_DEFAULT_BENCHMARK_FEED_QUERIES)
    requested_maximum = _benchmark_resolution_limit(resolution)
    feed_resolution = f"{requested_maximum}p" if requested_maximum else resolution

    client = NyaaClient(
        config.nyaa.base_url,
        proxy_mode=config.nyaa.proxy_mode,
        proxy_url=config.nyaa.proxy_url,
        pre_search_command=config.nyaa.pre_search_command,
        category=category,
    )
    found: dict[str, Any] = {}
    errors: list[str] = []
    try:
        for query in clean_queries:
            try:
                releases = client.search(
                    query,
                    category=category,
                    filter_id=0,
                    sort_by="seeders",
                    order="desc",
                )
            except NyaaError as exc:
                errors.append(f"{query}: {exc}")
                continue
            for release in releases:
                key = str(
                    getattr(release, "info_hash", "")
                    or getattr(release, "torrent_url", "")
                    or getattr(release, "link", "")
                )
                if key and key not in found:
                    found[key] = release
    finally:
        client.close()

    if not found and errors:
        raise NyaaError("; ".join(dict.fromkeys(errors)))

    eligible = [
        release
        for release in found.values()
        if _benchmark_feed_release_is_eligible(
            release,
            resolution=feed_resolution,
            minimum_seeders=minimum_seeders,
        )
    ]
    eligible.sort(key=_benchmark_feed_release_sort_key, reverse=True)

    rows_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    releases_by_key: dict[tuple[int, int], list[Any]] = {}
    cache_entries = _load_benchmark_feed_anilist_cache(anilist_cache_path)
    anilist_client: AniListClient | None = None
    plan_matched = 0
    anilist_matched = 0
    cache_matched = 0
    anilist_errors = 0
    unmatched = 0
    ambiguous = 0
    try:
        for release in eligible:
            row, reason = _match_benchmark_feed_release(
                release,
                plan_rows,
                negative_titles_by_media=negative_titles_by_media,
            )
            if row is not None:
                row["feed_identity_source"] = "top1000-plan"
                plan_matched += 1
            else:
                identity = _benchmark_feed_identity(release)
                row = _benchmark_feed_cached_row(identity, str(release.title), cache_entries)
                if row is not None:
                    cache_matched += 1
                else:
                    anilist_config = getattr(config, "anilist", None)
                    endpoint = str(getattr(anilist_config, "endpoint", "") or "").strip()
                    if not endpoint:
                        unmatched += 1
                        continue
                    if anilist_client is None:
                        anilist_client = AniListClient(
                            endpoint,
                            str(getattr(anilist_config, "access_token", "") or ""),
                            timeout=20.0,
                        )
                    try:
                        candidates = anilist_client.search(identity)
                    except AniListError:
                        anilist_errors += 1
                        continue
                    row, reason = _benchmark_feed_anilist_row(
                        identity,
                        candidates,
                        release_title=str(release.title),
                    )
                    if row is None:
                        ambiguous += int("ambiguous" in reason)
                        unmatched += int("ambiguous" not in reason)
                        continue
                    anilist_matched += 1
                    cache_key = normalize_title(str(getattr(identity, "title", "") or ""))
                    cached_row = dict(row)
                    cached_row.pop("episode", None)
                    cached_row.pop("feed_release_title", None)
                    cached_row["feed_identity_source"] = "anilist-cache"
                    cache_entries[cache_key] = cached_row
                    # Persist after every resolved title so Ctrl+C or a network
                    # interruption never makes the next run start from zero.
                    _store_benchmark_feed_anilist_cache(anilist_cache_path, cache_entries)

            key = (int(row["media_id"]), int(row["episode"]))
            anime = _benchmark_anime_from_row(row)
            scored = _score_benchmark_extra_release(
                release,
                anime,
                config,
                episode=int(row["episode"]),
                resolution=feed_resolution,
            )
            rows_by_key.setdefault(key, row)
            releases_by_key.setdefault(key, []).append(scored)
    finally:
        if anilist_client is not None:
            anilist_client.close()

    stats = {
        "raw": len(found),
        "eligible": len(eligible),
        "matched": len(rows_by_key),
        "plan_matched": plan_matched,
        "anilist_matched": anilist_matched,
        "cache_matched": cache_matched,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "anilist_errors": anilist_errors,
    }
    return list(rows_by_key.values()), releases_by_key, stats


def _concise_nyaa_error(exc: Exception) -> str:
    text = str(exc).strip()
    if "temporarily paused after repeated network failures" in text.casefold():
        return "Nyaa temporarily paused after repeated network failures"
    parts = [part.strip() for part in text.split(";") if part.strip()]
    value = parts[0] if parts else (text or exc.__class__.__name__)
    return value if len(value) <= 320 else value[:317] + "..."


def _benchmark_resumable_releases(
    downloader: Any,
    row: dict[str, Any],
    *,
    episodes: Iterable[int],
    root: Path,
    negative_titles: tuple[str, ...] = (),
) -> list[NyaaRelease]:
    requested = list(dict.fromkeys(int(value) for value in episodes if int(value) > 0))
    if not requested:
        return []
    first_episode = requested[0]
    anime = _benchmark_anime_from_row(row)
    target_media_id = int(row["media_id"])
    try:
        items = list(downloader.torrents())
    except Exception:
        return []

    resumable: list[NyaaRelease] = []
    for item in items:
        existing_root = Path(str(getattr(item, "save_path", "") or "")).expanduser()
        try:
            benchmark_owned = existing_root.resolve() == root.resolve()
        except OSError:
            benchmark_owned = False
        if not benchmark_owned:
            continue

        title = str(getattr(item, "name", "") or "").strip()
        torrent_hash = str(getattr(item, "torrent_hash", "") or "").strip()
        if not title or not torrent_hash:
            continue
        tagged_media_id = getattr(item, "media_id", None)
        if tagged_media_id is not None:
            try:
                if int(tagged_media_id) != target_media_id:
                    continue
            except (TypeError, ValueError):
                continue
        # Legacy benchmark torrents predate AniList identity tags. A matching
        # episode number is not enough: reuse production title identity and
        # fail closed so an unrelated anime can never be resumed here.
        if not release_title_is_plausible(
            anime, title, negative_titles=negative_titles
        ):
            continue
        if _source_special_mismatch(row, title):
            continue
        if _source_identity_mismatch_reason(row, title) is not None:
            continue
        try:
            files = downloader.files(torrent_hash)
        except Exception:
            continue
        choices = _video_file_choices(files, requested)
        if first_episode not in choices:
            continue
        first_name = str(choices[first_episode][1].get("name") or "")
        if _source_identity_mismatch_reason(row, first_name) is not None:
            continue

        raw = getattr(item, "raw", {}) or {}
        total_size = max(
            0,
            int(raw.get("total_size") or 0),
            sum(max(0, int(file_row.get("size") or 0)) for file_row in files),
        )
        seeders = max(
            0,
            int(raw.get("listed_seeders") or 0),
            int(raw.get("num_seeders") or 0),
        )
        is_batch = bool(
            getattr(item, "is_batch", False)
            or release_episode_range(title)
            or len(choices) > 1
        )
        resumable.append(
            NyaaRelease(
                title=title,
                link="",
                torrent_url="",
                info_hash=torrent_hash,
                size_text=_human_bytes(total_size),
                size_bytes=total_size,
                seeders=seeders,
                leechers=max(0, int(raw.get("listed_leechers") or 0)),
                downloads=0,
                trusted=False,
                remake=False,
                score=1_000_000.0,
                is_batch=is_batch,
                group="",
            )
        )
    return resumable



_STRESS_EPISODIC_FORMATS = {"TV", "TV_SHORT", "OVA", "ONA"}

_STRESS_LIBRARY_IGNORE_MARKER = ".pudge-ignore-library-scan"
_STRESS_CONFLICTING_SEQUEL_RE = re.compile(
    r"(?i)(?:"
    r"\bfinal[ ._-]+season\b|"
    r"\bseason[ ._-]*0*(?:[2-9]|[1-9]\d)\b|"
    r"\bS0*(?:[2-9]|[1-9]\d)E\d+\b|"
    r"\b(?:2nd|3rd|4th|5th|6th|7th|8th|9th|10th)[ ._-]+season\b|"
    r"\bpart[ ._-]*0*(?:[2-9]|[1-9]\d)\b"
    r")"
)


def _stress_discovery_aliases(row: dict[str, Any], *, limit: int = 3) -> list[str]:
    """Small, high-value alias set for stress discovery.

    Stress mode intentionally avoids the old JP/JPN/Netflix probing fan-out: it
    benchmarks normal episode acquisition first and lets the subtitle pipeline
    independently find Japanese subtitles afterwards.
    """
    values = [str(row.get("title") or "")]
    values.extend(str(value) for value in row.get("titles") or [])
    values.extend(str(value) for value in row.get("synonyms") or [])
    clean = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return clean[: max(1, int(limit))]


def _stress_discovery_queries(row: dict[str, Any], *, batch: bool) -> list[str]:
    episode = int(row["episode"])
    aliases = _stress_discovery_aliases(row)
    queries: list[str] = []
    if not batch:
        for alias in aliases:
            queries.extend(
                [
                    f"{alias} {episode:02d}",
                    f"{alias} E{episode:02d}",
                ]
            )
            if episode < 10:
                queries.append(f"{alias} {episode}")
    else:
        for alias in aliases[:2]:
            queries.extend([alias, f"{alias} batch", f"{alias} complete"])
    return list(dict.fromkeys(queries))


def _stress_parsed_release_title(source_title: str) -> str:
    # parse_anime_filename treats '/' as a path separator; preserve legitimate
    # titles such as Ranma 1/2 while parsing the release identity.
    marker = "PUDGESTRESSSLASH"
    try:
        identity = parse_anime_filename(str(source_title or "").replace("/", marker))
        return str(getattr(identity, "title", "") or "").replace(marker, "/").strip()
    except Exception:
        return ""


def _stress_release_alias_matches_target(
    row: dict[str, Any],
    source_title: str,
) -> bool:
    """Strict title evidence that intentionally ignores season-relation policy.

    Production ``release_title_is_plausible`` also applies cross-season guards.
    Stress identity needs the title-only part separately so it can decide whether
    an explicit S02/S06 marker is actually compatible with the exact AniList
    target.  Keeping this helper title-only avoids the circular r1/r2 failure
    where the cross-season rejection prevented the stress override from ever
    seeing a valid translated alias such as Nagatoro ``2nd Attack``.
    """
    parsed = _stress_parsed_release_title(source_title)
    parsed_norm = normalize_title(parsed)
    if not parsed_norm:
        return False
    parsed_tokens = parsed_norm.split()
    for alias in _stress_discovery_aliases(row, limit=8):
        alias_norm = normalize_title(alias)
        if not alias_norm:
            continue
        if parsed_norm == alias_norm:
            return True
        alias_tokens = alias_norm.split()
        # One-word anime names are too ambiguous for substring matching. This
        # blocks cases such as target "Another" accidentally matching
        # "16bit Sensation - Another Layer".
        if len(alias_tokens) == 1:
            continue
        width = len(alias_tokens)
        if width <= len(parsed_tokens) and any(
            parsed_tokens[index:index + width] == alias_tokens
            for index in range(len(parsed_tokens) - width + 1)
        ):
            return True
    return False


def _stress_release_title_is_plausible(
    row: dict[str, Any],
    source_title: str,
    *,
    negative_titles: tuple[str, ...] = (),
) -> bool:
    anime = _benchmark_anime_from_row(row)
    if not release_title_is_plausible(anime, source_title, negative_titles=negative_titles):
        return False
    return _stress_release_alias_matches_target(row, source_title)


def _stress_target_season_hint(row: dict[str, Any]) -> int | None:
    """Best-effort season number declared by the target AniList titles.

    This is deliberately conservative and only uses explicit sequel markers.
    It exists to distinguish a correct target such as Nagatoro ``2nd Attack`` /
    BNHA ``6`` from a target season-one title accidentally matched to an S02+
    release.
    """
    hints: set[int] = set()
    roman = {
        "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
        "VII": 7, "VIII": 8, "IX": 9, "X": 10,
    }
    for value in _target_identity_titles(row):
        text = str(value or "")
        for pattern in (
            r"(?i)\bseason[ ._-]*0*(\d{1,2})\b",
            r"(?i)\bS0*(\d{1,2})(?!E\d)\b",
            r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\b",
        ):
            for match in re.finditer(pattern, text):
                number = int(match.group(1))
                if 2 <= number <= 20:
                    hints.add(number)
        trailing = re.search(r"(?i)(?:^|[\s:._-])([2-9]|1\d)(?=$|[\s:._-])", text)
        if trailing:
            hints.add(int(trailing.group(1)))
        for token, number in roman.items():
            if re.search(rf"(?:^|[\s:._-]){token}(?=$|[\s:._-])", text):
                hints.add(number)
    return next(iter(hints)) if len(hints) == 1 else None


def _stress_source_season_hints(source_title: str) -> set[int]:
    text = str(source_title or "")
    hints: set[int] = set()
    for pattern in (
        r"(?i)\bS0*(\d{1,2})(?:E\d+)?\b",
        r"(?i)\bseason[ ._-]*0*(\d{1,2})\b",
        r"(?i)\b(\d{1,2})(?:st|nd|rd|th)[ ._-]+season\b",
    ):
        for match in re.finditer(pattern, text):
            number = int(match.group(1))
            if 1 <= number <= 20:
                hints.add(number)
    return hints


def _stress_source_identity_mismatch_reason(
    row: dict[str, Any],
    source_title: str,
    negative_titles: tuple[str, ...] = (),
) -> str | None:
    """Stress identity contract with target-aware season tolerance.

    Production relation checks intentionally fail closed for explicit S02+/Part
    2+ markers.  Stress rows, however, already carry the exact AniList target and
    many valid releases spell the season only in the torrent title (Nagatoro
    ``2nd Attack`` -> ``S02`` is the concrete regression).  A production
    cross-season warning may therefore be relaxed only when the release title is
    otherwise a strict target-title match *and* every explicit source season
    agrees with an explicit target season hint.
    """
    mismatch = _source_identity_mismatch_reason(row, source_title)
    if mismatch != "cross_season_source_mismatch":
        return mismatch
    # At this point production rejected *because of season policy*.  Do not call
    # ``_stress_release_title_is_plausible`` here: that function intentionally
    # includes the same production policy and would make this override
    # unreachable.  Require strict target-title evidence instead, then validate
    # the explicit season marker below.
    if not _stress_release_alias_matches_target(row, source_title):
        return mismatch

    source_seasons = _stress_source_season_hints(source_title)
    if source_seasons:
        target_season = _stress_target_season_hint(row)
        if target_season is None or source_seasons != {target_season}:
            return mismatch
        return None

    source = str(source_title or "")
    if re.search(r"(?i)\bfinal[ ._-]+season\b", source):
        target_names = " ".join(_target_identity_titles(row))
        return None if re.search(r"(?i)\bfinal[ ._-]+season\b", target_names) else mismatch

    source_part = _PART_NUMBER_RE.search(source)
    if source_part and int(source_part.group(1)) >= 2:
        target_parts = {
            int(match.group(1))
            for value in _target_identity_titles(row)
            for match in [_PART_NUMBER_RE.search(value)]
            if match
        }
        return None if int(source_part.group(1)) in target_parts else mismatch

    # No explicit sequel marker remains: keep the older tolerance for false
    # relation-graph season warnings such as To Be Hero X.
    return None


def _stress_explicit_single_episode(title: str) -> int | None:
    """Return an explicitly written single episode, not a season/range number."""
    text = str(title or "")

    # Strong single-episode spellings win before generic range detection.  This
    # matters for sequel titles like ``... 2 - 05 | ... S2`` where a loose
    # range parser can misread the title's ``2 - 05`` as an episode range.
    for pattern in (
        r"(?i)\bS\d{1,2}E0*(\d{1,3})\b",
        r"(?i)\s-\s0*(\d{1,3})(?=\s*(?:$|[|\[(]))",
        r"(?i)(?:^|[\s._-])E0*(\d{1,3})(?=$|[\s._\-[(])",
    ):
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None

    if release_episode_range(text) is not None:
        return None
    return None



def _stress_source_confirms_video_season(source_title: str, video_name: str) -> bool:
    """Treat the torrent title as season context for terse SxxExx filenames."""
    try:
        identity = parse_anime_filename(str(video_name or ""))
        season = int(identity.season) if identity.season is not None else None
    except Exception:
        return False
    if season is None:
        return False
    source = str(source_title or "")
    patterns = (
        rf"(?i)\bS0*{season}(?:E\d+)?\b",
        rf"(?i)\bseason[ ._-]*0*{season}\b",
        rf"(?i)\b{season}(?:st|nd|rd|th)[ ._-]+season\b",
    )
    return any(re.search(pattern, source) for pattern in patterns)


_STRESS_IDENTITY_SUFFIX_NOISE = {
    "animation", "the", "uncensored", "uncut", "remaster",
    "remastered", "repack", "v2", "v3", "complete", "batch",
}
_STRESS_NEGATIVE_DISTINCTIVE_STOPWORDS = {
    "season", "part", "cour", "series", "movie", "special",
    "specials", "episode", "episodes", "the", "and",
}
_STRESS_LEADING_EPISODE_RE = re.compile(
    r"(?i)^(?:S\d{1,2}E\d{1,4}|(?:EP?|Episode)[ ._-]*\d{1,4})(?:$|[ ._-])"
)
_STRESS_VIDEO_SPECIAL_RE = re.compile(
    r"(?i)\b(?:specials?|ova|oad|ncop|nced|extra)\b"
)


def _stress_title_tokens(value: str) -> list[str]:
    return [token for token in normalize_title(value).split() if token]


def _stress_target_alias_token_sets(row: dict[str, Any]) -> list[list[str]]:
    return [
        tokens
        for value in _stress_discovery_aliases(row, limit=12)
        if (tokens := _stress_title_tokens(value))
    ]


def _stress_identity_video_title(video_name: str) -> str:
    parsed = _stress_parsed_release_title(video_name)
    # Parenthesized prefixes such as ``(CBB) Tokyo Ghoul`` are release groups,
    # not series identity. Bracketed groups were already stripped by the parser.
    return re.sub(r"^\([^)]{1,24}\)\s*", "", parsed).strip()


def _stress_video_starts_with_episode_label(video_name: str) -> bool:
    raw = Path(str(video_name or "")).name
    raw = re.sub(r"^(?:\[[^]]+\]\s*)+", "", raw).strip()
    return bool(_STRESS_LEADING_EPISODE_RE.search(raw))


def _stress_video_special_mismatch(row: dict[str, Any], video_name: str) -> bool:
    format_value = str(row.get("format") or "").upper()
    if format_value in {"OVA", "ONA", "SPECIAL", "MOVIE", "MUSIC"}:
        return False
    return bool(_STRESS_VIDEO_SPECIAL_RE.search(_stress_identity_video_title(video_name)))


def _stress_video_target_alias_safe(
    row: dict[str, Any],
    video_name: str,
) -> bool:
    """True when parsed filename is only target aliases + harmless metadata."""
    parsed_tokens = _stress_title_tokens(_stress_identity_video_title(video_name))
    if not parsed_tokens:
        return False
    aliases = _stress_target_alias_token_sets(row)
    if any(parsed_tokens == alias for alias in aliases):
        return True

    raw_parsed = _stress_identity_video_title(video_name)
    wrapped = re.fullmatch(r"\s*(.*?)\s*\(([^()]*)\)\s*", raw_parsed)
    if wrapped:
        outside = _stress_title_tokens(wrapped.group(1))
        inside = wrapped.group(2).strip()
        if any(outside == alias for alias in aliases):
            year_match = re.fullmatch(r"(?:19|20)\d{2}", inside)
            if year_match:
                try:
                    return int(inside) == int(row.get("season_year") or 0)
                except (TypeError, ValueError):
                    return False
            # Parenthesized textual names following an exact target alias are
            # overwhelmingly alternate translations, e.g. Rakudai/GATE.
            if inside and not re.search(r"(?i)\b(?:season|part|cour)\s*\d+\b", inside):
                return True

    # Some releases put romaji and English title next to each other.  Both are
    # authoritative target aliases, so their concatenation is safe.
    for left in aliases:
        for right in aliases:
            if left is right:
                continue
            if parsed_tokens == left + right:
                return True

    season_year = None
    try:
        season_year = int(row.get("season_year")) if row.get("season_year") else None
    except (TypeError, ValueError):
        season_year = None
    for alias in aliases:
        if len(alias) < 2 or len(parsed_tokens) <= len(alias):
            continue
        for index in range(len(parsed_tokens) - len(alias) + 1):
            if parsed_tokens[index:index + len(alias)] != alias:
                continue
            extras = parsed_tokens[:index] + parsed_tokens[index + len(alias):]
            remaining = []
            for token in extras:
                if token in _STRESS_IDENTITY_SUFFIX_NOISE:
                    continue
                if token in {"season", "s1", "01", "1"}:
                    continue
                if season_year is not None and token == str(season_year):
                    continue
                remaining.append(token)
            if not remaining:
                return True
            if any(remaining == other for other in aliases):
                return True
    return False


def _stress_video_title_extension_reason(
    row: dict[str, Any],
    video_name: str,
) -> str | None:
    """Detect a selected file that names a more-specific work than target."""
    if _stress_video_starts_with_episode_label(video_name):
        return None
    if _stress_video_target_alias_safe(row, video_name):
        return None
    parsed_tokens = _stress_title_tokens(_stress_identity_video_title(video_name))
    if not parsed_tokens:
        return None
    aliases = _stress_target_alias_token_sets(row)
    for alias in aliases:
        if len(alias) < 2 or len(parsed_tokens) <= len(alias):
            continue
        width = len(alias)
        if any(
            parsed_tokens[index:index + width] == alias
            for index in range(len(parsed_tokens) - width + 1)
        ):
            return "video_target_title_extension"
    return None


def _stress_video_matches_negative_title(
    row: dict[str, Any],
    source_title: str,
    video_name: str,
    negative_titles: tuple[str, ...],
) -> str | None:
    """Return a sibling title strongly supported by source + selected file.

    Exact matching was too weak for real releases: ``Vigilantes`` vs
    ``Vigilante: Boku no Hero Academia ILLEGALS`` and ``Owaranai Seraph
    Specials`` vs the AniList side-story title are the same work but not the same
    normalized string.  Fuzzy evidence is accepted only when both source and
    selected filename resemble the same negative title and the negative contributes
    at least one distinctive token absent from the target aliases.
    """
    parsed = _stress_identity_video_title(video_name)
    parsed_norm = normalize_title(parsed)
    if not parsed_norm:
        return None
    source_norm = normalize_title(source_title)
    target_tokens = {
        token
        for alias in _stress_target_alias_token_sets(row)
        for token in alias
    }
    source_tokens = set(source_norm.split())
    parsed_tokens = set(parsed_norm.split())

    for candidate in negative_titles:
        candidate_norm = normalize_title(candidate)
        if not candidate_norm:
            continue
        if parsed_norm == candidate_norm:
            return str(candidate)

        candidate_tokens = set(candidate_norm.split())
        distinctive = {
            token
            for token in candidate_tokens - target_tokens
            if len(token) >= 5
            and not token.isdigit()
            and token not in _STRESS_NEGATIVE_DISTINCTIVE_STOPWORDS
        }
        if not distinctive:
            continue
        source_score = title_similarity(source_title, candidate)
        video_score = title_similarity(parsed, candidate)
        if source_score < 84.0 or video_score < 82.0:
            continue
        if not any(
            token in source_tokens
            or token in parsed_tokens
            or f"{token}s" in source_tokens
            or f"{token}s" in parsed_tokens
            or (token.endswith("s") and token[:-1] in source_tokens | parsed_tokens)
            for token in distinctive
        ):
            continue
        return str(candidate)
    return None


def _stress_selected_video_rejection_reason(
    row: dict[str, Any],
    *,
    source_title: str,
    video_name: str,
    negative_titles: tuple[str, ...] = (),
) -> str | None:
    """Validate a downloaded stress target without requiring basename title parity.

    Discovery already validated the torrent title against AniList.  A selected
    file can legitimately use a translated alias or only ``S01E01 - Episode
    Title``.  Therefore basename title similarity is not a second acceptance
    gate.  The file only vetoes an otherwise-valid source when it provides
    stronger contradictory evidence: wrong explicit episode, a special/OVA for a
    normal TV target, or an exact known sibling title.
    """
    source_special = _source_special_mismatch(row, source_title)
    if source_special:
        return "source_special_source_mismatch"

    # Keep the production season verdict separate from the stress override.
    # v105 r1-r3 relaxed a proven false positive (e.g. Nagatoro ``2nd Attack``
    # whose torrent spells the same target as ``S02``), but then immediately ran
    # the same production plausibility check again and rejected it as
    # ``source_title_identity_mismatch``.  If the *only* production objection was
    # cross-season and the stricter stress helper accepted that exact title+season
    # combination, do not ask the contradictory production gate a second time.
    production_source_mismatch = _source_identity_mismatch_reason(row, source_title)
    source_mismatch = _stress_source_identity_mismatch_reason(
        row, source_title, negative_titles
    )
    if source_mismatch:
        return f"source_{source_mismatch}"

    source_title_plausible = _stress_release_title_is_plausible(
        row, source_title, negative_titles=negative_titles
    )
    stress_cross_season_override = bool(
        production_source_mismatch == "cross_season_source_mismatch"
        and source_mismatch is None
    )
    if not source_title_plausible and not stress_cross_season_override:
        return "source_title_identity_mismatch"

    expected_episode = int(row.get("episode") or 0)
    source_episode = _stress_explicit_single_episode(source_title)
    if expected_episode > 0 and source_episode is not None and source_episode != expected_episode:
        return "source_episode_mismatch"

    try:
        identity = parse_anime_filename(str(video_name or ""))
        video_episode = int(identity.episode) if identity.episode is not None else None
    except Exception:
        identity = None
        video_episode = None
    if expected_episode > 0 and video_episode is not None and video_episode != expected_episode:
        return "video_episode_mismatch"

    if _stress_video_special_mismatch(row, video_name):
        return "video_special_source_mismatch"

    # Episode-title-only filenames do not carry series identity in the basename.
    if _stress_video_starts_with_episode_label(video_name):
        return None

    # Exact/safe target aliases are authoritative.  Do not use the broader production
    # plausibility gate yet: it intentionally accepts title extensions and would
    # hide wrong franchise entries such as ``... Goddesses Arc`` / ``Kyoto
    # Douran`` before the stricter benchmark veto gets a chance to inspect them.
    if _stress_video_target_alias_safe(row, video_name):
        return None

    # Explicit target seasons (Nagatoro S02 / BNHA S06) may safely use a terse
    # basename once both source and file agree on that same target season.  Do this
    # narrow bypass before sibling fuzzing so a legitimate sequel is not confused
    # with its prequel title.
    target_season = _stress_target_season_hint(row)
    try:
        video_season = int(identity.season) if identity is not None and identity.season is not None else None
    except (TypeError, ValueError):
        video_season = None
    if (
        target_season is not None
        and video_season == target_season
        and _stress_source_confirms_video_season(source_title, video_name)
    ):
        return None

    sibling = _stress_video_matches_negative_title(
        row, source_title, video_name, negative_titles
    )
    if sibling is not None:
        return "video_known_sibling_title"

    extension_reason = _stress_video_title_extension_reason(row, video_name)
    if extension_reason is not None:
        return extension_reason

    # After sibling/extension vetoes, ordinary translated aliases may use the
    # broader production matcher.
    if _stress_release_title_is_plausible(
        row, video_name, negative_titles=negative_titles
    ):
        return None

    # A terse episode-title-only basename is safe when its season is explicitly
    # confirmed by the already-validated torrent title.  This broader fallback is
    # intentionally after sibling/extension vetoes.
    if _stress_source_confirms_video_season(source_title, video_name):
        return None

    # No positive title evidence in the basename is not itself a contradiction.
    # Episode-title-only files are common inside otherwise well-identified packs.
    return None


def _stress_release_rejection_reason(
    row: dict[str, Any],
    release: Any,
    *,
    resolution: str,
    minimum_seeders: int,
    negative_titles: tuple[str, ...] = (),
) -> str | None:
    title = str(getattr(release, "title", "") or "")
    seeders = max(0, int(getattr(release, "seeders", 0) or 0))
    if seeders < max(1, int(minimum_seeders)):
        return "too_few_seeders"
    height = _benchmark_resolution_height(title)
    maximum = _benchmark_resolution_limit(resolution)
    if height is None:
        return "resolution_unknown"
    if maximum is not None and height > maximum:
        return "resolution_above_limit"
    if _source_special_mismatch(row, title):
        return "special_source_mismatch"
    mismatch = _stress_source_identity_mismatch_reason(
        row, title, negative_titles
    )
    if mismatch:
        return mismatch
    if not _stress_release_title_is_plausible(
        row, title, negative_titles=negative_titles
    ):
        return "title_identity_mismatch"
    if _benchmark_batch_release(release):
        if not release_is_safe_batch_candidate(
            _benchmark_anime_from_row(row),
            release,
            negative_titles=negative_titles,
        ):
            return "unsafe_batch_candidate"
    else:
        found_episode = release_episode(title)
        if found_episode is None:
            return "episode_missing"
        if int(found_episode) != int(row["episode"]):
            return "episode_mismatch"
    return None


def _stress_resolution_preference(title: str, resolution: str) -> int:
    height = _benchmark_resolution_height(title)
    maximum = _benchmark_resolution_limit(resolution)
    if height is None:
        return -10000
    if maximum is None:
        return height
    return -abs(maximum - height)


def _stress_source_sort_key(release: Any, resolution: str) -> tuple[float, ...]:
    title = str(getattr(release, "title", "") or "")
    seeders = max(0, int(getattr(release, "seeders", 0) or 0))
    score = float(getattr(release, "score", 0.0) or 0.0)
    size = max(0, int(getattr(release, "size_bytes", 0) or 0))
    return (
        float(min(seeders, 1000)),
        float(_stress_resolution_preference(title, resolution)),
        0.0 if _benchmark_batch_release(release) else 1.0,
        score,
        -float(size),
    )


def _score_stress_release(
    release: Any,
    anime: LibraryAnime,
    config: Any,
    *,
    episode: int,
    resolution: str,
    minimum_seeders: int,
) -> Any:
    return score_release(
        release,
        anime,
        episode=episode,
        batch=_benchmark_batch_release(release),
        trusted_groups=config.nyaa.trusted_groups,
        preferred_groups=config.nyaa.preferred_groups,
        blocked_groups=config.nyaa.blocked_groups,
        preferred_resolution=resolution,
        min_seeders=max(1, int(minimum_seeders)),
        target_episode_min_bytes=max(
            40 * 1024 * 1024, config.nyaa.episode_min_size_mb * 1024 * 1024
        ),
        target_episode_max_bytes=config.nyaa.episode_max_size_mb * 1024 * 1024,
        preferred_video_codecs=config.nyaa.preferred_video_codecs,
        preferred_sources=config.nyaa.preferred_sources,
        require_japanese_audio=config.nyaa.require_japanese_audio,
        avoid_upscaled=config.nyaa.avoid_upscaled,
    )


def _nyaa_ranked_for_stress(
    row: dict[str, Any],
    config: Any,
    *,
    resolution: str,
    min_source_seeders: int = 1,
    negative_titles: tuple[str, ...] = (),
) -> tuple[list[Any], dict[str, object]]:
    """Normal Nyaa acquisition for stress mode, with forensic reject logging."""
    anime = _benchmark_anime_from_row(row)
    client = NyaaClient(
        config.nyaa.base_url,
        proxy_mode=config.nyaa.proxy_mode,
        proxy_url=config.nyaa.proxy_url,
        pre_search_command=config.nyaa.pre_search_command,
        category=config.nyaa.category,
    )
    minimum = max(1, int(min_source_seeders), int(config.nyaa.min_seeders))
    all_queries: list[str] = []
    raw: list[Any] = []
    errors: list[str] = []
    candidates_by_key: dict[str, Any] = {}
    reject_counts: dict[str, int] = {}
    rejected_examples: list[dict[str, object]] = []

    def run_query(query: str) -> None:
        all_queries.append(query)
        try:
            # Nyaa's default RSS ordering is freshness-oriented. Popular older
            # anime benchmark much better when each query asks the server for
            # live swarms first.
            raw.extend(
                client.search(
                    query,
                    sort_by="seeders",
                    order="desc",
                )
            )
        except Exception as exc:
            errors.append(f"{query}: {type(exc).__name__}: {exc}")

    def rank_current() -> list[Any]:
        candidates_by_key.clear()
        reject_counts.clear()
        rejected_examples.clear()
        for release in raw:
            key = str(
                getattr(release, "info_hash", "")
                or getattr(release, "torrent_url", "")
                or getattr(release, "link", "")
                or getattr(release, "title", "")
            )
            if not key or key in candidates_by_key:
                continue
            reason = _stress_release_rejection_reason(
                row,
                release,
                resolution=resolution,
                minimum_seeders=minimum,
                negative_titles=negative_titles,
            )
            if reason:
                reject_counts[reason] = reject_counts.get(reason, 0) + 1
                if len(rejected_examples) < 12:
                    rejected_examples.append(
                        {
                            "reason": reason,
                            "title": str(getattr(release, "title", "") or ""),
                            "seeders": max(0, int(getattr(release, "seeders", 0) or 0)),
                        }
                    )
                candidates_by_key[key] = None
                continue
            candidates_by_key[key] = _score_stress_release(
                release,
                anime,
                config,
                episode=int(row["episode"]),
                resolution=resolution,
                minimum_seeders=minimum,
            )
        accepted = [value for value in candidates_by_key.values() if value is not None]
        accepted.sort(
            key=lambda release: _stress_source_sort_key(release, resolution),
            reverse=True,
        )
        return accepted

    try:
        # Fast path: exact episode forms first. Stop querying as soon as one
        # exact query produces a valid single release: Nyaa already returns
        # that query ordered by seeders, so extra aliases mostly add load and
        # increase the chance of 504/circuit-breaker pauses.
        accepted: list[Any] = []
        for query in _stress_discovery_queries(row, batch=False):
            run_query(query)
            accepted = [
                release
                for release in rank_current()
                if not _benchmark_batch_release(release)
            ]
            if accepted:
                break

        # Batch/broad fallback is also incremental. A valid pack from the first
        # useful alias is enough; avoid spraying every synonym at Nyaa.
        if not accepted:
            for query in _stress_discovery_queries(row, batch=True):
                run_query(query)
                accepted = rank_current()
                if accepted:
                    break

        diagnostics: dict[str, object] = {
            "queries": list(dict.fromkeys(all_queries)),
            "raw_candidates": len(raw),
            "reject_counts": dict(sorted(reject_counts.items())),
            "rejected_examples": rejected_examples,
        }
        if errors:
            diagnostics["query_errors"] = errors[:8]
        return accepted, diagnostics
    finally:
        client.close()


def _stress_existing_download_sizes(corpus: Path) -> dict[tuple[int, int], int]:
    """Recover successful bytes from prior stress runs so resume keeps the 100 GiB budget."""
    path = Path(corpus) / "stress-events.jsonl"
    sizes: dict[tuple[int, int], int] = {}
    if not path.is_file():
        return sizes
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return sizes
    for line in lines:
        try:
            payload = json.loads(line)
            if payload.get("event") != "download_ok":
                continue
            key = (int(payload["media_id"]), int(payload["episode"]))
            size = max(0, int(payload.get("bytes") or 0))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        sizes[key] = max(sizes.get(key, 0), size)
    return sizes


def _nyaa_ranked_for_plan(
    row: dict[str, Any],
    config,
    *,
    resolution: str,
    min_source_seeders: int = 1,
    negative_titles: tuple[str, ...] = (),
) -> list[Any]:
    anime = _benchmark_anime_from_row(row)
    client = NyaaClient(
        config.nyaa.base_url,
        proxy_mode=config.nyaa.proxy_mode,
        proxy_url=config.nyaa.proxy_url,
        pre_search_command=config.nyaa.pre_search_command,
        category=config.nyaa.category,
    )

    preferred_search_resolution = (
        "480p"
        if (_benchmark_resolution_limit(resolution) or 0) >= 480
        else resolution
    )
    minimum = max(1, int(min_source_seeders), int(config.nyaa.min_seeders))

    def valid(release: Any) -> bool:
        is_batch = _benchmark_batch_release(release)
        return bool(
            max(0, int(getattr(release, "seeders", 0) or 0)) >= minimum
            and not _source_special_mismatch(row, str(getattr(release, "title", "")))
            and _source_identity_mismatch_reason(row, str(getattr(release, "title", ""))) is None
            and (
                release_is_safe_batch_candidate(
                    anime, release, negative_titles=negative_titles
                )
                if is_batch
                else (
                    release_episode(str(getattr(release, "title", ""))) == int(row["episode"])
                    and release_title_is_plausible(
                        anime,
                        str(getattr(release, "title", "")),
                        negative_titles=negative_titles,
                    )
                )
            )
        )

    def deduplicate_rank(releases: Iterable[Any]) -> list[Any]:
        combined: dict[str, Any] = {}
        for release in releases:
            key = str(
                getattr(release, "info_hash", "")
                or getattr(release, "torrent_url", "")
                or getattr(release, "link", "")
            )
            if not key:
                continue
            current = combined.get(key)
            if current is None or _benchmark_source_sort_key(release) > _benchmark_source_sort_key(current):
                combined[key] = release
        filtered = [
            release
            for release in _filter_benchmark_resolution(list(combined.values()), resolution)
            if valid(release)
        ]
        filtered.sort(key=_benchmark_source_sort_key, reverse=True)
        return filtered

    def search(batch: bool, budget: float) -> list[Any]:
        return search_ranked(
            client,
            anime,
            episode=int(row["episode"]),
            batch=batch,
            trusted_groups=config.nyaa.trusted_groups,
            preferred_groups=config.nyaa.preferred_groups,
            blocked_groups=config.nyaa.blocked_groups,
            preferred_resolution=preferred_search_resolution,
            min_seeders=minimum,
            target_episode_min_bytes=max(40 * 1024 * 1024, config.nyaa.episode_min_size_mb * 1024 * 1024),
            target_episode_max_bytes=config.nyaa.episode_max_size_mb * 1024 * 1024,
            preferred_video_codecs=config.nyaa.preferred_video_codecs,
            preferred_sources=config.nyaa.preferred_sources,
            require_japanese_audio=config.nyaa.require_japanese_audio,
            avoid_upscaled=config.nyaa.avoid_upscaled,
            negative_titles=negative_titles,
            max_queries=5,
            query_budget_seconds=budget,
        )

    try:
        # Source-first discovery: ask for explicit JP/JPN/MultiSub/Netflix/Erai
        # signals before spending query budget on generic anime searches. If a
        # live explicit-JP candidate exists, use that high-confidence tier
        # directly and avoid downloading lower-confidence generic releases.
        extra: list[Any] = []
        for query in _benchmark_source_queries(row):
            try:
                for release in client.search(query):
                    extra.append(
                        _score_benchmark_extra_release(
                            release,
                            anime,
                            config,
                            episode=int(row["episode"]),
                            resolution=resolution,
                        )
                    )
            except Exception:
                continue
        targeted = deduplicate_rank(extra)
        high_confidence = [
            release
            for release in targeted
            if _benchmark_has_explicit_jp_signal(str(getattr(release, "title", "")))
        ]
        if high_confidence:
            return high_confidence

        singles = search(False, 55.0)
        batches = search(True, 55.0)
        return deduplicate_rank([*targeted, *singles, *batches])
    finally:
        client.close()


def _usable_benchmark_releases(
    releases: Iterable[Any],
    *,
    row: dict[str, Any],
    corpus: Path,
    max_bytes: int,
    diagnostic_releases: set[tuple[int, int, str]],
    diagnostic_media_releases: set[tuple[int, str]],
    diagnostic_media_families: set[tuple[int, str]],
    attempted_release_keys: set[str] | None = None,
) -> list[Any]:
    """Apply durable benchmark source guards to one discovery phase."""
    usable: list[Any] = []
    attempted = attempted_release_keys if attempted_release_keys is not None else set()
    for release in releases:
        release_key = _release_diagnostic_key(row, release)[2]
        if release_key and release_key in attempted:
            continue
        is_batch = _benchmark_batch_release(release)
        if not (is_batch or 0 < release.size_bytes <= max_bytes):
            continue
        if _release_diagnostic_key(row, release) in diagnostic_releases:
            continue
        if is_batch and _release_media_diagnostic_key(row, release) in diagnostic_media_releases:
            continue
        family_key = _benchmark_release_family_key(release)
        if (
            is_batch
            and family_key
            and (int(row["media_id"]), family_key) in diagnostic_media_families
        ):
            print(f"  skip known no-JP release family: {release.title}")
            continue
        if (
            _benchmark_declares_no_japanese_subtitles(str(release.title))
            and not _NETFLIX_SOURCE_RE.search(str(release.title))
        ):
            languages = ",".join(
                _benchmark_declared_subtitle_languages(str(release.title))
            )
            print(
                f"  skip declared subtitle languages without JPN [{languages}]: "
                f"{release.title}"
            )
            family = _record_source_rejection(
                corpus,
                row,
                release,
                reason="declared_subtitle_languages_without_japanese",
            )
            if is_batch and family:
                diagnostic_media_families.add((int(row["media_id"]), family))
            continue
        usable.append(release)
    return usable



def _append_stress_event(corpus: Path, payload: dict[str, object]) -> None:
    path = Path(corpus) / "stress-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": "pudge-subtitle-random-stress-event-v1",
        "observed_at": time.time(),
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _random_stress_rows(
    endpoint: str,
    *,
    anime_limit: int,
    sort: str,
    seed: int,
    cache_dir: Path | None,
    max_retries: int,
    retry_base_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    """Choose exactly one random episode from each popular anime, then shuffle titles."""
    plan = fetch_top_anime_plan(
        endpoint,
        limit=max(1, int(anime_limit)),
        sort=sort,
        sample_episodes=1,
        seed=int(seed),
        cache_dir=cache_dir,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    rows = [dict(row) for row in plan.get("episodes") or [] if isinstance(row, dict)]
    rows = [
        row
        for row in rows
        if not str(row.get("format") or "").strip()
        or str(row.get("format") or "").upper() in _STRESS_EPISODIC_FORMATS
    ]
    random.Random(int(seed)).shuffle(rows)
    return rows, plan


def _ensure_stress_library_ignore_marker(corpus: Path) -> Path:
    """Make ordinary Library discovery ignore benchmark-owned torrent files."""
    root = Path(corpus).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _STRESS_LIBRARY_IGNORE_MARKER
    payload = (
        "Pudge stress benchmark corpus.\n"
        "Ordinary Library scanning and completed-download reconciliation must ignore "
        "this tree; subtitle_benchmark_cli registers only authoritative target episodes.\n"
    )
    try:
        current = marker.read_text(encoding="utf-8") if marker.is_file() else ""
    except OSError:
        current = ""
    if current != payload:
        marker.write_text(payload, encoding="utf-8")
    return marker


def _stress_library_anime(
    row: dict[str, Any], existing: LibraryAnime | None = None
) -> LibraryAnime:
    """Canonical AniList metadata for one stress target without touching user progress."""
    planned = _benchmark_anime_from_row(row)
    if existing is None:
        return planned
    return LibraryAnime(
        media_id=planned.media_id,
        title=planned.title or existing.title,
        titles=planned.titles or existing.titles,
        synonyms=planned.synonyms or existing.synonyms,
        cover_url=existing.cover_url,
        site_url=planned.site_url or existing.site_url,
        status=existing.status,
        progress=existing.progress,
        episodes=planned.episodes if planned.episodes is not None else existing.episodes,
        format=planned.format or existing.format,
        season_year=(
            planned.season_year if planned.season_year is not None else existing.season_year
        ),
        start_date=existing.start_date,
        studio=existing.studio,
        media_status=planned.media_status or existing.media_status,
        end_date=existing.end_date,
        mean_score=(
            planned.mean_score if planned.mean_score is not None else existing.mean_score
        ),
        user_score=existing.user_score,
        duration=planned.duration if planned.duration is not None else existing.duration,
        next_airing_episode=existing.next_airing_episode,
        next_airing_at=existing.next_airing_at,
        relations=planned.relations or existing.relations,
    )


def _register_stress_library_video(
    config: Any,
    row: dict[str, Any],
    video: Path,
    *,
    torrent_hash: str = "",
) -> dict[str, object]:
    """Register a downloaded stress target as local media under its known AniList ID.

    The stress plan is stronger identity evidence than a release filename.  Register
    before subtitle preparation so a later pipeline failure cannot leave a downloaded
    target invisible, then call this again after preparation to restore canonical
    AniList metadata if a production scan temporarily used a torrent/release title.
    """
    media_id = int(row["media_id"])
    media_episode = int(row["episode"])
    resolved = Path(video).expanduser().resolve()
    db = Database(config.library.database_path)
    canonical = _stress_library_anime(row, db.get_anime(media_id))
    db.upsert_anime(canonical)

    existing = db.episode_by_path(resolved)
    parsed = parse_anime_filename(resolved)
    release_episode = (
        existing.release_episode
        if existing is not None and existing.release_episode is not None
        else parsed.episode
        if parsed.episode is not None
        else media_episode
    )
    item = LibraryEpisode(
        media_id=media_id,
        title=canonical.title,
        episode=media_episode,
        media_episode=media_episode,
        release_episode=release_episode,
        video_path=resolved,
        subtitle_path=existing.subtitle_path if existing is not None else None,
        embedded_subtitle_id=(
            existing.embedded_subtitle_id if existing is not None else None
        ),
        subtitle_origin=existing.subtitle_origin if existing is not None else "",
        state=existing.state if existing is not None else "local",
        torrent_hash=(
            existing.torrent_hash
            if existing is not None and existing.torrent_hash
            else str(torrent_hash or "")
        ),
        watched_at=existing.watched_at if existing is not None else None,
        delete_after=existing.delete_after if existing is not None else None,
    )
    downloaded_at = None
    if existing is None:
        try:
            downloaded_at = float(resolved.stat().st_mtime)
        except OSError:
            downloaded_at = time.time()
    db.upsert_episode(item, downloaded_at=downloaded_at)
    return {
        "media_id": media_id,
        "episode": media_episode,
        "path": str(resolved),
        "title": canonical.title,
        "state": item.state,
    }


def _stress_download_event_rows(corpus: Path) -> dict[tuple[int, int], dict[str, Any]]:
    path = Path(corpus) / "stress-events.jsonl"
    latest: dict[tuple[int, int], dict[str, Any]] = {}
    if not path.is_file():
        return latest
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            payload = json.loads(line)
            if payload.get("event") != "download_ok":
                continue
            key = (int(payload["media_id"]), int(payload["episode"]))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        latest[key] = dict(payload)
    return latest


def _stress_download_video_index(download_root: Path) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    root = Path(download_root)
    if not root.is_dir():
        return by_name
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        by_name.setdefault(path.name, []).append(path.resolve())
    return by_name


def _stress_current_invalid_targets(
    corpus: Path,
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Audit durable download events with the current identity contract.

    This pure helper is shared by repair and replay so replay cannot silently use
    an older ``stress-library-repair.json`` after the validator changes.
    """
    corpus = Path(corpus).expanduser()
    plan_rows = [dict(row) for row in rows]
    plan_by_key = {
        (int(row["media_id"]), int(row["episode"])): dict(row)
        for row in plan_rows
        if row.get("media_id") is not None and row.get("episode") is not None
    }
    negative_titles_by_media = _plan_negative_titles_by_media(plan_rows)
    events = _stress_download_event_rows(corpus)
    invalid: list[dict[str, Any]] = []
    for key, event in events.items():
        row = plan_by_key.get(key)
        if row is None:
            event_title = str(event.get("title") or "").strip()
            if not event_title:
                continue
            row = {
                "media_id": key[0],
                "episode": key[1],
                "title": event_title,
                "titles": [event_title],
                "synonyms": [],
            }
        video_name = str(event.get("video_name") or "").strip()
        if not video_name:
            continue
        source = event.get("source_release") if isinstance(event.get("source_release"), dict) else {}
        source_title = str(source.get("title") or video_name)
        reason = _stress_selected_video_rejection_reason(
            row,
            source_title=source_title,
            video_name=video_name,
            negative_titles=negative_titles_by_media.get(key[0], ()),
        )
        if reason:
            invalid.append(
                {
                    "media_id": key[0],
                    "episode": key[1],
                    "video_name": video_name,
                    "source_title": source_title,
                    "reason": reason,
                }
            )
    return invalid


def _repair_stress_library_registrations(
    corpus: Path,
    rows: Iterable[dict[str, Any]],
    config: Any,
) -> dict[str, Any]:
    """Repair historical stress targets using durable plan/event identity evidence."""
    corpus = Path(corpus).expanduser()
    plan_rows = [dict(row) for row in rows]
    plan_by_key = {
        (int(row["media_id"]), int(row["episode"])): dict(row)
        for row in plan_rows
        if row.get("media_id") is not None and row.get("episode") is not None
    }
    events = _stress_download_event_rows(corpus)
    files_by_name = _stress_download_video_index(corpus / "downloads")
    invalid_audit = _stress_current_invalid_targets(corpus, plan_rows)
    invalid_by_key = {
        (int(item["media_id"]), int(item["episode"])): item
        for item in invalid_audit
    }
    result = {
        "registered": 0,
        "already_or_repaired": 0,
        "identity_rejected": 0,
        "missing_file": 0,
        "ambiguous_file": 0,
        "errors": 0,
        "stale_rows_removed": 0,
        "isolated_sibling_rows_removed": 0,
        "isolated_subtitle_jobs_removed": 0,
        "identity_rejected_reasons": {},
        "invalid_targets": [],
    }
    if not events:
        return result
    db = Database(config.library.database_path)
    download_root = (corpus / "downloads").resolve()
    valid_target_paths: set[Path] = set()
    subtitle_jobs = getattr(db, "subtitle_jobs", None)
    job_paths = (
        {str(row["video_path"]) for row in subtitle_jobs()}
        if callable(subtitle_jobs)
        else set()
    )

    def remove_stress_row(path: Path, *, sibling: bool) -> bool:
        resolved = Path(path).expanduser().resolve()
        existing = db.episode_by_path(resolved)
        if existing is None:
            return False
        if str(resolved) in job_paths:
            db.delete_subtitle_job(resolved)
            job_paths.discard(str(resolved))
            result["isolated_subtitle_jobs_removed"] += 1
        db.delete_episode_record(resolved)
        result["stale_rows_removed"] += 1
        if sibling:
            result["isolated_sibling_rows_removed"] += 1
        return True

    for key, event in events.items():
        row = plan_by_key.get(key)
        if row is None:
            # v101 generated stress targets before v102 started filtering out
            # movies/specials. Keep enough durable event identity to audit those
            # historical downloads instead of silently skipping them forever.
            event_title = str(event.get("title") or "").strip()
            if not event_title:
                continue
            row = {
                "media_id": key[0],
                "episode": key[1],
                "title": event_title,
                "titles": [event_title],
                "synonyms": [],
            }
        video_name = str(event.get("video_name") or "").strip()
        candidates = list(files_by_name.get(video_name, ())) if video_name else []
        if not candidates:
            result["missing_file"] += 1
            continue
        if len(candidates) > 1:
            exact_episode = []
            for candidate in candidates:
                try:
                    identity = parse_anime_filename(candidate)
                except Exception:
                    continue
                if identity.episode == int(row["episode"]):
                    exact_episode.append(candidate)
            if len(exact_episode) == 1:
                candidates = exact_episode
            else:
                result["ambiguous_file"] += 1
                continue
        video = candidates[0]
        source = event.get("source_release") if isinstance(event.get("source_release"), dict) else {}
        audited_invalid = invalid_by_key.get(key)
        rejection_reason = str(audited_invalid.get("reason")) if audited_invalid else None
        if rejection_reason:
            result["identity_rejected"] += 1
            reason_counts = result["identity_rejected_reasons"]
            assert isinstance(reason_counts, dict)
            reason_counts[rejection_reason] = int(reason_counts.get(rejection_reason, 0)) + 1
            invalid_targets = result["invalid_targets"]
            assert isinstance(invalid_targets, list)
            invalid_targets.append(dict(audited_invalid))
            remove_stress_row(video, sibling=False)
            continue
        valid_target_paths.add(video.resolve())
        existed = db.episode_by_path(video) is not None
        try:
            _register_stress_library_video(
                config,
                row,
                video,
                torrent_hash=str(source.get("info_hash") or ""),
            )
        except Exception:
            result["errors"] += 1
            continue
        result["already_or_repaired" if existed else "registered"] += 1

    # Ordinary Library scans used to import sparse/preallocated siblings from
    # benchmark batch torrents.  Keep only paths backed by a valid download_ok
    # target; delete DB/job rows only, never source video files.
    episodes = getattr(db, "episodes", None)
    stale_rows = list(episodes()) if callable(episodes) else []
    for stale in stale_rows:
        try:
            resolved = stale.video_path.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == download_root or download_root not in resolved.parents:
            continue
        if resolved in valid_target_paths:
            continue
        remove_stress_row(resolved, sibling=True)
    return result


def repair_stress_library(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    corpus = args.corpus.expanduser()
    marker = _ensure_stress_library_ignore_marker(corpus)
    plan_path = corpus / "stress-plan.json"
    if not plan_path.is_file():
        raise RuntimeError(f"stress plan not found: {plan_path}")
    rows = _read_plan(plan_path)
    result = _repair_stress_library_registrations(corpus, rows, config)
    payload = {
        "schema": "pudge-subtitle-stress-library-repair-v1",
        "observed_at": time.time(),
        "library_isolation_marker": str(marker),
        **result,
    }
    write_json(corpus / "stress-library-repair.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result["errors"] == 0 else 1


def _stress_invalid_target_keys(corpus: Path) -> set[tuple[int, int]]:
    path = Path(corpus) / "stress-library-repair.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()
    result: set[tuple[int, int]] = set()
    for row in payload.get("invalid_targets") or []:
        if not isinstance(row, dict):
            continue
        try:
            result.add((int(row["media_id"]), int(row["episode"])))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _stress_stored_report_paths(corpus: Path) -> list[Path]:
    root = Path(corpus)
    paths = [
        *sorted((root / "cases").glob("*/case.json")),
        *sorted((root / "diagnostics").glob("*/diagnostic.json")),
    ]
    def key(path: Path) -> tuple[int, int, str]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return (int(payload.get("media_id") or 0), int(payload.get("episode") or 0), str(path))
        except Exception:
            return (0, 0, str(path))
    return sorted(paths, key=key)


def replay_stress(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    corpus = args.corpus.expanduser()
    _ensure_stress_library_ignore_marker(corpus)
    persisted_invalid_keys = _stress_invalid_target_keys(corpus)
    plan_path = corpus / "stress-plan.json"
    current_invalid: list[dict[str, Any]] = []
    if plan_path.is_file():
        try:
            current_invalid = _stress_current_invalid_targets(corpus, _read_plan(plan_path))
        except Exception:
            current_invalid = []
    current_invalid_keys = {
        (int(item["media_id"]), int(item["episode"]))
        for item in current_invalid
    }
    invalid_keys = persisted_invalid_keys | current_invalid_keys
    if current_invalid:
        write_json(
            corpus / "stress-identity-audit-v107.json",
            {
                "schema": "pudge-subtitle-stress-identity-audit-v107",
                "observed_at": time.time(),
                "invalid_targets": current_invalid,
            },
        )
    paths = _stress_stored_report_paths(corpus)
    start = max(0, int(args.start_index))
    limit = max(0, int(args.limit))

    eligible: list[tuple[Path, dict[str, Any]]] = []
    skipped_invalid = 0
    unreadable = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = (int(payload["media_id"]), int(payload["episode"]))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            unreadable += 1
            continue
        if key in invalid_keys:
            skipped_invalid += 1
            continue
        eligible.append((path, payload))

    stored_keys: set[tuple[int, int]] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored_keys.add((int(payload["media_id"]), int(payload["episode"])))
        except Exception:
            continue
    invalid_without_report = len(invalid_keys - stored_keys)

    selected = eligible[start:]
    if limit:
        selected = selected[:limit]

    replayed = 0
    no_candidates = 0
    errors = 0
    results: list[dict[str, Any]] = []
    print(
        f"Stress replay: stored={len(paths)} eligible={len(eligible)} "
        f"invalid_known={len(invalid_keys)} invalid_skipped={skipped_invalid} "
        f"selected={len(selected)}",
        flush=True,
    )
    for index, (path, payload) in enumerate(selected, start=start):
        media_id = int(payload["media_id"])
        episode = int(payload["episode"])
        title = str(payload.get("title") or media_id)
        print(f"REPLAY {index}: {title} E{episode}", flush=True)
        try:
            row = replay_stored_benchmark_case(path, config=config, force=True)
            status = str(row.get("status") or "replayed")
            if status == "no_candidates":
                no_candidates += 1
            elif status == "error":
                errors += 1
            else:
                replayed += 1
            result_row = {
                "media_id": media_id,
                "episode": episode,
                "title": title,
                **row,
            }
            results.append(result_row)
            _append_stress_event(
                corpus,
                {
                    "event": "replay_ok",
                    "media_id": media_id,
                    "episode": episode,
                    "title": title,
                    "status": status,
                    "report_path": str(path),
                    "accepted": row.get("accepted"),
                },
            )
            print(f"  {status} accepted={row.get('accepted')}", flush=True)
        except Exception as exc:
            errors += 1
            result_row = {
                "media_id": media_id,
                "episode": episode,
                "title": title,
                "report_path": str(path),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            results.append(result_row)
            _append_stress_event(
                corpus,
                {
                    "event": "replay_error",
                    "media_id": media_id,
                    "episode": episode,
                    "title": title,
                    "report_path": str(path),
                    "error": result_row["error"],
                },
            )
            print(f"  ERROR {result_row['error']}", flush=True)

    try:
        _write_corpus_summary(corpus)
    except Exception:
        pass
    summary = {
        "schema": "pudge-subtitle-stress-replay-v107",
        "finished_at": time.time(),
        "stored_reports": len(paths),
        "eligible_reports": len(eligible),
        "invalid_known": len(invalid_keys),
        "invalid_skipped": skipped_invalid,
        "invalid_without_report": invalid_without_report,
        "unreadable": unreadable,
        "selected_reports": len(selected),
        "replayed": replayed,
        "no_candidates": no_candidates,
        "errors": errors,
        "results": results,
    }
    write_json(corpus / "stress-replay-v107.json", summary)
    # Compatibility alias for v106 tooling/tests; payload schema identifies v107.
    write_json(corpus / "stress-replay-v106.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if errors == 0 else 1


def acquire_random_stress(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.jimaku.api_key:
        raise RuntimeError("Jimaku API key is required for stress-random")

    corpus = args.corpus.expanduser()
    corpus.mkdir(parents=True, exist_ok=True)
    _ensure_stress_library_ignore_marker(corpus)
    download_root = corpus / "downloads"
    download_root.mkdir(parents=True, exist_ok=True)
    cache_dir = args.cache_dir or (corpus / ".anilist-cache")
    rows, plan = _random_stress_rows(
        config.anilist.endpoint,
        anime_limit=int(args.anime_limit),
        sort=str(args.sort),
        seed=int(args.seed),
        cache_dir=cache_dir,
        max_retries=int(args.anilist_retries),
        retry_base_seconds=float(args.retry_base_seconds),
    )
    write_json(corpus / "stress-plan.json", {**plan, "episodes": rows, "stress_random": True})

    repaired = _repair_stress_library_registrations(corpus, rows, config)
    repair_counts = {
        key: value for key, value in repaired.items() if key != "invalid_targets"
    }
    invalid_targets = repaired.get("invalid_targets")
    invalid_target_rows = invalid_targets if isinstance(invalid_targets, list) else []
    if any(repair_counts.values()) or invalid_target_rows:
        print(
            "Stress library repair: "
            + " ".join(f"{key}={value}" for key, value in repair_counts.items())
            + f" invalid_targets={len(invalid_target_rows)}",
            flush=True,
        )

    existing_keys = _completed_episode_keys(corpus)
    for diagnostic in collect_diagnostic_reports(corpus):
        try:
            existing_keys.add((int(diagnostic.get("media_id")), int(diagnostic.get("episode"))))
        except (TypeError, ValueError):
            continue
    for invalid in invalid_target_rows:
        if not isinstance(invalid, dict):
            continue
        try:
            existing_keys.discard((int(invalid["media_id"]), int(invalid["episode"])))
        except (KeyError, TypeError, ValueError):
            continue
    negative_titles_by_media = _plan_negative_titles_by_media(rows)
    target_bytes = int(max(0.1, float(args.target_gb)) * 1024**3)
    max_episode_bytes = int(max(0.1, float(args.max_download_gb)) * 1024**3)
    backend_name, downloader = _download_client(config, args.backend)
    existing_download_sizes = (
        _stress_existing_download_sizes(corpus) if bool(args.keep_videos) else {}
    )
    downloaded_bytes = sum(existing_download_sizes.values())
    completed_cases = 0
    failed_targets = 0
    attempted_targets = 0
    print(
        f"Random stress: anime={len(rows)} target={_human_bytes(target_bytes)} "
        f"resolution<={args.resolution} backend={backend_name} seed={args.seed} "
        f"existing={_human_bytes(downloaded_bytes)}",
        flush=True,
    )

    try:
        for index, row in enumerate(rows):
            if downloaded_bytes >= target_bytes:
                break
            key = (int(row["media_id"]), int(row["episode"]))
            if key in existing_keys:
                continue
            attempted_targets += 1
            print(
                f"STRESS {index}: #{row.get('rank')} {row['title']} E{row['episode']} "
                f"total={_human_bytes(downloaded_bytes)}/{_human_bytes(target_bytes)}",
                flush=True,
            )
            try:
                releases, discovery = _nyaa_ranked_for_stress(
                    row,
                    config,
                    resolution=str(args.resolution),
                    min_source_seeders=int(args.min_source_seeders),
                    negative_titles=negative_titles_by_media.get(key[0], ()),
                )
            except NyaaError as exc:
                _append_stress_event(
                    corpus,
                    {
                        "event": "discovery_error",
                        "media_id": key[0],
                        "episode": key[1],
                        "title": str(row["title"]),
                        "error": _concise_nyaa_error(exc),
                    },
                )
                print("  Nyaa: " + _concise_nyaa_error(exc), flush=True)
                failed_targets += 1
                continue

            if not releases:
                _append_stress_event(
                    corpus,
                    {
                        "event": "no_release",
                        "media_id": key[0],
                        "episode": key[1],
                        "title": str(row["title"]),
                        "discovery": discovery,
                    },
                )
                reject_counts = discovery.get("reject_counts") or {}
                detail = ", ".join(
                    f"{name}={count}" for name, count in reject_counts.items()
                )
                print(
                    "  no exact release candidate" + (f" ({detail})" if detail else ""),
                    flush=True,
                )
                failed_targets += 1
                continue

            target_done = False
            for release in releases[: max(1, int(args.max_release_attempts))]:
                torrent_hash = ""
                release_meta = {
                    "title": str(release.title),
                    "info_hash": str(getattr(release, "info_hash", "") or ""),
                    "seeders": int(getattr(release, "seeders", 0) or 0),
                    "size_bytes": int(getattr(release, "size_bytes", 0) or 0),
                    "is_batch": bool(_benchmark_batch_release(release)),
                    "resolution": f"{_benchmark_resolution_height(str(release.title))}p"
                    if _benchmark_resolution_height(str(release.title))
                    else "unknown",
                }
                print(
                    f"  try seeds={release_meta['seeders']:3d} "
                    f"{'pack' if release_meta['is_batch'] else 'single':6s} {release.title}",
                    flush=True,
                )
                try:
                    torrent_hash, videos = _download_release_videos(
                        downloader,
                        release,
                        episodes=[key[1]],
                        root=download_root,
                        metadata_timeout_seconds=float(args.metadata_timeout_seconds),
                        download_timeout_seconds=float(args.download_timeout_minutes) * 60.0,
                        max_target_bytes=max_episode_bytes,
                        stall_timeout_seconds=float(args.stall_timeout_seconds),
                        progress_interval_seconds=float(args.progress_interval_seconds),
                        identity_row=row,
                        probe_first=False,
                    )
                    video = videos.get(key[1])
                    if video is None or not video.is_file():
                        raise SubtitleBenchmarkError("exact requested episode file was not produced")
                    size_bytes = int(video.stat().st_size)
                    previous_size = existing_download_sizes.get(key, 0)
                    downloaded_bytes += max(0, size_bytes - previous_size)
                    existing_download_sizes[key] = max(previous_size, size_bytes)
                    negative_titles = negative_titles_by_media.get(key[0], ())
                    selected_rejection = _stress_selected_video_rejection_reason(
                        row,
                        source_title=str(release.title),
                        video_name=video.name,
                        negative_titles=negative_titles,
                    )
                    if selected_rejection:
                        raise SubtitleBenchmarkError(
                            "downloaded file identity mismatch "
                            f"({selected_rejection}): {video.name}"
                        )
                    _append_stress_event(
                        corpus,
                        {
                            "event": "download_ok",
                            "media_id": key[0],
                            "episode": key[1],
                            "title": str(row["title"]),
                            "rank": row.get("rank"),
                            "video_name": video.name,
                            "bytes": size_bytes,
                            "cumulative_bytes": downloaded_bytes,
                            "source_release": release_meta,
                        },
                    )
                    try:
                        _register_stress_library_video(
                            config,
                            row,
                            video,
                            torrent_hash=torrent_hash or str(release_meta.get("info_hash") or ""),
                        )
                    except Exception as exc:
                        _append_stress_event(
                            corpus,
                            {
                                "event": "library_registration_error",
                                "stage": "post_download",
                                "media_id": key[0],
                                "episode": key[1],
                                "title": str(row["title"]),
                                "video_name": video.name,
                                "error": str(exc),
                            },
                        )
                        print(f"  library registration warning: {exc}", flush=True)

                    try:
                        case_dir = create_case_from_video(
                            video=video,
                            corpus_dir=corpus,
                            config=config,
                            media_id=key[0],
                            title=str(row["title"]),
                            episode=key[1],
                            fetch_jimaku=True,
                            run_pudge=True,
                        )
                        case_kind = "gold"
                    except SubtitleBenchmarkError as exc:
                        if "no embedded Japanese text subtitle" not in str(exc):
                            raise
                        case_dir = create_diagnostic_case_from_video(
                            video=video,
                            corpus_dir=corpus,
                            config=config,
                            media_id=key[0],
                            title=str(row["title"]),
                            episode=key[1],
                            reason="random_stress_no_embedded_japanese_oracle",
                            source_release=release_meta,
                            fetch_jimaku=True,
                            run_pudge=True,
                        )
                        case_kind = "diagnostic"

                    try:
                        _register_stress_library_video(
                            config,
                            row,
                            video,
                            torrent_hash=torrent_hash or str(release_meta.get("info_hash") or ""),
                        )
                    except Exception as exc:
                        _append_stress_event(
                            corpus,
                            {
                                "event": "library_registration_error",
                                "stage": "post_pipeline",
                                "media_id": key[0],
                                "episode": key[1],
                                "title": str(row["title"]),
                                "video_name": video.name,
                                "error": str(exc),
                            },
                        )
                        print(f"  library normalization warning: {exc}", flush=True)

                    _append_stress_event(
                        corpus,
                        {
                            "event": "pipeline_ok",
                            "media_id": key[0],
                            "episode": key[1],
                            "title": str(row["title"]),
                            "case_kind": case_kind,
                            "case_dir": str(case_dir),
                            "cumulative_bytes": downloaded_bytes,
                        },
                    )
                    print(
                        f"  {case_kind.upper()} {case_dir} "
                        f"[{_human_bytes(downloaded_bytes)}/{_human_bytes(target_bytes)}]",
                        flush=True,
                    )
                    existing_keys.add(key)
                    completed_cases += 1
                    target_done = True
                    _write_corpus_summary(corpus)
                except (SubtitleBenchmarkError, QBittorrentError, Aria2Error) as exc:
                    _append_stress_event(
                        corpus,
                        {
                            "event": "attempt_failed",
                            "media_id": key[0],
                            "episode": key[1],
                            "title": str(row["title"]),
                            "source_release": release_meta,
                            "error": str(exc),
                            "cumulative_bytes": downloaded_bytes,
                        },
                    )
                    print(f"  reject: {exc}", flush=True)
                finally:
                    if torrent_hash and not bool(args.keep_videos):
                        try:
                            downloader.delete(torrent_hash, delete_files=True)
                        except (QBittorrentError, Aria2Error) as exc:
                            print(f"  cleanup warning ({backend_name}): {exc}", flush=True)
                if target_done or downloaded_bytes >= target_bytes:
                    break

            if not target_done:
                failed_targets += 1
    finally:
        downloader.close()
        try:
            _write_corpus_summary(corpus)
        except Exception:
            pass

    result = {
        "schema": "pudge-subtitle-random-stress-summary-v1",
        "finished_at": time.time(),
        "target_bytes": target_bytes,
        "downloaded_bytes": downloaded_bytes,
        "target_gib": round(target_bytes / 1024**3, 3),
        "downloaded_gib": round(downloaded_bytes / 1024**3, 3),
        "attempted_targets": attempted_targets,
        "completed_cases": completed_cases,
        "failed_targets": failed_targets,
        "seed": int(args.seed),
        "anime_limit": int(args.anime_limit),
    }
    write_json(corpus / "stress-summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def acquire_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.jimaku.api_key:
        raise RuntimeError("Jimaku API key is required for acquire-plan")

    corpus = args.corpus.expanduser()
    download_root = corpus / "downloads"
    download_root.mkdir(parents=True, exist_ok=True)
    completed = _completed_episode_keys(corpus)
    diagnostic_releases = _diagnostic_release_keys(corpus)
    diagnostic_media_releases = _diagnostic_media_release_keys(corpus)
    diagnostic_media_families = _diagnostic_media_release_family_keys(corpus)
    diagnostic_media_families.update(_source_rejection_family_keys(corpus))
    all_rows = _read_plan(args.plan)
    negative_titles_by_media = _plan_negative_titles_by_media(all_rows)
    source_strategy = str(getattr(args, "source_strategy", "plan-search") or "plan-search")
    feed_releases_by_key: dict[tuple[int, int], list[Any]] = {}
    if source_strategy == "erai-jp-feed":
        try:
            feed_rows, feed_releases_by_key, feed_stats = _discover_benchmark_feed(
                all_rows,
                config,
                queries=getattr(args, "feed_query", ()) or _DEFAULT_BENCHMARK_FEED_QUERIES,
                category=str(getattr(args, "feed_category", "1_0") or "1_0"),
                resolution=str(args.resolution),
                minimum_seeders=max(
                    1,
                    int(args.min_source_seeders),
                    int(config.nyaa.min_seeders),
                ),
                negative_titles_by_media=negative_titles_by_media,
                anilist_cache_path=corpus / "feed-anilist-cache.json",
            )
        except NyaaError as exc:
            print("Nyaa feed unavailable: " + _concise_nyaa_error(exc), flush=True)
            print("Rerun acquire-plan later; no generic releases were attempted.", flush=True)
            return 0
        print(
            "Erai JP/NF feed: "
            f"raw={feed_stats['raw']} eligible={feed_stats['eligible']} "
            f"matched={feed_stats['matched']} "
            f"(plan={feed_stats['plan_matched']} anilist={feed_stats['anilist_matched']} "
            f"cache={feed_stats['cache_matched']}) "
            f"unmatched={feed_stats['unmatched']} ambiguous={feed_stats['ambiguous']} "
            f"anilist_errors={feed_stats['anilist_errors']}",
            flush=True,
        )
        all_source_rows = feed_rows
    else:
        all_source_rows = all_rows
    start_index = max(0, int(args.start_index))
    rows = all_source_rows[start_index:]

    backend_name, downloader = _download_client(config, args.backend)
    print(f"Download backend: {backend_name}")
    processed = 0
    diagnostic_count = 0
    scanned = 0
    useful_limit = max(0, int(args.limit))
    episode_batch_size = max(1, int(args.episode_batch_size))
    try:
        for local_index, row in enumerate(rows):
            if useful_limit and processed >= useful_limit:
                break
            plan_index = start_index + local_index
            key = (int(row["media_id"]), int(row["episode"]))
            if key in completed:
                print(f"SKIP {plan_index}: {key[0]} E{key[1]} already benchmarked")
                continue

            scanned += 1
            # Look ahead only within the same AniList title. If we later choose
            # a season pack, aria2 can select several exact files and download
            # them together from the same swarm. Singles still stay one episode
            # per torrent.
            target_rows: list[dict[str, Any]] = []
            for candidate in rows[local_index:]:
                candidate_key = (int(candidate["media_id"]), int(candidate["episode"]))
                if candidate_key in completed or candidate_key[0] != key[0]:
                    continue
                target_rows.append(candidate)
                remaining = useful_limit - processed if useful_limit else episode_batch_size
                if len(target_rows) >= min(episode_batch_size, max(1, remaining)):
                    break
            target_episodes = [int(candidate["episode"]) for candidate in target_rows]
            target_suffix = ""
            if len(target_episodes) > 1:
                target_suffix = " targets=" + ",".join(f"E{episode}" for episode in target_episodes)
            case_label = "FEED" if source_strategy == "erai-jp-feed" else "CASE"
            print(
                f"{case_label} {plan_index}: #{row.get('rank')} {row['title']} "
                f"E{row['episode']}{target_suffix}"
            )

            max_bytes = int(max(0.1, float(args.max_download_gb)) * 1024**3)
            max_unverified_probe_bytes = int(
                max(1.0, float(getattr(args, "max_unverified_probe_mib", 700.0))) * 1024**2
            )
            negative_titles = negative_titles_by_media.get(key[0], ())
            resumable_releases = (
                []
                if source_strategy == "erai-jp-feed"
                else _benchmark_resumable_releases(
                    downloader,
                    row,
                    episodes=target_episodes,
                    root=download_root,
                    negative_titles=negative_titles,
                )
            )
            deferred_discovery = bool(resumable_releases)
            if source_strategy == "erai-jp-feed":
                releases = feed_releases_by_key.get(key, [])
            elif resumable_releases:
                print(
                    f"  found {len(resumable_releases)} resumable benchmark source(s); "
                    "try before fresh Nyaa discovery",
                    flush=True,
                )
                releases = resumable_releases
            else:
                try:
                    releases = _nyaa_ranked_for_plan(
                        row,
                        config,
                        resolution=args.resolution,
                        min_source_seeders=int(args.min_source_seeders),
                        negative_titles=negative_titles,
                    )
                except NyaaError as exc:
                    print(
                        "  Nyaa unavailable; ending acquisition cleanly: "
                        + _concise_nyaa_error(exc),
                        flush=True,
                    )
                    print(
                        "  rerun acquire-plan later; benchmark-owned downloads remain resumable",
                        flush=True,
                    )
                    break
            # Whole season packs may be tens of GiB, but aria2 only downloads
            # selected episode files. Apply the limit to singles here and to
            # every selected pack file after metadata arrives.
            attempted_release_keys: set[str] = set()
            releases = _usable_benchmark_releases(
                releases,
                row=row,
                corpus=corpus,
                max_bytes=max_bytes,
                diagnostic_releases=diagnostic_releases,
                diagnostic_media_releases=diagnostic_media_releases,
                diagnostic_media_families=diagnostic_media_families,
            )
            if not releases:
                if deferred_discovery:
                    print("  no usable resumable source; trying fresh Nyaa discovery")
                elif source_strategy == "erai-jp-feed":
                    print("  strict feed release is no longer usable; no generic fallback")
                    continue
                else:
                    print(
                        f"  no live Nyaa source at or below {args.resolution} "
                        f"with >= {max(1, int(args.min_source_seeders), int(config.nyaa.min_seeders))} listed seeders"
                    )
                    continue

            success = False
            release_index = 0
            phase_attempts = 0
            nyaa_unavailable = False
            while True:
                if (
                    release_index >= len(releases)
                    or phase_attempts >= max(1, int(args.max_release_attempts))
                ):
                    if not deferred_discovery:
                        break
                    deferred_discovery = False
                    print(
                        "  resumable sources produced no GOLD case; "
                        "trying fresh Nyaa discovery",
                        flush=True,
                    )
                    try:
                        discovered = _nyaa_ranked_for_plan(
                            row,
                            config,
                            resolution=args.resolution,
                            min_source_seeders=int(args.min_source_seeders),
                            negative_titles=negative_titles,
                        )
                    except NyaaError as exc:
                        print(
                            "  Nyaa unavailable; ending acquisition cleanly: "
                            + _concise_nyaa_error(exc),
                            flush=True,
                        )
                        print(
                            "  rerun acquire-plan later; benchmark-owned downloads "
                            "remain resumable",
                            flush=True,
                        )
                        nyaa_unavailable = True
                        break
                    releases = _usable_benchmark_releases(
                        discovered,
                        row=row,
                        corpus=corpus,
                        max_bytes=max_bytes,
                        diagnostic_releases=diagnostic_releases,
                        diagnostic_media_releases=diagnostic_media_releases,
                        diagnostic_media_families=diagnostic_media_families,
                        attempted_release_keys=attempted_release_keys,
                    )
                    release_index = 0
                    phase_attempts = 0
                    if not releases:
                        print(
                            f"  no fresh Nyaa source at or below {args.resolution} "
                            "after resumable attempts"
                        )
                    continue

                release = releases[release_index]
                release_index += 1
                phase_attempts += 1
                release_key = _release_diagnostic_key(row, release)[2]
                if release_key:
                    attempted_release_keys.add(release_key)
                is_pack = _benchmark_batch_release(release)
                source_kind = "pack" if is_pack else "single"
                explicit_jp = _benchmark_has_explicit_jp_signal(str(release.title))
                affinity, affinity_label = _benchmark_source_affinity(str(release.title))
                height = _benchmark_resolution_height(str(release.title))
                source_note = affinity_label if affinity else "generic"
                resolution_note = f"{height}p" if height else "?p"
                print(
                    f"  try {release.score:6.1f} seeds={int(release.seeders):3d} "
                    f"{source_kind:6s} {resolution_note:5s} src={source_note:20s} "
                    f"{release.size_text:>8} {release.title}"
                )
                torrent_hash = ""
                try:
                    requested = target_episodes if is_pack else [int(row["episode"])]
                    torrent_hash, videos = _download_release_videos(
                        downloader,
                        release,
                        episodes=requested,
                        root=download_root,
                        metadata_timeout_seconds=float(args.metadata_timeout_seconds),
                        download_timeout_seconds=float(args.download_timeout_minutes) * 60.0,
                        max_target_bytes=max_bytes,
                        stall_timeout_seconds=float(args.stall_timeout_seconds),
                        progress_interval_seconds=float(args.progress_interval_seconds),
                        identity_row=row,
                        probe_first=bool(is_pack and not explicit_jp),
                        probe_ffprobe_path=getattr(getattr(config, "tools", None), "ffprobe", "ffprobe"),
                        probe_ffmpeg_path=getattr(getattr(config, "tools", None), "ffmpeg", "ffmpeg"),
                        max_unverified_probe_bytes=max_unverified_probe_bytes,
                    )

                    release_meta = {
                        "title": str(release.title),
                        "info_hash": str(getattr(release, "info_hash", "") or ""),
                        "seeders": int(getattr(release, "seeders", 0) or 0),
                        "size_bytes": int(getattr(release, "size_bytes", 0) or 0),
                        "source_affinity": source_note,
                        "resolution": resolution_note,
                        "is_batch": bool(is_pack),
                        "declared_subtitle_languages": list(
                            _benchmark_declared_subtitle_languages(str(release.title))
                        ),
                        "release_family_key": _benchmark_release_family_key(release),
                    }
                    current_episode = int(row["episode"])
                    if not videos:
                        raise SubtitleBenchmarkError("downloaded release produced no usable episode file")

                    # Process every selected file. A no-oracle source is still
                    # valuable: preserve compact media, embedded text/Jimaku and
                    # production diagnostics, then continue trying another release
                    # for GOLD. Extra pack episodes can become GOLD independently.
                    rows_by_episode = {int(item["episode"]): item for item in target_rows}
                    current_gold = False
                    for episode, video in sorted(videos.items()):
                        plan_row = rows_by_episode.get(int(episode))
                        if plan_row is None:
                            continue
                        episode_key = (int(plan_row["media_id"]), int(episode))
                        if episode_key in completed:
                            continue
                        try:
                            case_dir = create_case_from_video(
                                video=video,
                                corpus_dir=corpus,
                                config=config,
                                media_id=int(plan_row["media_id"]),
                                title=str(plan_row["title"]),
                                episode=int(episode),
                                fetch_jimaku=True,
                                run_pudge=True,
                            )
                        except SubtitleBenchmarkError as exc:
                            if "no embedded Japanese text subtitle" not in str(exc):
                                print(f"  E{episode} reject: {exc}")
                                continue
                            diagnostic = create_diagnostic_case_from_video(
                                video=video,
                                corpus_dir=corpus,
                                config=config,
                                media_id=int(plan_row["media_id"]),
                                title=str(plan_row["title"]),
                                episode=int(episode),
                                reason="no_embedded_japanese_text_subtitle",
                                source_release=release_meta,
                                fetch_jimaku=True,
                                run_pudge=True,
                            )
                            release_key = str(
                                release_meta["info_hash"] or release_meta["title"]
                            ).casefold()
                            diagnostic_releases.add(
                                (int(plan_row["media_id"]), int(episode), release_key)
                            )
                            if is_pack:
                                diagnostic_media_releases.add(
                                    (int(plan_row["media_id"]), release_key)
                                )
                                family_key = str(release_meta.get("release_family_key") or "")
                                if family_key:
                                    diagnostic_media_families.add(
                                        (int(plan_row["media_id"]), family_key)
                                    )
                            diagnostic_count += 1
                            print(f"  diagnostic E{episode} {diagnostic}")
                            _write_corpus_summary(corpus)
                            continue

                        print(f"  GOLD E{episode} {case_dir}")
                        completed.add(episode_key)
                        processed += 1
                        _write_corpus_summary(corpus)
                        if int(episode) == current_episode:
                            current_gold = True
                    success = current_gold
                except SubtitleBenchmarkError as exc:
                    print(f"  reject release: {exc}")
                except (QBittorrentError, Aria2Error) as exc:
                    print(f"  {backend_name}: {exc}")
                else:
                    # Normal processing completed (GOLD and/or diagnostic). The
                    # compact corpus is durable, so the heavy source can go.
                    if torrent_hash:
                        try:
                            downloader.delete(torrent_hash, delete_files=True)
                        except (QBittorrentError, Aria2Error) as exc:
                            print(f"  cleanup warning ({backend_name}): {exc}")
                        torrent_hash = ""
                if success:
                    break
            if nyaa_unavailable:
                break
            if not success:
                print("  no release produced a benchmark case")
    finally:
        downloader.close()
        try:
            _write_corpus_summary(corpus)
        except Exception:
            # Never mask Ctrl+C / the original acquisition error merely because
            # a best-effort summary refresh failed during shutdown.
            pass

    summary = _write_corpus_summary(corpus)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Processed GOLD cases: {processed}")
    print(f"Saved diagnostic releases: {diagnostic_count}")
    print(f"Plan rows scanned: {scanned}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)

    if args.command == "evaluate":
        report = evaluate_subtitle_files(args.candidate, args.oracle, ngram=max(3, int(args.ngram)))
        payload = report.as_dict()
        if args.output:
            write_json(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ingest":
        config = load_config(args.config)
        case_dir = create_case_from_video(
            video=args.video,
            corpus_dir=args.corpus,
            config=config,
            media_id=args.media_id,
            title=args.title,
            episode=args.episode,
            fetch_jimaku=not args.no_jimaku,
            run_pudge=not args.no_replay,
        )
        print(case_dir)
        return 0

    if args.command == "aggregate":
        summary = _aggregate_corpus(args.corpus)
        if args.output:
            write_json(args.output, summary)
        else:
            write_json(args.corpus / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "plan-top":
        config = load_config(args.config)
        payload = fetch_top_anime_plan(
            config.anilist.endpoint,
            limit=args.limit,
            sort=args.sort,
            sample_episodes=args.sample_episodes,
            seed=args.seed,
            cache_dir=args.cache_dir or (args.output.parent / ".anilist-cache"),
            max_retries=args.anilist_retries,
            retry_base_seconds=args.retry_base_seconds,
        )
        write_json(args.output, payload)
        print(
            f"anime={payload['anime_count']} episodes={payload['episode_count']} "
            f"unresolved={len(payload['unresolved_anime'])} -> {args.output}"
        )
        return 0

    if args.command == "acquire-plan":
        return acquire_plan(args)

    if args.command == "stress-random":
        return acquire_random_stress(args)

    if args.command == "repair-stress-library":
        return repair_stress_library(args)

    if args.command == "replay-stress":
        return replay_stress(args)

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
