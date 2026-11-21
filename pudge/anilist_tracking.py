from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from .i18n import err
from .fsutil import atomic_write_json
from .models import AniListAnime, VideoIdentity


_ANILIST_ID_RE = re.compile(r"(?:anilist\.co/anime/)?(?P<id>\d+)", re.IGNORECASE)


@dataclass(slots=True)
class TrackingPayload:
    video: str
    title: str
    media_id: int
    episode: int
    total_episodes: int | None
    threshold: float
    mapping_key: str
    # Two episodes in one file (audit 17А): the second episode and where it starts.
    episode_end: int | None = None
    part_boundary: float | None = None


def mapping_key(video: Path, identity: VideoIdentity) -> str:
    parent = str(video.expanduser().resolve().parent)
    identity_part = "|".join(
        [
            identity.title.casefold().strip(),
            str(identity.year or ""),
            str(identity.season or ""),
        ]
    )
    return hashlib.sha256(f"{parent}\0{identity_part}".encode("utf-8")).hexdigest()


def mappings_path(cache_dir: Path) -> Path:
    return cache_dir / "anilist" / "mappings.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload, mode=0o600, ensure_ascii=False, indent=2)


def load_mapping(cache_dir: Path, key: str) -> AniListAnime | None:
    path = mappings_path(cache_dir)
    data = _load_json(path)
    item = data.get(key)
    if not isinstance(item, dict):
        return None
    expires_at = float(item.get("expires_at") or 0)
    if expires_at and expires_at < time.time():
        data.pop(key, None)
        _write_json(path, data)
        return None
    try:
        return AniListAnime(
            id=int(item["media_id"]),
            titles=[str(value) for value in item.get("titles", []) if value],
            synonyms=[str(value) for value in item.get("synonyms", []) if value],
            season_year=int(item["season_year"]) if item.get("season_year") else None,
            episodes=int(item["episodes"]) if item.get("episodes") else None,
            format=str(item["format"]) if item.get("format") else None,
            score=float(item.get("score") or 1000.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_mapping(
    cache_dir: Path,
    key: str,
    anime: AniListAnime,
    *,
    corrected: bool = False,
    ttl_hours: float = 24.0,
) -> None:
    path = mappings_path(cache_dir)
    data = _load_json(path)
    ttl = 28 * 24 if corrected else max(1.0, ttl_hours)
    data[key] = {
        "media_id": anime.id,
        "titles": anime.titles,
        "synonyms": anime.synonyms,
        "season_year": anime.season_year,
        "episodes": anime.episodes,
        "format": anime.format,
        "score": anime.score,
        "corrected": corrected,
        "expires_at": time.time() + ttl * 3600,
    }
    _write_json(path, data)


# A run's tracking file outlives mpv: the "count on close" helper starts as mpv
# exits and reads it a moment later. Old ones are removed at the next launch;
# a day is far longer than any player session.
TRACKING_FILE_MAX_AGE = 24 * 3600


def remove_stale_tracking_files(run_dir: Path, *, max_age: float = TRACKING_FILE_MAX_AGE) -> None:
    cutoff = time.time() - max_age
    try:
        paths = list(run_dir.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def create_tracking_file(cache_dir: Path, payload: TrackingPayload) -> Path:
    run_dir = cache_dir / "anilist" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_tracking_files(run_dir)
    token = hashlib.sha256(
        f"{payload.video}\0{payload.media_id}\0{payload.episode}\0{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:20]
    path = run_dir / f"{token}.json"
    _write_json(path, asdict(payload))
    return path


def read_tracking_file(path: Path) -> TrackingPayload:
    payload = _load_json(path)
    return TrackingPayload(
        video=str(payload["video"]),
        title=str(payload["title"]),
        media_id=int(payload["media_id"]),
        episode=int(payload["episode"]),
        total_episodes=int(payload["total_episodes"]) if payload.get("total_episodes") else None,
        threshold=float(payload["threshold"]),
        mapping_key=str(payload["mapping_key"]),
        episode_end=int(payload["episode_end"]) if payload.get("episode_end") else None,
        part_boundary=float(payload["part_boundary"]) if payload.get("part_boundary") else None,
    )


def update_tracking_media(path: Path, anime: AniListAnime) -> TrackingPayload:
    payload = read_tracking_file(path)
    payload.media_id = anime.id
    payload.title = anime.titles[0] if anime.titles else str(anime.id)
    payload.total_episodes = anime.episodes
    _write_json(path, asdict(payload))
    return payload


def parse_anilist_id(value: str) -> int:
    match = _ANILIST_ID_RE.search(value.strip())
    if not match:
        raise ValueError(err("cli.anilistIdRequired"))
    media_id = int(match.group("id"))
    if media_id < 1:
        raise ValueError(err("cli.anilistIdPositive"))
    return media_id


# An episode that was watched but could not reach AniList (network error, rate
# limit, a 403 outage, mpv closed while the helper ran) waits here and is sent
# with the next AniList list refresh, so AniList never stays an episode behind.
# The watch itself is applied locally at once, and every list refresh lays the
# queued episode over AniList's progress until AniList has it. Queued episodes
# never expire: sending one again is harmless (AniList's progress is read first
# and never lowered).
PENDING_PROGRESS_KEY = "anilist_pending_progress"
# A list status queued longer ago than this is no longer the user's wish: dropped.
PENDING_STATUS_MAX_AGE = 30 * 24 * 3600
# AniList rejecting one entry's request (HTTP 400) is retried at this many refreshes.
PENDING_MAX_REJECTIONS = 5


@contextmanager
def _state_transaction(db: Any) -> Iterator[None]:
    """Run one queue read-modify-write as a single SQLite write transaction.

    The mpv helper, the window and the agent change the same queue from
    different processes; without the lock a flush clearing one entry wrote back
    its older copy and erased an episode queued meanwhile.
    """
    scope = getattr(db, "connection_scope", None)
    if not callable(scope):
        yield
        return
    with scope() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        yield


def _load_blob(db: Any, key: str) -> dict[str, Any]:
    try:
        data = json.loads(db.get_state(key, "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mutate_blob(db: Any, key: str, mutate: Any) -> Any:
    with _state_transaction(db):
        data = _load_blob(db, key)
        before = json.dumps(data, sort_keys=True)
        result = mutate(data)
        if json.dumps(data, sort_keys=True) != before:
            if data:
                db.set_state(key, json.dumps(data, ensure_ascii=False))
            else:
                db.delete_state(key)
    return result


def _pending_entries(db: Any) -> dict[str, dict[str, Any]]:
    return {
        str(key): value
        for key, value in _load_blob(db, PENDING_PROGRESS_KEY).items()
        if isinstance(value, dict)
    }


def queue_pending_progress(
    db: Any,
    *,
    media_id: int,
    episode: int,
    total_episodes: int | None,
    video: str,
    part_one: bool = False,
) -> None:
    item = {
        "media_id": int(media_id),
        "episode": int(episode),
        "total_episodes": int(total_episodes) if total_episodes else None,
        "video": str(video),
        "part_one": bool(part_one),
        "queued_at": time.time(),
    }

    def mutate(data: dict[str, Any]) -> None:
        data[f"{int(media_id)}:{int(episode)}"] = item

    _mutate_blob(db, PENDING_PROGRESS_KEY, mutate)


def clear_pending_progress(db: Any, media_id: int, episode: int) -> None:
    _mutate_blob(db, PENDING_PROGRESS_KEY, lambda data: data.pop(f"{int(media_id)}:{int(episode)}", None))


def pending_progress(db: Any) -> list[dict[str, Any]]:
    return sorted(_pending_entries(db).values(), key=lambda item: (item["media_id"], item["episode"]))


def pending_progress_by_media(db: Any) -> dict[int, int]:
    """The highest queued (watched, not yet on AniList) episode of each title."""
    result: dict[int, int] = {}
    for item in pending_progress(db):
        try:
            media_id, episode = int(item["media_id"]), int(item["episode"])
        except (KeyError, TypeError, ValueError):
            continue
        result[media_id] = max(result.get(media_id, 0), episode)
    return result


# A list status the window set while AniList could not be reached (HTTP 403/429,
# no network): applied locally at once, sent with the next AniList list refresh,
# and kept over the refreshed list until then so the entry does not jump back.
# Each entry remembers the status AniList had when it was queued ("previous"):
# it is sent only while AniList still has that status, so a change made later
# on anilist.co (Completed, deleted) is never overwritten by an old click.
# "REMOVED" queues a deletion from the list.
PENDING_STATUS_KEY = "anilist_pending_status"
QUEUED_REMOVAL = "REMOVED"


def _status_entries(db: Any) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for key, value in _load_blob(db, PENDING_STATUS_KEY).items():
        try:
            if isinstance(value, dict) and str(value.get("status") or ""):
                result[int(key)] = value
        except (TypeError, ValueError):
            continue
    return result


def _status_expired(value: dict[str, Any], now: float | None = None) -> bool:
    try:
        queued_at = float(value.get("queued_at") or 0)
    except (TypeError, ValueError):
        queued_at = 0.0
    return (time.time() if now is None else now) - queued_at > PENDING_STATUS_MAX_AGE


def pending_statuses(db: Any) -> dict[int, str]:
    return {
        media_id: str(value["status"]).upper()
        for media_id, value in _status_entries(db).items()
        if not _status_expired(value)
    }


def _list_status(value: Any) -> str:
    status = str(value or "").strip().upper()
    return "" if status == QUEUED_REMOVAL else status


def queue_pending_status(db: Any, media_id: int, status: str, previous: str | None = None) -> None:
    """Queue ``status`` for the next refresh; ``previous`` is what AniList has now ("" = not listed).

    A second change keeps the first one's ``previous`` (AniList has not seen
    either); an older entry without one (queued by an earlier version) stays
    without, as the local status then no longer tells what AniList has. Going
    back to ``previous`` cancels the queued change.
    """
    target = str(status).upper()

    def mutate(data: dict[str, Any]) -> None:
        key = str(int(media_id))
        old = data.get(key) if isinstance(data.get(key), dict) else {}
        entry: dict[str, Any] = {"status": target, "queued_at": time.time()}
        if old and not _status_expired(old):
            if "previous" in old:
                entry["previous"] = old["previous"]
        elif previous is not None:
            entry["previous"] = _list_status(previous)
        if "previous" in entry and _list_status(target) == entry["previous"]:
            data.pop(key, None)
        else:
            data[key] = entry

    _mutate_blob(db, PENDING_STATUS_KEY, mutate)


def clear_pending_status(db: Any, media_id: int) -> None:
    _mutate_blob(db, PENDING_STATUS_KEY, lambda data: data.pop(str(int(media_id)), None))


def _drop_expired(db: Any, key: str, step: str, logger: Any) -> None:
    def mutate(data: dict[str, Any]) -> list[str]:
        expired = [
            name for name, value in data.items() if not isinstance(value, dict) or _status_expired(value)
        ]
        for name in expired:
            data.pop(name, None)
        return expired

    for name in _mutate_blob(db, key, mutate):
        logger.warning("SKIP step=%s media_id=%s reason=expired", step, name)


def _drop_expired_statuses(db: Any, logger: Any) -> None:
    _drop_expired(db, PENDING_STATUS_KEY, "anilist.pending_status", logger)


def _note_rejection(db: Any, key: str, keys: list[str], logger: Any, step: str, exc: Exception) -> None:
    """Count a 400 for these queue entries; give up on them after PENDING_MAX_REJECTIONS."""

    def mutate(data: dict[str, Any]) -> list[str]:
        dropped = []
        for name in keys:
            value = data.get(name)
            if not isinstance(value, dict):
                continue
            value["rejections"] = int(value.get("rejections") or 0) + 1
            if value["rejections"] >= PENDING_MAX_REJECTIONS:
                data.pop(name, None)
                dropped.append(name)
        return dropped

    dropped = _mutate_blob(db, key, mutate)
    logger.warning(
        "SKIP step=%s keys=%s reason=rejected dropped=%s error=%r",
        step,
        ",".join(keys),
        ",".join(dropped),
        exc,
    )


def flush_pending_statuses(db: Any, client: Any, logger: Any) -> int:
    """Send list statuses set while AniList was unreachable; returns how many were sent.

    An error that concerns AniList as a whole (403, 429, 5xx, no network, a bad
    token) keeps the rest for the next refresh; an error for one entry (gone
    from AniList, a rejected request) does not hold back the others.
    """
    from .providers.anilist import AniListError, entry_error_reason

    _drop_expired_statuses(db, logger)
    read_entry = getattr(client, "list_entry", None)
    sent = 0
    for media_id, value in sorted(_status_entries(db).items()):
        status = str(value.get("status") or "").upper()
        try:
            if "previous" in value and callable(read_entry):
                entry = read_entry(media_id)
                remote = _list_status((entry or {}).get("status"))
                if remote == _list_status(status):
                    clear_pending_status(db, media_id)
                    logger.info(
                        "SKIP step=anilist.pending_status media_id=%s status=%s reason=already",
                        media_id,
                        status,
                    )
                    continue
                if remote != _list_status(value.get("previous")):
                    clear_pending_status(db, media_id)
                    logger.warning(
                        "SKIP step=anilist.pending_status media_id=%s status=%s reason=changed_on_anilist "
                        "queued_over=%s anilist_now=%s",
                        media_id,
                        status,
                        value.get("previous") or "none",
                        remote or "none",
                    )
                    continue
            if status == QUEUED_REMOVAL:
                client.delete_list_entry(media_id)
            else:
                client.set_list_status(media_id, status)
        except AniListError as exc:
            reason = entry_error_reason(exc)
            if reason == "not_found":
                clear_pending_status(db, media_id)
                logger.warning(
                    "SKIP step=anilist.pending_status media_id=%s reason=not_found error=%r", media_id, exc
                )
                continue
            if reason:
                _note_rejection(
                    db, PENDING_STATUS_KEY, [str(media_id)], logger, "anilist.pending_status", exc
                )
                continue
            logger.warning(
                "RETRY step=anilist.pending_status media_id=%s status=%s error=%r", media_id, status, exc
            )
            break
        clear_pending_status(db, media_id)
        sent += 1
        logger.info("DONE step=anilist.pending_status media_id=%s status=%s", media_id, status)
    return sent


# A score the user gave while AniList could not be reached: saved locally at
# once and sent with the next refresh (after the statuses). The last score per
# title wins; it expires like a queued status.
PENDING_SCORE_KEY = "anilist_pending_score"


def queue_pending_score(db: Any, media_id: int, score: float) -> None:
    entry = {"score": float(score), "queued_at": time.time()}
    _mutate_blob(db, PENDING_SCORE_KEY, lambda data: data.__setitem__(str(int(media_id)), entry))


def clear_pending_score(db: Any, media_id: int) -> None:
    _mutate_blob(db, PENDING_SCORE_KEY, lambda data: data.pop(str(int(media_id)), None))


def pending_scores(db: Any) -> dict[int, float]:
    result: dict[int, float] = {}
    for key, value in _load_blob(db, PENDING_SCORE_KEY).items():
        try:
            if isinstance(value, dict) and not _status_expired(value):
                result[int(key)] = float(value["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return result


def flush_pending_scores(db: Any, client: Any, logger: Any) -> int:
    """Send scores given while AniList was unreachable; returns how many were sent."""
    from .providers.anilist import AniListError, entry_error_reason

    _drop_expired(db, PENDING_SCORE_KEY, "anilist.pending_score", logger)
    sent = 0
    for media_id, score in sorted(pending_scores(db).items()):
        try:
            client.set_score(media_id, score)
        except AniListError as exc:
            reason = entry_error_reason(exc)
            if reason == "not_found":
                clear_pending_score(db, media_id)
                logger.warning(
                    "SKIP step=anilist.pending_score media_id=%s reason=not_found error=%r", media_id, exc
                )
                continue
            if reason:
                _note_rejection(db, PENDING_SCORE_KEY, [str(media_id)], logger, "anilist.pending_score", exc)
                continue
            logger.warning(
                "RETRY step=anilist.pending_score media_id=%s score=%s error=%r", media_id, score, exc
            )
            break
        clear_pending_score(db, media_id)
        sent += 1
        logger.info("DONE step=anilist.pending_score media_id=%s score=%s", media_id, score)
    return sent


def progress_update_options(config: Any) -> dict[str, Any]:
    anilist = config.anilist
    return {
        "add_if_missing": anilist.add_if_missing,
        "update_when_rewatching": anilist.update_when_rewatching,
        "completed_to_rewatching_on_episode_one": anilist.completed_to_rewatching_on_episode_one,
        "complete_current_final": anilist.complete_current_final,
        "complete_rewatching_final": anilist.complete_rewatching_final,
    }


def _watched_locally(db: Any, video: Path) -> bool:
    lookup = getattr(db, "episode_by_path", None)
    if not callable(lookup) or not str(video):
        return False
    try:
        row = lookup(video)
    except Exception:  # noqa: BLE001 - only decides whether to schedule again
        return False
    return row is not None and str(getattr(row, "state", "") or "") == "watched"


def _count_locally(db: Any, config: Any, item: dict[str, Any], status: str) -> None:
    """The episode reached AniList: make sure the local side shows it watched too."""
    media_id, episode = int(item["media_id"]), int(item["episode"])
    video = Path(str(item.get("video") or ""))
    if item.get("part_one") or _watched_locally(db, video):
        # Part one of a two-episode file, or already applied when it was queued:
        # do not push its deletion time back.
        db.raise_anime_progress(media_id, episode)
        return
    db.schedule_cleanup(
        video,
        config.agent.delete_after_watched_hours,
        list_status=status,
        media_id=media_id,
        episode=episode,
    )


def _progress_counted(result: dict[str, Any]) -> bool:
    return bool(result.get("updated")) or str(result.get("reason") or "") == "already_at_or_above"


def flush_pending_progress(db: Any, client: Any, config: Any, logger: Any) -> int:
    """Send queued watched episodes to AniList; returns how many were counted.

    Only the highest queued episode of a title is sent (one read and one write
    for a whole offline binge); the lower ones are covered by it. An error that
    concerns AniList as a whole keeps the rest of the queue for the next
    refresh; an error for one title (gone from AniList, a rejected request)
    does not block the titles after it.
    """
    from .providers.anilist import AniListError, entry_error_reason

    counted = 0
    groups: dict[int, list[dict[str, Any]]] = {}
    for item in pending_progress(db):
        groups.setdefault(int(item["media_id"]), []).append(item)

    def send(item: dict[str, Any]) -> dict[str, Any]:
        return client.update_progress(
            int(item["media_id"]),
            int(item["episode"]),
            item.get("total_episodes"),
            **progress_update_options(config),
        )

    def log_result(item: dict[str, Any], result: dict[str, Any], covered: int) -> None:
        logger.info(
            "RESULT step=anilist.pending_progress media_id=%s episode=%s updated=%s reason=%s progress=%s covered=%s",
            item["media_id"],
            item["episode"],
            bool(result.get("updated")),
            str(result.get("reason") or ""),
            result.get("progress"),
            covered,
        )

    for media_id, items in groups.items():
        items.sort(key=lambda item: int(item["episode"]))
        top = items[-1]
        try:
            result = send(top)
            if _progress_counted(result):
                # The newest episode counts for every lower one queued with it.
                for item in items:
                    clear_pending_progress(db, media_id, int(item["episode"]))
                    _count_locally(db, config, item, str(result.get("status") or ""))
                counted += len(items)
                log_result(top, result, len(items))
                continue
            log_result(top, result, 0)
            if len(items) == 1:
                clear_pending_progress(db, media_id, int(top["episode"]))
                continue
            # The newest episode alone was not counted (a rewatch of a Completed
            # title starts at episode one): send them one by one, oldest first.
            for item in items:
                result = send(item)
                clear_pending_progress(db, media_id, int(item["episode"]))
                if _progress_counted(result):
                    counted += 1
                    _count_locally(db, config, item, str(result.get("status") or ""))
                log_result(item, result, 1)
        except AniListError as exc:
            reason = entry_error_reason(exc)
            keys = [f"{media_id}:{int(item['episode'])}" for item in items]
            if reason == "not_found":
                for item in items:
                    clear_pending_progress(db, media_id, int(item["episode"]))
                logger.warning(
                    "SKIP step=anilist.pending_progress media_id=%s episodes=%s reason=not_found error=%r",
                    media_id,
                    ",".join(str(item["episode"]) for item in items),
                    exc,
                )
                continue
            if reason:
                _note_rejection(db, PENDING_PROGRESS_KEY, keys, logger, "anilist.pending_progress", exc)
                continue
            logger.warning(
                "RETRY step=anilist.pending_progress media_id=%s episode=%s error=%r",
                media_id,
                top["episode"],
                exc,
            )
            break
    return counted


def pending_summary(db: Any) -> dict[str, Any]:
    """What waits for AniList, for the window: episodes, titles, statuses."""
    progress = pending_progress(db)
    return {
        "episodes": len(progress),
        "titles": len({int(item["media_id"]) for item in progress}),
        "statuses": len(pending_statuses(db)),
        "scores": len(pending_scores(db)),
    }
