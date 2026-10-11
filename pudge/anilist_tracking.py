from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .i18n import err
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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)
    path.chmod(0o600)


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


def create_tracking_file(cache_dir: Path, payload: TrackingPayload) -> Path:
    run_dir = cache_dir / "anilist" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
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
# limit, mpv closed while the helper ran) waits here and is sent with the next
# AniList list refresh, so AniList never stays an episode behind.
PENDING_PROGRESS_KEY = "anilist_pending_progress"
PENDING_PROGRESS_MAX_AGE = 30 * 24 * 3600


def _pending_entries(db: Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(db.get_state(PENDING_PROGRESS_KEY, "") or "{}")
    except (TypeError, ValueError):
        return {}
    return (
        {str(key): value for key, value in data.items() if isinstance(value, dict)}
        if isinstance(data, dict)
        else {}
    )


def _store_pending(db: Any, entries: dict[str, dict[str, Any]]) -> None:
    if entries:
        db.set_state(PENDING_PROGRESS_KEY, json.dumps(entries, ensure_ascii=False))
    else:
        db.delete_state(PENDING_PROGRESS_KEY)


def queue_pending_progress(
    db: Any,
    *,
    media_id: int,
    episode: int,
    total_episodes: int | None,
    video: str,
    part_one: bool = False,
) -> None:
    entries = _pending_entries(db)
    entries[f"{int(media_id)}:{int(episode)}"] = {
        "media_id": int(media_id),
        "episode": int(episode),
        "total_episodes": int(total_episodes) if total_episodes else None,
        "video": str(video),
        "part_one": bool(part_one),
        "queued_at": time.time(),
    }
    _store_pending(db, entries)


def clear_pending_progress(db: Any, media_id: int, episode: int) -> None:
    entries = _pending_entries(db)
    if entries.pop(f"{int(media_id)}:{int(episode)}", None) is not None:
        _store_pending(db, entries)


def pending_progress(db: Any) -> list[dict[str, Any]]:
    return sorted(_pending_entries(db).values(), key=lambda item: (item["media_id"], item["episode"]))


def progress_update_options(config: Any) -> dict[str, Any]:
    anilist = config.anilist
    return {
        "add_if_missing": anilist.add_if_missing,
        "update_when_rewatching": anilist.update_when_rewatching,
        "completed_to_rewatching_on_episode_one": anilist.completed_to_rewatching_on_episode_one,
        "complete_current_final": anilist.complete_current_final,
        "complete_rewatching_final": anilist.complete_rewatching_final,
    }


def flush_pending_progress(db: Any, client: Any, config: Any, logger: Any) -> int:
    """Send queued watched episodes to AniList; returns how many were counted.

    An AniList error keeps the rest of the queue for the next refresh.
    """
    from .providers.anilist import AniListError

    counted = 0
    now = time.time()
    for item in pending_progress(db):
        media_id, episode = int(item["media_id"]), int(item["episode"])
        if now - float(item.get("queued_at") or now) > PENDING_PROGRESS_MAX_AGE:
            clear_pending_progress(db, media_id, episode)
            logger.info(
                "SKIP step=anilist.pending_progress media_id=%s episode=%s reason=expired", media_id, episode
            )
            continue
        try:
            result = client.update_progress(
                media_id, episode, item.get("total_episodes"), **progress_update_options(config)
            )
        except AniListError as exc:
            logger.warning(
                "RETRY step=anilist.pending_progress media_id=%s episode=%s error=%r", media_id, episode, exc
            )
            break
        reason = str(result.get("reason") or "")
        clear_pending_progress(db, media_id, episode)
        if result.get("updated") or reason == "already_at_or_above":
            counted += 1
            if item.get("part_one"):
                db.raise_anime_progress(media_id, episode)
            else:
                db.schedule_cleanup(
                    Path(str(item.get("video") or "")),
                    config.agent.delete_after_watched_hours,
                    list_status=str(result.get("status") or ""),
                    media_id=media_id,
                    episode=episode,
                )
        logger.info(
            "RESULT step=anilist.pending_progress media_id=%s episode=%s updated=%s reason=%s progress=%s",
            media_id,
            episode,
            bool(result.get("updated")),
            reason,
            result.get("progress"),
        )
    return counted
