"""“Met often, not in deck” on the Statistics page.

Words you do not know (new, or in none of your decks) ranked by how many times you really
met them in the selected period: the subtitle lines shown while watching, the manga pages
looked at, the visual-novel lines read and the light-novel passages read. The count is
built from the word units Statistics already keeps (consumption_words) joined with the word
states kept on this Mac: opening the page asks Jiten for nothing. Words whose state is not
kept yet are left out and counted as "unchecked"; the page then asks for them in batches
(``check``), and names words nothing has named yet on demand (episode_words.word_names_payload).
"""

from __future__ import annotations

import json
import threading
import time
from bisect import bisect_right
from collections import defaultdict
from typing import Any

from . import jiten_client
from .consumption_volume import prior_volume_state
from .consumption_words import READY, _anime_intervals, _cue_pairs, _manga_pages, vn_line_key
from .episode_words import study_backend, word_row, word_states
from .word_states import word_state_group

Pair = tuple[int, int]
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# Unchecked words asked about per "check" (three batched requests at most).
CHECK_WORDS = 1500
CACHE_SECONDS = 45.0
SOURCES_SHOWN = 4


class PeriodCount:
    def __init__(self) -> None:
        self.counts: dict[Pair, int] = defaultdict(int)
        # pair -> media_uuid -> times met there
        self.sources: dict[Pair, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.titles: dict[str, tuple[str, str]] = {}  # media_uuid -> (title, kind)
        self.pending = 0

    def add(self, media_uuid: str, pairs: Any) -> None:
        for pair in pairs:
            pair = (int(pair[0]), int(pair[1]))
            self.counts[pair] += 1
            self.sources[pair][media_uuid] += 1


def _work_title(title: str, kind: str) -> str:
    """Episodes of one anime are one source ("Title · 3" -> "Title")."""
    if kind in {"anime", "anime_episode"} and " · " in title:
        return title.rsplit(" · ", 1)[0]
    return title


def count_period(statistics: Any, normalized: dict[str, Any]) -> PeriodCount:
    """Every word met in the period, with where. Reads only what is already parsed."""
    # Listening has no words to count here: its rows (most of a long history) are not loaded.
    segments, _journal, _corrections = statistics._effective_segments(
        normalized, without_kinds=("audiobook",)
    )
    store = statistics.word_units
    result = PeriodCount()
    vn_media: dict[str, str] = {}
    for segment in segments:
        kind = str(segment.get("kind") or "")
        for media in segment.get("media") or []:
            media_uuid = str(media.get("media_uuid") or "")
            if media_uuid and media_uuid not in result.titles:
                result.titles[media_uuid] = (
                    str(media.get("title_snapshot") or ""),
                    str(media.get("media_kind") or kind),
                )
            if kind == "visual_novel" and media_uuid and segment.get("session_id"):
                vn_media.setdefault(str(segment["session_id"]), media_uuid)
    anime = _anime_intervals(segments)
    manga = _manga_pages(segments)
    vn_lines: dict[str, set[str]] = defaultdict(set)
    if vn_media:
        for session, _at, text in store.vn_lines(
            session_ids=set(vn_media), start=normalized["start_utc"], end=normalized["end_utc"]
        ):
            vn_lines[session].add(" ".join(text.split()))
    keys = {f"anime:{media_uuid}" for _session, media_uuid in anime}
    keys.update(key for pages in manga.values() for key in pages)
    keys.update(vn_line_key(text) for texts in vn_lines.values() for text in texts)
    known = store.lookup(keys)
    result.pending = sum(1 for key in keys if known.get(key, (None, None))[0] != READY)
    for (_session, media_uuid), intervals in anime.items():
        status, words = known.get(f"anime:{media_uuid}", (None, None))
        if status == READY:
            result.add(media_uuid, _cue_pairs((words or {}).get("cues"), intervals))
    for pages in manga.values():
        for key in pages:
            status, words = known.get(key, (None, None))
            if status == READY:
                result.add(key.split(":", 2)[1], (words or {}).get("words") or [])
    for session, texts in vn_lines.items():
        for text in texts:
            status, words = known.get(vn_line_key(text), (None, None))
            if status == READY:
                result.add(vn_media.get(session, ""), (words or {}).get("words") or [])
    if getattr(statistics, "ln_words", None) is not None:
        numeric = prior_volume_state(statistics.database, segments)["numeric"]
        for key, ranges in numeric.items():
            if len(key) != 4 or key[2] != "light_novel" or str(key[3]).startswith("chapter:"):
                continue
            positions = statistics._chapter_words(str(key[3]))
            if positions is None:
                result.pending += 1
                continue
            offsets = [row[0] for row in positions]
            for start, end in ranges:
                first = bisect_right(offsets, start - 1e-9)
                last = bisect_right(offsets, end - 1e-9)
                result.add(str(key[0]), ((row[1], row[2]) for row in positions[first:last]))
    return result


class MetWords:
    def __init__(self, statistics: Any, service: Any, *, logger: Any = None) -> None:
        self.statistics = statistics
        self.service = service
        self.logger = logger
        self._lock = threading.Lock()
        self._cache: tuple[str, float, PeriodCount] | None = None

    def _period(self, scope: dict[str, Any] | None, *, fresh: bool = False) -> PeriodCount:
        normalized = self.statistics._scope(scope)
        key = json.dumps(
            {name: value for name, value in normalized.items() if name != "end_utc"},
            sort_keys=True,
            default=str,
        )
        with self._lock:
            cached = self._cache
            if not fresh and cached is not None and cached[0] == key:
                if time.monotonic() - cached[1] < CACHE_SECONDS:
                    return cached[2]
        counted = count_period(self.statistics, normalized)
        with self._lock:
            self._cache = (key, time.monotonic(), counted)
        # Kept for "show more", the names toggle and the check that follow at once, then dropped.
        timer = threading.Timer(CACHE_SECONDS * 2, self.release_idle)
        timer.daemon = True
        timer.start()
        return counted

    def release_idle(self, force: bool = False) -> None:
        with self._lock:
            cached = self._cache
            if cached is not None and (force or time.monotonic() - cached[1] >= CACHE_SECONDS):
                self._cache = None

    def _sources(self, period: PeriodCount, pair: Pair) -> tuple[list[dict[str, Any]], int]:
        merged: dict[tuple[str, str], int] = defaultdict(int)
        for media_uuid, count in period.sources.get(pair, {}).items():
            title, kind = period.titles.get(media_uuid, ("", ""))
            merged[(_work_title(title, kind), kind)] += count
        rows = sorted(merged.items(), key=lambda item: (-item[1], item[0][0].casefold()))
        shown = [{"title": title, "kind": kind, "count": count} for (title, kind), count in rows]
        return shown[:SOURCES_SHOWN], max(0, len(shown) - SOURCES_SHOWN)

    def query(
        self,
        scope: dict[str, Any] | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        hide_names: bool = False,
        check: bool = False,
        fresh: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        service = self.service
        if study_backend(service) != "jiten":
            return {"ok": False, "reason": "jiten_only"}
        if not str(getattr(service.settings(), "jiten_api_key", "") or "").strip():
            return {"ok": False, "reason": "no_token"}
        limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
        period = self._period(scope, fresh=fresh)
        counts = period.counts
        if not counts:
            return {"ok": False, "reason": "no_words", "pending_units": period.pending}
        ranked = sorted(counts, key=lambda pair: (-counts[pair], pair))
        states = word_states(service, ranked, live=False)
        paused = jiten_client.pause_reason()
        checked = 0
        if check and not paused:
            # One batched request per 500 words (never one per word), most met first.
            # The words about to be shown are asked about too: a kept state may be days old.
            shown = [pair for pair in ranked if word_state_group(states.get(pair, "")) == "new"]
            asked = [pair for pair in ranked if pair not in states][:CHECK_WORDS] + shown[: limit * 2]
            if asked:
                answered = word_states(service, asked, live=True)
                checked = sum(1 for pair in asked if pair in answered)
                states.update(answered)
                paused = jiten_client.pause_reason()
        candidates = [pair for pair in ranked if word_state_group(states.get(pair, "unknown")) == "new"]
        unchecked = sum(1 for pair in ranked if pair not in states)
        rows: list[dict[str, Any]] = []
        hidden_names = 0
        names_marked = False
        position = 0
        # Names of the candidates are read a screenful at a time (hidden proper names
        # make the list reach further down the ranking).
        has_more = False
        while position < len(candidates) and not has_more:
            chunk = candidates[position : position + max(100, limit * 2)]
            position += len(chunk)
            names = service.word_names(chunk, fetch=0)
            for pair in chunk:
                info = names.get(pair)
                if info and info.get("pos"):
                    names_marked = True
                hidden = bool(hide_names and info and info.get("is_name"))
                if len(rows) >= limit:
                    # "Show more" only when a word that would be shown is left.
                    if not hidden:
                        has_more = True
                        break
                    continue
                if hidden:
                    hidden_names += 1
                    continue
                row = word_row(pair, counts[pair], info, states.get(pair, ""))
                row["sources"], row["more_sources"] = self._sources(period, pair)
                rows.append(row)
        result = {
            "ok": True,
            "backend": "jiten",
            "words": rows,
            "candidates": len(candidates),
            "has_more": has_more,
            "unique_words": len(counts),
            "tokens": sum(counts.values()),
            "unchecked": unchecked,
            "checked": checked,
            "pending_units": period.pending,
            "hidden_names": hidden_names,
            "names_marked": names_marked,
            "unnamed": sum(1 for row in rows if not row["spelling"]),
            "paused": paused,
        }
        if self.logger is not None:
            self.logger.info(
                "EVENT statistics.met_words words=%d candidates=%d unchecked=%d check=%s ms=%.1f",
                len(counts),
                len(candidates),
                unchecked,
                bool(check),
                (time.perf_counter() - started) * 1000,
            )
        return result


def manager_for(api: Any) -> MetWords:
    lock = api.__dict__.setdefault("_met_words_lock", threading.Lock())
    with lock:
        manager = api.__dict__.get("_met_words_manager")
        if manager is None:
            manager = api.__dict__["_met_words_manager"] = MetWords(
                api.consumption_statistics, api.light_novels, logger=getattr(api, "logger", None)
            )
        return manager
