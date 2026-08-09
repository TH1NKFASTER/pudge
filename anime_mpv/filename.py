from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz

from .models import VideoIdentity


BRACKET_RE = re.compile(r"\[[^\]]*\]")
COMMON_PAREN_RE = re.compile(
    r"\((?:[^)]*(?:1080p|720p|2160p|web[- .]?dl|bluray|blu[- .]?ray|bdrip|webrip|x26[45]|hevc|avc|aac|flac|multi(?:sub)?)[^)]*)\)",
    re.IGNORECASE,
)
SEASON_EP_RE = re.compile(r"(?i)(?:^|[\s._-])S(?P<season>\d{1,2})E(?P<episode>\d{1,4})(?:v\d+)?(?:$|[\s._-])")
BRACKET_EP_RE = re.compile(
    r"(?i)(?<!\d)\[(?:E|EP)?\s*0*(?P<episode>\d{1,3})(?:v\d+)?\](?!\d)"
)
EPISODE_LABEL_RE = re.compile(
    r"(?i)(?:^|[\s._-])(?:EP?|Episode)\s*0*(?P<episode>\d{1,4})(?:v\d+)?(?:$|[\s._-])"
)
DASH_EP_RE = re.compile(r"(?i)\s[-–—]\s*0*(?P<episode>\d{1,4})(?:v\d+)?(?=\s|$)")
TRAILING_EP_RE = re.compile(r"(?i)(?:^|\s)0*(?P<episode>\d{1,3})(?:v\d+)?$")
SEASON_LABEL_RE = re.compile(r"(?i)(?:season|saison)\s*(?P<season>\d{1,2})")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

KNOWN_MEDIA_SUFFIXES = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts",
    ".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".idx",
    ".zip", ".7z", ".rar",
}


def _strip_known_suffixes(name: str) -> str:
    value = name
    while Path(value).suffix.casefold() in KNOWN_MEDIA_SUFFIXES:
        value = Path(value).stem
    return value


NOISE_TOKENS = {
    "1080p", "720p", "2160p", "480p", "web", "webdl", "webrip", "bdrip", "bluray", "bd",
    "hevc", "avc", "x264", "x265", "h264", "h265", "aac", "flac", "opus", "multi", "multisub",
    "dual", "audio", "cr", "nf", "netflix", "amazon", "amzn", "atx", "batch", "complete",
}

RELEASE_TAGS = {
    "webdl", "webrip", "bluray", "bdrip", "bd", "cr", "netflix", "nf", "amazon", "amzn",
    "atx", "unext", "bglobal", "hdtv", "1080p", "720p", "2160p",
}


LATIN_SEARCH_FOLD = str.maketrans(
    {
        "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
        "Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
        "Đ": "D", "đ": "d", "Ð": "D", "ð": "d",
        "Þ": "Th", "þ": "th", "ß": "ss", "ẞ": "SS",
        "Ħ": "H", "ħ": "h", "ı": "i", "Ŋ": "N", "ŋ": "n",
    }
)


def fold_search_title(value: str) -> str:
    """Fold decorative Latin Unicode while preserving Japanese text.

    NFKD alone would also split Japanese dakuten into combining marks. We only
    discard a combining mark when it belongs to a Latin base character, so
    ``Caraméliser`` becomes ``Carameliser`` without damaging kana titles.
    """
    decomposed = unicodedata.normalize("NFKD", value.translate(LATIN_SEARCH_FOLD))
    result: list[str] = []
    previous_base_was_latin = False
    for char in decomposed:
        if unicodedata.category(char).startswith("M"):
            if previous_base_was_latin:
                continue
            result.append(char)
            continue
        previous_base_was_latin = "LATIN" in unicodedata.name(char, "")
        result.append(char)
    return unicodedata.normalize("NFC", "".join(result))


def normalize_title(value: str) -> str:
    value = fold_search_title(value)
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", " ", value, flags=re.UNICODE)
    tokens = [token for token in value.split() if token not in NOISE_TOKENS]
    return " ".join(tokens)


def title_similarity(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return float(max(fuzz.WRatio(a, b), fuzz.token_set_ratio(a, b)))


def _clean_display_name(name: str) -> str:
    name = BRACKET_RE.sub(" ", name)
    name = COMMON_PAREN_RE.sub(" ", name)
    name = name.replace("_", " ").replace(".", " ")
    name = re.sub(r"\s+", " ", name).strip(" -–—._")
    return name


def parse_anime_filename(path_or_name: str | Path) -> VideoIdentity:
    source_path = Path(path_or_name)
    raw_name = source_path.name
    stem = _strip_known_suffixes(raw_name)
    bracket_episode_match = BRACKET_EP_RE.search(stem)
    cleaned = _clean_display_name(stem)

    season: int | None = None
    episode: int | None = None
    episode_match: re.Match[str] | None = None

    match = SEASON_EP_RE.search(cleaned)
    if match:
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        episode_match = match
    else:
        for pattern in (EPISODE_LABEL_RE, DASH_EP_RE, TRAILING_EP_RE):
            match = pattern.search(cleaned)
            if match:
                episode = int(match.group("episode"))
                episode_match = match
                break
        if episode is None and bracket_episode_match is not None:
            episode = int(bracket_episode_match.group("episode"))

    season_match = SEASON_LABEL_RE.search(cleaned)
    if season is None and season_match:
        season = int(season_match.group("season"))

    # Files managed by Sonarr/Plex often keep the season only in a parent
    # directory: ``Anime title/Season 02/Episode 05.mkv``. Preserve the exact
    # filename parser as the primary source and use nearby directories only as
    # a fallback.
    season_parent: Path | None = None
    if season is None and source_path.parent != Path("."):
        for parent in list(source_path.parents)[:3]:
            parent_match = SEASON_LABEL_RE.search(parent.name)
            if parent_match:
                season = int(parent_match.group("season"))
                season_parent = parent
                break

    year_match = YEAR_RE.search(cleaned)
    year = int(year_match.group(1)) if year_match else None

    title_part = cleaned
    if episode_match:
        title_part = cleaned[: episode_match.start()]
    title_part = SEASON_EP_RE.sub(" ", title_part)
    title_part = re.sub(r"(?i)\b(?:episode|ep?)\s*$", " ", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(" -–—._")

    # If the filename is only an episode label, take the series title from the
    # directory above ``Season XX``. This mirrors common media-library layouts
    # without overriding informative release names.
    normalized_title = normalize_title(title_part)
    generic_episode_title = normalized_title in {"e", "ep", "episode"} or len(normalized_title) < 2
    if generic_episode_title and season_parent is not None and season_parent.parent.name:
        title_part = _clean_display_name(season_parent.parent.name)
    elif len(normalized_title) < 2:
        title_part = cleaned

    return VideoIdentity(title=title_part, episode=episode, season=season, year=year, raw_name=raw_name)


def release_tokens(name: str) -> set[str]:
    normalized = normalize_title(name).replace(" ", "")
    tokens: set[str] = set()
    aliases = {
        "web-dl": "webdl", "web dl": "webdl", "blu-ray": "bluray", "blu ray": "bluray",
        "u-next": "unext", "b-global": "bglobal",
    }
    lowered = unicodedata.normalize("NFKC", name).casefold()
    for source, target in aliases.items():
        if source in lowered:
            tokens.add(target)
    for tag in RELEASE_TAGS:
        if tag in normalized:
            tokens.add(tag)
    return tokens
