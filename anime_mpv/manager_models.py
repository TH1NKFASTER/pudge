from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any


@dataclass(slots=True)
class LibraryAnime:
    media_id: int
    title: str
    titles: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    cover_url: str = ""
    site_url: str = ""
    status: str = ""
    progress: int = 0
    episodes: int | None = None
    format: str | None = None
    season_year: int | None = None
    start_date: str | None = None
    studio: str = ""
    media_status: str | None = None
    end_date: str | None = None
    mean_score: int | None = None
    user_score: float | None = None
    duration: int | None = None
    next_airing_episode: int | None = None
    next_airing_at: int | None = None
    relations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def next_episode(self) -> int:
        return max(1, self.progress + 1)

    @property
    def released_episodes(self) -> int | None:
        if self.next_airing_episode:
            released = self.next_airing_episode - 1
            # AniList's cached nextAiringEpisode remains useful even before the
            # next metadata refresh. As soon as its airing timestamp passes, the
            # episode is considered released and can move to Waiting for
            # preparation / enter the 10-minute Nyaa retry loop.
            if self.next_airing_at and self.next_airing_at <= int(time.time()):
                released = self.next_airing_episode
            return max(0, released)
        return self.episodes


@dataclass(slots=True)
class NyaaRelease:
    title: str
    link: str
    torrent_url: str
    info_hash: str
    size_text: str
    size_bytes: int
    seeders: int
    leechers: int
    downloads: int
    trusted: bool
    remake: bool
    category_id: str = ""
    published: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    is_batch: bool = False
    group: str = ""

    @property
    def magnet(self) -> str:
        if not self.info_hash:
            return self.torrent_url
        from urllib.parse import quote

        return f"magnet:?xt=urn:btih:{self.info_hash}&dn={quote(self.title)}"


@dataclass(slots=True)
class LibraryEpisode:
    media_id: int | None
    title: str
    episode: int | None
    video_path: Path
    subtitle_path: Path | None = None
    embedded_subtitle_id: int | None = None
    state: str = "local"
    torrent_hash: str = ""
    watched_at: float | None = None
    delete_after: float | None = None
    playback_position: float | None = None
    playback_duration: float | None = None
    playback_updated_at: float | None = None
    playback_active_seconds: float = 0.0
    subtitle_origin: str = ""


@dataclass(slots=True)
class DownloadItem:
    torrent_hash: str
    name: str
    state: str
    progress: float
    save_path: str
    content_path: str
    media_id: int | None = None
    episode: int | None = None
    is_batch: bool = False
    added_on: int = 0
    completed_on: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
