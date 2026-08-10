from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
        raise ValueError("Нужен AniList ID или ссылка вида https://anilist.co/anime/12345")
    media_id = int(match.group("id"))
    if media_id < 1:
        raise ValueError("AniList ID должен быть положительным")
    return media_id
