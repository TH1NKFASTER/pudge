from __future__ import annotations

import contextvars
import functools
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from rapidfuzz import fuzz

from .models import VideoIdentity
from .release_parser import parse_release_name


BRACKET_RE = re.compile(r"\[[^\]]*\]")
COMMON_PAREN_RE = re.compile(
    r"\((?:[^)]*(?:1080p|720p|2160p|web[- .]?dl|bluray|blu[- .]?ray|bdrip|webrip|x26[45]|hevc|avc|aac|flac|multi(?:sub)?)[^)]*)\)",
    re.IGNORECASE,
)
SEASON_EP_RE = re.compile(r"(?i)(?:^|[\s._-])S(?P<season>\d{1,2})E(?P<episode>\d{1,4})(?:v\d+)?(?:$|[\s._-])")
BRACKET_EP_RE = re.compile(r"(?i)(?<!\d)\[(?:E|EP)?\s*0*(?P<episode>\d{1,3})(?:v\d+)?\](?!\d)")
EPISODE_LABEL_RE = re.compile(
    r"(?i)(?:^|[\s._-])(?:EP?|Episode)\s*0*(?P<episode>\d{1,4})(?:v\d+)?(?:$|[\s._-])"
)
DASH_EP_RE = re.compile(r"(?i)\s[-–—]\s*0*(?P<episode>\d{1,4})(?:v\d+)?(?=\s|$)")
# "Roshidere-08-JPN v1": a dash-joined number right before a language or version tag.
# The dash must follow a title character (not "Kaiju No 8-JPN"), at most three digits
# and never 0, so years, resolutions and title numbers stay out.
TAGGED_DASH_EP_RE = re.compile(
    # Two digits or more ("-08-"): a single unpadded digit glued with dashes is the title's own
    # number ("Kaiju-No-8-JPN", "Danganronpa-3-JPN"), not an episode.
    r"(?i)(?<=[^\s\d\-])-0*(?P<episode>[1-9]\d{0,2})(?<=\d\d)(?:v\d+)?-"
    r"(?=(?:jpn?|jap|ja|japanese|eng?|english|chs|cht|zh|sc|tc|v\d+)(?:$|[\s._-]))"
)
TRAILING_EP_RE = re.compile(r"(?i)(?:^|\s)0*(?P<episode>\d{1,3})(?:v\d+)?$")
ARCHIVE_BDSUP_EP_RE = re.compile(r"(?i)(?:^|\s)0*(?P<episode>\d{1,3})(?:v\d+)?\s+(?:BD\s*SUP|BDSUP)$")
SEASON_LABEL_RE = re.compile(r"(?i)(?:season|saison)\s*(?P<season>\d{1,2})")
# "2nd Season 05": the ordinal names the season; the number after "Season" is the episode.
ORDINAL_SEASON_RE = re.compile(r"(?i)\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+season\b")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

KNOWN_MEDIA_SUFFIXES = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".webm",
    ".ts",
    ".m2ts",
    ".srt",
    ".ass",
    ".ssa",
    ".vtt",
    ".sup",
    ".sub",
    ".idx",
    ".zip",
    ".7z",
    ".rar",
}


_SUBTITLE_CONTAINER_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".idx", ".zip", ".7z", ".rar"}
# Language tag between the name and a subtitle extension: "Title 05.ja.srt".
_SUBTITLE_LANGUAGE_SUFFIXES = {
    ".ja",
    ".jp",
    ".jpn",
    ".jap",
    ".japanese",
    ".en",
    ".eng",
    ".english",
    ".zh",
    ".chi",
    ".chs",
    ".cht",
    ".zho",
}


def _strip_known_suffixes(name: str) -> str:
    value = name
    subtitle = False
    while True:
        suffix = Path(value).suffix.casefold()
        if suffix in KNOWN_MEDIA_SUFFIXES:
            subtitle = subtitle or suffix in _SUBTITLE_CONTAINER_SUFFIXES
        elif not (subtitle and suffix in _SUBTITLE_LANGUAGE_SUFFIXES):
            return value
        value = Path(value).stem


