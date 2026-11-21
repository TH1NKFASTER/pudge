"""“Episode words” of an anime: how much of an episode (or of every episode with subtitles)
you know, and its most frequent new words — the anime twin of the manga “Volume words”.

The words of an episode are its word unit ``anime:<media_uuid>`` (consumption_words): the
unit Statistics counts watched words from. An episode already counted costs nothing; one
that is not is parsed here (one Jiten parse per episode, kept as that unit, so Statistics
does not parse it again). Parsing waits for Jiten's rate limit like every background job;
a whole title also waits for the energy rules (energy saving, heat, a video playing) unless
the user says "count now": what waits is queued for the Statistics words job.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import jiten_client
from .consumption_words import READY, WordUnits
from .i18n import err, t
from .review_episode_due import episode_cue_words, resolve_episode_review_subtitle
from .word_states import word_state_group

Pair = tuple[int, int]
DEFAULT_LIMIT = 40
MAX_LIMIT = 200


class _Paused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ shared by the word lists
def study_backend(service: Any) -> str:
    return str(getattr(service.settings(), "study_backend", "") or "jiten").casefold()


def word_states(service: Any, pairs: Iterable[Pair], *, live: bool) -> dict[Pair, str]:
    """normalizedState of words: the states kept on this Mac, and (``live``) Jiten's answer
    for all of them in batches of 500 — never one request per word."""
    wanted = list(dict.fromkeys(pairs))
    cached = getattr(service, "jiten_cached_states", None)
    states: dict[Pair, str] = dict(cached(wanted)) if callable(cached) else {}
    if not live:
        return states
    for start in range(0, len(wanted), 500):
        if jiten_client.pause_reason():
            break
        try:
            rows = service.jiten_live_states(wanted[start : start + 500], force=False)
        except Exception:  # noqa: BLE001 - the kept states stay
            break
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                pair = (int(row["wordId"]), int(row["readingIndex"]))
            except (KeyError, TypeError, ValueError):
                continue
            state = str(row.get("normalizedState") or "")
            if state:
                states[pair] = state
    return states


def word_row(pair: Pair, count: int, info: dict[str, Any] | None, state: str = "") -> dict[str, Any]:
    info = info or {}
    return {
        "word_id": int(pair[0]),
        "reading_index": int(pair[1]),
        "spelling": str(info.get("spelling") or ""),
        "reading": str(info.get("reading") or ""),
        "meaning": str(info.get("meaning") or "")[:120],
        "count": int(count),
        "frequency_rank": info.get("frequency_rank"),
        "is_name": bool(info.get("is_name")),
        "state": state,
    }


def word_names_payload(service: Any, pairs: Any, *, limit: int = 200) -> dict[str, Any]:
    """Names of the listed words for a page that shows them without (a lazy look-up: the
    unnamed ones are fetched on the paced background queue and come with a later call)."""
    wanted: list[Pair] = []
    for item in pairs or []:
        try:
            pair = (int(item[0]), int(item[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if pair[0] > 0 and pair[1] >= 0 and pair not in wanted:
            wanted.append(pair)
    wanted = wanted[: max(1, int(limit))]
    paused = jiten_client.pause_reason()
    names = service.word_names(wanted, fetch=0 if paused else len(wanted)) if wanted else {}
    return {
        "ok": True,
        "paused": paused,
        "words": [word_row(pair, 0, names[pair]) for pair in wanted if pair in names],
        "missing": sum(1 for pair in wanted if pair not in names),
    }


def summarize(
    service: Any, counts: dict[Pair, int], states: dict[Pair, str], *, limit: int
) -> dict[str, Any]:
    """The “Volume words” payload for counted words: coverage and the most frequent new words."""
    total = sum(counts.values())
    by_state: dict[str, int] = {}
    unique_by_state: dict[str, int] = {}
    for pair, count in counts.items():
        group = word_state_group(states.get(pair, "unknown"))
        by_state[group] = by_state.get(group, 0) + count
        unique_by_state[group] = unique_by_state.get(group, 0) + 1
    new_pairs = [pair for pair in counts if word_state_group(states.get(pair, "unknown")) == "new"]
    names = service.word_names(new_pairs, fetch=0) if new_pairs else {}
    new_pairs.sort(
        key=lambda pair: (-counts[pair], int(names.get(pair, {}).get("frequency_rank") or 10**9), pair)
    )
    top = new_pairs[:limit]
    unnamed = [pair for pair in top if pair not in names]
    if unnamed and not jiten_client.pause_reason():
        names.update(service.word_names(unnamed, fetch=len(unnamed)))
    known = by_state.get("known", 0)
    learning = by_state.get("learning", 0)
    return {
        "ok": True,
        "backend": "jiten",
        "tokens": total,
        "unique_words": len(counts),
        "known_share": round(known / total, 4) if total else 0.0,
        "known_or_learning_share": round((known + learning) / total, 4) if total else 0.0,
        "new_unique": unique_by_state.get("new", 0),
        "unknown_state_unique": unique_by_state.get("unknown", 0),
        "top_new": [word_row(pair, counts[pair], names.get(pair), states.get(pair, "")) for pair in top],
        "unnamed": sum(1 for pair in top if pair not in names),
    }


# ------------------------------------------------------------------ episodes of a title
def _has_subtitles(item: Any) -> bool:
    subtitle = getattr(item, "subtitle_path", None)
    try:
        if subtitle is not None and Path(subtitle).is_file():
            return True
    except OSError:
        pass
    return getattr(item, "embedded_subtitle_id", None) is not None


def title_episodes(api: Any, anime: Any) -> dict[int, Any]:
    """Library rows of a title by episode number (of several files of one episode: the one
    with subtitles)."""
    single = bool(api._planning_single_file_title(anime))
    result: dict[int, Any] = {}
    for item in api.manager.db.episodes(anime.media_id):
        number = api._display_episode_number(anime, item.episode)
        if number is None:
            if not single:
                continue
            number = 1
        known = result.get(int(number))
        if known is not None and (_has_subtitles(known) or not _has_subtitles(item)):
            continue
        result[int(number)] = item
    return result


def _episode_end(api: Any, anime: Any, item: Any, number: int) -> int | None:
    end = getattr(item, "media_episode_end", None)
    end = api._display_episode_number(anime, end) if end else None
    return int(end) if end and int(end) > int(number) else None


def _units(api: Any) -> WordUnits:
    statistics = getattr(api, "consumption_statistics", None)
    units = getattr(statistics, "word_units", None)
    return units if units is not None else WordUnits(api.manager.db)


def _unit_key(api: Any, item: Any) -> str:
    media = api.consumption.anime_episode_media(Path(item.video_path))
    return f"anime:{media.media_uuid}" if media is not None else ""


def options(api: Any, media_id: int) -> dict[str, Any]:
    """The episode picker: every episode in the library, and the next one to watch."""
    anime = api.manager.db.get_anime(int(media_id))
    if anime is None:
        raise KeyError(err("web.unknownAnime", mediaId=media_id))
    rows = title_episodes(api, anime)
    episodes = []
    for number in sorted(rows):
        item = rows[number]
        episodes.append(
            {
                "episode": number,
                "episode_end": _episode_end(api, anime, item, number),
                "subtitles": _has_subtitles(item),
                "watched": bool(str(item.state) == "watched" or getattr(item, "watched_at", None)),
            }
        )
    progress = int(getattr(anime, "progress", 0) or 0)
    # The next unwatched episode: after the AniList progress, else the first file not watched.
    default = next((row["episode"] for row in episodes if row["episode"] > progress), None)
    if default is None or progress <= 0:
        default = next((row["episode"] for row in episodes if not row["watched"]), default)
    if default is None and episodes:
        default = episodes[-1]["episode"]
    return {
        "media_id": int(anime.media_id),
        "title": anime.title,
        "episodes": episodes,
        "default_episode": default,
        "with_subtitles": sum(1 for row in episodes if row["subtitles"]),
    }


class EpisodeWords:
    """One running count per title; the last result is kept for the open dialog."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------ jobs
    def start(
        self,
        media_id: int,
        episode: int | None = None,
        limit: int = DEFAULT_LIMIT,
        force: bool = False,
    ) -> dict[str, Any]:
        api = self.api
        anime = api.manager.db.get_anime(int(media_id))
        if anime is None:
            raise KeyError(err("web.unknownAnime", mediaId=media_id))
        limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
        job = {
            "id": uuid.uuid4().hex,
            "media_id": int(anime.media_id),
            "episode": int(episode) if episode is not None else None,
            "state": "running",
            "current": 0,
            "total": 0,
            "result": None,
            "error": "",
            "cancel": threading.Event(),
            "started_at": time.time(),
            "center_id": "",
        }
        with self._lock:
            running = self._jobs.get(job["media_id"])
            if running is not None and running["state"] == "running":
                if running["episode"] == job["episode"]:
                    return self._payload(running)
                running["cancel"].set()  # another episode was asked for: that one is shown
            self._jobs[job["media_id"]] = job
        center = getattr(api, "job_center", None)
        if center is not None:
            try:
                job["center_id"] = center.start(
                    "episode_words",
                    t("episodeWords.jobTitle", title=anime.title),
                    payload={"media_id": job["media_id"], "episode": job["episode"]},
                )
            except Exception:  # noqa: BLE001 - the journal is optional
                job["center_id"] = ""
        thread = threading.Thread(
            target=self._run, args=(job, anime, limit, bool(force)), name="episode-words", daemon=True
        )
        job["thread"] = thread
        thread.start()
        return self._payload(job)

    def status(self, media_id: int) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(int(media_id))
        return self._payload(job) if job is not None else {"media_id": int(media_id), "state": "idle"}

    def cancel(self, media_id: int) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(int(media_id))
        if job is not None and job["state"] == "running":
            job["cancel"].set()
        return self.status(media_id)

    def wait(self, media_id: int, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(int(media_id))
        thread = (job or {}).get("thread")
        if thread is not None:
            thread.join(timeout)
        return self.status(media_id)

    @staticmethod
    def _payload(job: dict[str, Any]) -> dict[str, Any]:
        total = int(job["total"] or 0)
        return {
            "media_id": job["media_id"],
            "job_id": job["id"],
            "episode": job["episode"],
            "state": job["state"],
            "busy": job["state"] == "running",
            "current": int(job["current"] or 0),
            "total": total,
            "progress": max(0.0, min(1.0, job["current"] / total)) if total else 0.0,
            "error": job["error"],
            "result": job["result"],
        }

    def _progress(self, job: dict[str, Any], current: int, total: int) -> None:
        job["current"], job["total"] = current, total
        center = getattr(self.api, "job_center", None)
        if center is not None and job["center_id"]:
            try:
                center.update(
                    job["center_id"],
                    current=current,
                    total=total,
                    message=t("episodeWords.jobProgress", current=current, total=total),
                )
            except Exception:  # noqa: BLE001
                pass

    def _run(self, job: dict[str, Any], anime: Any, limit: int, force: bool) -> None:
        api = self.api
        center = getattr(api, "job_center", None) if job["center_id"] else None
        try:
            result = self._count(job, anime, limit, force)
        except Exception as exc:  # noqa: BLE001 - the dialog shows why
            job["error"] = str(exc)[:300]
            job["result"] = {"ok": False, "reason": "parse_failed", "detail": job["error"]}
            job["state"] = "failed"
            api.logger.warning("FAIL step=episode_words media=%s error=%r", job["media_id"], job["error"])
            if center is not None:
                center.fail(job["center_id"], job["error"])
            return
        if job["cancel"].is_set():
            job["state"] = "cancelled"
            if center is not None:
                center.cancelled(job["center_id"])
            return
        job["result"] = result
        job["state"] = "done"
        if center is not None:
            center.finish(job["center_id"], message=t("episodeWords.jobDone"))
        api.logger.info(
            "DONE episode_words media=%s episode=%s ok=%s counted=%s waiting=%s paused=%s",
            job["media_id"],
            job["episode"],
            result.get("ok"),
            result.get("episodes_counted"),
            len(result.get("episodes_waiting") or []),
            result.get("paused") or "",
        )

    # ------------------------------------------------------------------ counting
    def _gate(self, *, bulk: bool, force: bool) -> str:
        """Why an episode must not be parsed now ("" when it may)."""
        reason = jiten_client.pause_reason()
        if reason or not bulk or force:
            return str(reason or "")
        return str(self.api.manager.work_scheduler.background_wait_reason() or "")

    def _episode_cues(
        self, units: WordUnits, item: Any, *, bulk: bool, force: bool
    ) -> tuple[list[Any] | None, str]:
        """The episode's subtitle lines with their words, and where they came from:
        "unit" (already counted), "parsed", "no_subtitles"; raises _Paused."""
        api = self.api
        key = _unit_key(api, item)
        if key:
            status, words = units.lookup([key]).get(key, (None, None))
            if status == READY:
                return list((words or {}).get("cues") or []), "unit"
        if not _has_subtitles(item):
            return None, "no_subtitles"
        reason = self._gate(bulk=bulk, force=force)
        if reason:
            raise _Paused(reason)
        try:
            subtitle_path, _meta = resolve_episode_review_subtitle(
                video_path=Path(item.video_path),
                subtitle_path=getattr(item, "subtitle_path", None),
                embedded_subtitle_id=getattr(item, "embedded_subtitle_id", None),
                cache_dir=api.config.paths.cache_dir,
                ffmpeg_path=api.config.tools.ffmpeg,
                ffprobe_path=api.config.tools.ffprobe,
            )
        except (FileNotFoundError, ValueError, OSError):
            return None, "no_subtitles"
        try:
            with jiten_client.background_requests(bulk):
                cues = episode_cue_words(api.light_novels, subtitle_path, api.config.paths.cache_dir)
        except Exception as exc:
            # JitenUnavailableError: nothing was sent (rate limit, outage) — wait, not fail.
            if hasattr(exc, "retry_after") and hasattr(exc, "reason"):
                raise _Paused(str(getattr(exc, "reason", "") or "jiten_unavailable")) from exc
            raise
        if key:
            units.enqueue({key: ("anime", {"media_uuid": key.split(":", 1)[1]})})
            units.store(key, {"cues": cues})
        return cues, "parsed"

    def _count(self, job: dict[str, Any], anime: Any, limit: int, force: bool) -> dict[str, Any]:
        api = self.api
        service = api.light_novels
        if study_backend(service) != "jiten":
            return {"ok": False, "reason": "jiten_only"}
        if not str(getattr(service.settings(), "jiten_api_key", "") or "").strip():
            return {"ok": False, "reason": "no_token"}
        rows = title_episodes(api, anime)
        single = job["episode"] is not None
        if single:
            item = rows.get(int(job["episode"]))
            if item is None:
                return {"ok": False, "reason": "no_episode"}
            chosen = {int(job["episode"]): item}
        else:
            chosen = rows
            if not chosen:
                return {"ok": False, "reason": "no_episodes"}
        units = _units(api)
        counts: dict[Pair, int] = {}
        counted: list[int] = []
        without: list[int] = []
        waiting: list[int] = []
        paused = ""
        parsed = 0
        self._progress(job, 0, len(chosen))
        for index, number in enumerate(sorted(chosen)):
            if job["cancel"].is_set():
                return {"ok": False, "reason": "cancelled"}
            item = chosen[number]
            if paused:
                # Episodes already counted are still read; the others wait.
                key = _unit_key(api, item)
                known = units.lookup([key]).get(key, (None, None)) if key else (None, None)
                cues = list((known[1] or {}).get("cues") or []) if known[0] == READY else None
                source = (
                    "unit" if cues is not None else ("waiting" if _has_subtitles(item) else "no_subtitles")
                )
            else:
                try:
                    cues, source = self._episode_cues(units, item, bulk=not single, force=force)
                except _Paused as wait:
                    paused, cues, source = wait.reason, None, "waiting"
            if source == "parsed":
                parsed += 1
            if cues is None:
                (waiting if source == "waiting" else without).append(number)
            else:
                counted.append(number)
                for cue in cues:
                    try:
                        words = cue[2]
                    except (TypeError, IndexError):
                        continue
                    for word in words or []:
                        try:
                            pair = (int(word[0]), int(word[1]))
                        except (TypeError, ValueError, IndexError):
                            continue
                        counts[pair] = counts.get(pair, 0) + 1
            self._progress(job, index + 1, len(chosen))
        if waiting:
            # What could not be parsed now is left to the Statistics words job.
            queued = {}
            for number in waiting:
                key = _unit_key(api, chosen[number])
                if key:
                    queued[key] = ("anime", {"media_uuid": key.split(":", 1)[1]})
            units.enqueue(queued)
            starter = getattr(api, "statistics_words_start", None)
            if queued and callable(starter):
                try:
                    starter()
                except Exception:  # noqa: BLE001 - it starts on the next look at Statistics
                    pass
        extra = {
            "scope": "episode" if single else "title",
            "episode": job["episode"],
            "episodes_total": len(chosen),
            "episodes_counted": len(counted),
            "episodes_parsed": parsed,
            "episodes_without_subtitles": without,
            "episodes_waiting": waiting,
            "paused": paused,
        }
        if not counts:
            if waiting:
                reason = "paused"
            elif without and not counted:
                reason = "no_subtitles"
            else:
                reason = "no_words"
            return {"ok": False, "reason": reason, **extra}
        states = word_states(service, counts, live=True)
        return {**summarize(service, counts, states, limit=limit), **extra}


def manager_for(api: Any) -> EpisodeWords:
    """The api's one EpisodeWords (made on first use)."""
    lock = api.__dict__.setdefault("_episode_words_lock", threading.Lock())
    with lock:
        manager = api.__dict__.get("_episode_words_manager")
        if manager is None:
            manager = api.__dict__["_episode_words_manager"] = EpisodeWords(api)
        return manager
