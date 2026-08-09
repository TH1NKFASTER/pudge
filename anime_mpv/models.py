from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VideoIdentity:
    title: str
    episode: int | None = None
    season: int | None = None
    year: int | None = None
    raw_name: str = ""


@dataclass(slots=True)
class EmbeddedSubtitle:
    stream_index: int
    subtitle_id: int
    codec: str
    language: str = ""
    title: str = ""
    score: float = 0.0
    detected_from_text: bool = False


@dataclass(slots=True)
class SubtitleCandidate:
    path: Path
    source: str
    score: float
    name: str
    episode: int | None = None
    verified_japanese: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AniListAnime:
    id: int
    titles: list[str]
    synonyms: list[str]
    season_year: int | None
    episodes: int | None
    format: str | None
    score: float = 0.0


@dataclass(slots=True)
class JimakuEntry:
    id: int
    name: str
    english_name: str | None
    japanese_name: str | None
    anilist_id: int | None
    flags: dict[str, Any]


@dataclass(slots=True)
class JimakuFile:
    url: str
    name: str
    size: int
    last_modified: str
    score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