NOISE_TOKENS = {
    "1080p",
    "720p",
    "2160p",
    "480p",
    "web",
    "webdl",
    "webrip",
    "bdrip",
    "bluray",
    "bd",
    "hevc",
    "avc",
    "x264",
    "x265",
    "h264",
    "h265",
    "aac",
    "flac",
    "opus",
    "multi",
    "multisub",
    "dual",
    "audio",
    "cr",
    "nf",
    "netflix",
    "amazon",
    "amzn",
    "atx",
    "batch",
    "complete",
}

RELEASE_TAGS = {
    "webdl",
    "webrip",
    "bluray",
    "bdrip",
    "bd",
    "cr",
    "netflix",
    "nf",
    "amazon",
    "amzn",
    "atx",
    "unext",
    "bglobal",
    "hdtv",
    "1080p",
    "720p",
    "2160p",
}


LATIN_SEARCH_FOLD = str.maketrans(
    {
        "Æ": "AE",
        "æ": "ae",
        "Œ": "OE",
        "œ": "oe",
        "Ø": "O",
        "ø": "o",
        "Ł": "L",
        "ł": "l",
        "Đ": "D",
        "đ": "d",
        "Ð": "D",
        "ð": "d",
        "Þ": "Th",
        "þ": "th",
        "ß": "ss",
        "ẞ": "SS",
        "Ħ": "H",
        "ħ": "h",
        "ı": "i",
        "Ŋ": "N",
        "ŋ": "n",
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


# Inside one release search (title_memo): each distinct title is normalised once. Scoring
# compares every candidate with every alias, and each comparison normalised both again.
_TITLE_MEMO: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "pudge_normalize_title_memo", default=None
)
_TITLE_MEMO_MAX = 50_000
_F = TypeVar("_F", bound=Callable[..., Any])


@contextmanager
def title_memo() -> Iterator[None]:
    """Remember normalize_title answers until the block ends (nested blocks share one)."""
    if _TITLE_MEMO.get() is not None:
        yield
        return
    token = _TITLE_MEMO.set({})
    try:
        yield
    finally:
        _TITLE_MEMO.reset(token)


def with_title_memo(function: _F) -> _F:
    """Run ``function`` inside title_memo()."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with title_memo():
            return function(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def normalize_title(value: str) -> str:
    memo = _TITLE_MEMO.get()
    if memo is None or not isinstance(value, str):
        return _normalize_title(value)
    cached = memo.get(value)
    if cached is None:
        cached = _normalize_title(value)
        if len(memo) < _TITLE_MEMO_MAX:
            memo[value] = cached
    return cached


def _normalize_title(value: str) -> str:
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
        patterns = [EPISODE_LABEL_RE, DASH_EP_RE, TAGGED_DASH_EP_RE, TRAILING_EP_RE]
        # Jimaku commonly stores bitmap subtitle packs as archives named like
        # ``Rakudai Kishi no Cavalry 02 BDSUP.7z``.  The bare number is an
        # episode only in this narrow archive convention; treating arbitrary
        # trailing release numbers this way would confuse years/resolutions.
        if source_path.suffix.casefold() in {".zip", ".7z", ".rar"}:
            patterns.insert(0, ARCHIVE_BDSUP_EP_RE)
        for pattern in patterns:
            match = pattern.search(cleaned)
            if match:
                episode = int(match.group("episode"))
                episode_match = match
                break
        if episode is None and bracket_episode_match is not None:
            episode = int(bracket_episode_match.group("episode"))

    ordinal_match = ORDINAL_SEASON_RE.search(cleaned)
    if season is None and ordinal_match:
        season = int(ordinal_match.group("season"))
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

    # Anitomy cross-check (see pudge/release_parser.py). The historical parser
    # above stays primary for regular names; the adapter only removes unsafe
    # single-episode reads and fills what the old parser could not see.
    parsed = parse_release_name(raw_name)
    if parsed.unsafe_single_episode:
        episode = None  # 12.5, 01 ~ 25, NCOP1, SP1: never a regular episode
    elif episode is None and parsed.episode is not None and parsed.explicit_episode:
        episode = parsed.episode
    elif (
        episode is not None
        and episode_match is not None
        and episode_match.re is DASH_EP_RE
        and 1950 <= episode <= 2099
        and parsed.parser_source == "anitomy"
        and not parsed.raw_episodes
    ):
        # "Show - 2024 [1080p]" is a year (Anitomy agrees); "One Piece - 1071" stays an episode.
        episode = None
    if season is None and parsed.season is not None and season_parent is None:
        season = parsed.season

    return VideoIdentity(title=title_part, episode=episode, season=season, year=year, raw_name=raw_name)


def release_tokens(name: str) -> set[str]:
    normalized = normalize_title(name).replace(" ", "")
    tokens: set[str] = set()
    aliases = {
        "web-dl": "webdl",
        "web dl": "webdl",
        "blu-ray": "bluray",
        "blu ray": "bluray",
        "u-next": "unext",
        "b-global": "bglobal",
    }
    lowered = unicodedata.normalize("NFKC", name).casefold()
    for source, target in aliases.items():
        if source in lowered:
            tokens.add(target)
    for tag in RELEASE_TAGS:
        if tag in normalized:
            tokens.add(tag)
    return tokens


# What only a release or file name carries, never an anime title: a container
# extension, a resolution, a codec or source tag, a [Batch] tag, a CRC32 hash,
# an SxxEyy label or a dotted scene name.
_RELEASE_NAME_RE = re.compile(
    r"(?ix)"
    r"\.(?:mkv|mp4|m4v|avi|mov|webm|ts|m2ts|wmv|flv)\s*$"
    r"|(?<![a-z0-9])(?:360|480|540|576|720|1080|1440|2160)[pi](?![a-z0-9])"
    r"|(?<![a-z0-9])(?:x26[45]|h\.?26[45]|hevc|avc|web[- .]?dl|webrip|bdrip|blu[- .]?ray|10[- ]?bit)(?![a-z0-9])"
    r"|[\[(]\s*batch\s*[\])]"
    r"|\[[0-9a-f]{8}\]"
    r"|(?<![a-z0-9])s\d{1,2}e\d{1,4}(?![0-9])"
)
_EPISODE_RANGE_PAREN_RE = re.compile(r"\(\s*\d{1,4}\s*[-~–]\s*\d{1,4}\s*\)")


def looks_like_release_name(value: str) -> bool:
    """A torrent, release or file name rather than an anime title.

    "[HorribleSubs] Youjo Senki - 03 [1080p].mkv" or "Solo.Leveling.S01E11.1080p...":
    yes; "[Oshi no Ko]" or "Re:Zero kara Hajimeru Isekai Seikatsu": no.
    """
    text = str(value or "").strip()
    return bool(text) and _RELEASE_NAME_RE.search(text) is not None


def release_name_display_title(value: str, media_id: int | None = None) -> str:
    """The title a placeholder row shows: never the raw release or file name.

    A name that is no release name is kept as it is. A release name becomes the
    series title parsed from it ("Youjo Senki"); ``AniList <id>`` when nothing
    readable is left.
    """
    text = str(value or "").strip()
    fallback = f"AniList {int(media_id)}" if media_id is not None else ""
    if not text:
        return fallback
    if not looks_like_release_name(text):
        return text
    try:
        title = parse_anime_filename(text).title
    except Exception:  # noqa: BLE001 - a display title never fails a write
        title = ""
    title = _EPISODE_RANGE_PAREN_RE.sub(" ", str(title or ""))
    title = re.sub(r"\s+", " ", title).strip(" -–—._")
    if len(normalize_title(title)) < 2 or looks_like_release_name(title):
        return fallback or text
    return title
