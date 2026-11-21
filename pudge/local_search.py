from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz

from .archive_limits import (
    IMAGE_MEMBER_LIMIT,
    SUBTITLE_ARCHIVE_LIMIT,
    TEXT_MEMBER_LIMIT,
    ArchiveBudget,
    ArchiveTooLargeError,
    extract_member,
)
from .filename import normalize_title, parse_anime_filename, release_tokens, title_similarity
from .language import (
    IMAGE_SUBTITLE_EXTENSIONS,
    TEXT_SUBTITLE_EXTENSIONS,
    has_japanese_marker,
    is_japanese_subtitle,
    is_macos_metadata_path,
)
from .models import SubtitleCandidate, VideoIdentity
from .subtitle_formats import format_preference_bonus


SUBTITLE_EXTENSIONS = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS
ARCHIVE_EXTENSIONS = {".zip"}

# Romaji particles: "Kimi no Na wa - 03" scored like "Kimetsu no Yaiba - 03" through
# "no" and "wa" alone (WEBFLOW-10).
_TITLE_PARTICLES = frozenset({"no", "wa", "ga", "to", "ni", "wo", "de", "mo", "e"})
# A file outside the video's folder (a shared subtitle folder such as a Kitsunekko
# dump) must name this title: the episode number alone matches every show.
OUTSIDE_FOLDER_MIN_SIMILARITY = 70.0


def _core_title(title: str) -> str:
    normalized = normalize_title(title)
    words = [word for word in normalized.split() if word not in _TITLE_PARTICLES]
    return " ".join(words) or normalized


def _compact_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", normalize_title(title))


def _core_title_similarity(left: str, right: str) -> float:
    # Whole words only: WRatio's partial matching let "Ore dake Level Up na Ken" match
    # "Kimi no Na wa" through a two-letter token (85.5).
    words = float(fuzz.token_set_ratio(_core_title(left), _core_title(right)))
    # A title written as one word ("OshiNoKo", "Kaguyasama wa ...") has other word
    # boundaries: compare it without spaces too, as a whole or as the start of the
    # longer title, so a mid-word fragment never counts.
    a, b = _compact_title(left), _compact_title(right)
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 6:
        if fuzz.ratio(a, b) >= 90:
            return max(words, float(fuzz.ratio(a, b)))
        if len(shorter) >= 8 and longer.startswith(shorter):
            return max(words, 90.0)
    return words


def _candidate_score(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    verified_japanese: bool,
    display_name: str | None = None,
    prefer_srt: bool = True,
    titles: tuple[str, ...] = (),
) -> tuple[float, int | None]:
    """``titles``: the title's other names (AniList romaji, English, native, synonyms).
    A subtitle outside the video's folder may be named by any of them."""
    name = display_name or path.name
    parsed = parse_anime_filename(name)
    similarity = title_similarity(identity.title, parsed.title)
    score = similarity * 0.58
    score += format_preference_bonus(name, prefer_srt)

    if identity.episode is not None:
        if parsed.episode == identity.episode:
            score += 46
        elif parsed.episode is None:
            score += 4
        else:
            score -= 90

    same_directory = False
    try:
        same_directory = path.parent.resolve() == video.parent.resolve()
    except OSError:
        same_directory = False
    if same_directory:
        score += 22
        parsed_latin_words = re.findall(r"[A-Za-z]{3,}", parsed.title)
        identity_latin_words = re.findall(r"[A-Za-z]{3,}", identity.title)
        if similarity < 55 and len(parsed_latin_words) >= 2 and len(identity_latin_words) >= 2:
            # A fully named Latin subtitle from another show can end up next to
            # the video after manual downloads or old experiments. Explicit
            # markers such as .ja[cc] verify the language, not the anime title.
            # Do not apply this gate to Japanese-vs-English alternate titles.
            score -= 120
    elif (
        max([similarity, *(title_similarity(title, parsed.title) for title in titles if title)]) < 45
        or max(
            (_core_title_similarity(title, parsed.title) for title in (identity.title, *titles) if title),
            default=0.0,
        )
        < OUTSIDE_FOLDER_MIN_SIMILARITY
    ):
        # An exact episode number alone is not enough: otherwise every globally
        # scanned ``... - 05.srt`` becomes a candidate for every episode 5 and
        # triggers an expensive audio comparison. Keep loosely named sidecars in
        # the video's own directory, but reject unrelated library-wide files
        # (compared without romaji particles).
        score -= 120

    if verified_japanese:
        score += 30
    elif has_japanese_marker(name):
        score += 14

    overlap = release_tokens(video.name) & release_tokens(name)
    score += min(16, len(overlap) * 4)

    lowered = name.casefold()
    if any(token in lowered for token in ("signs", "songs", "karaoke", "forced")):
        score -= 35

    return score, parsed.episode


def _iter_files(roots: Iterable[Path], max_files: int) -> Iterable[Path]:
    seen: set[Path] = set()
    count = 0
    for root in roots:
        root = root.expanduser()
        if not root.exists() or not root.is_dir():
            continue
        try:
            iterator = root.rglob("*")
            for path in iterator:
                if count >= max_files:
                    return
                # Hidden parts below the root only: a subtitle folder inside ~/.config
                # (or any dot folder) yielded nothing (WEBFLOW-15).
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    relative = path
                if (
                    not path.is_file()
                    or any(part.startswith(".") for part in relative.parts)
                    or is_macos_metadata_path(str(relative))
                ):
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                count += 1
                if path.suffix.casefold() in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS:
                    yield path
        except (OSError, PermissionError):
            continue


def _extract_best_from_zip(
    archive: Path,
    identity: VideoIdentity,
    video: Path,
    cache_dir: Path,
    prefer_srt: bool,
    min_score: float | None = None,
    titles: tuple[str, ...] = (),
) -> SubtitleCandidate | None:
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [
                name
                for name in zf.namelist()
                if not name.endswith("/")
                and Path(name).suffix.casefold() in SUBTITLE_EXTENSIONS
                and not is_macos_metadata_path(name)
            ]
            ranked: list[tuple[float, str, int | None]] = []
            for name in names:
                pseudo = Path(name)
                marker = has_japanese_marker(name)
                score, episode = _candidate_score(
                    pseudo, identity, video, marker, display_name=name, prefer_srt=prefer_srt, titles=titles
                )
                ranked.append((score, name, episode))
            ranked.sort(reverse=True, key=lambda row: row[0])

            # A tiny zip can unpack to gigabytes: members are copied with the subtitle caps.
            budget = ArchiveBudget(SUBTITLE_ARCHIVE_LIMIT)
            for raw_score, name, episode in ranked[:8]:
                digest = hashlib.sha1(f"{archive.resolve()}::{name}".encode()).hexdigest()[:16]
                output_dir = cache_dir / "local-archives" / digest
                if min_score is not None and _best_possible_score(
                    (Path(name), output_dir / Path(name).name), identity, video, name, prefer_srt, titles
                ) < float(min_score):
                    continue  # its name cannot pass, whatever it holds: not unpacked
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / Path(name).name
                if not output.exists():
                    suffix = Path(name).suffix.casefold()
                    image = suffix in IMAGE_SUBTITLE_EXTENSIONS or suffix == ".sub"
                    try:
                        extract_member(
                            zf,
                            name,
                            output,
                            limit=IMAGE_MEMBER_LIMIT if image else TEXT_MEMBER_LIMIT,
                            budget=budget,
                        )
                    except ArchiveTooLargeError:
                        continue
                verified = (
                    is_japanese_subtitle(output)
                    if output.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS
                    else has_japanese_marker(name)
                )
                score, episode = _candidate_score(
                    output, identity, video, verified, display_name=name, prefer_srt=prefer_srt, titles=titles
                )
                if verified or output.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS:
                    return SubtitleCandidate(
                        path=output,
                        source="local-zip",
                        score=max(score, raw_score),
                        name=name,
                        episode=episode,
                        verified_japanese=verified,
                        details={"archive": str(archive)},
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return None
    return None


def _best_possible_score(
    paths: Iterable[Path],
    identity: VideoIdentity,
    video: Path,
    name: str,
    prefer_srt: bool,
    titles: tuple[str, ...] = (),
) -> float:
    """The highest score this name can get (as if its text were verified Japanese)."""
    return max(
        _candidate_score(
            path, identity, video, True, display_name=name, prefer_srt=prefer_srt, titles=titles
        )[0]
        for path in paths
    )


def find_local_subtitles(
    video: Path,
    identity: VideoIdentity,
    subtitle_dirs: list[Path],
    cache_dir: Path,
    max_files: int,
    prefer_srt: bool = True,
    verbose: bool = False,
    min_score: float | None = None,
    titles: Iterable[str] = (),
) -> list[SubtitleCandidate]:
    """Local subtitle candidates, best first.

    ``min_score`` (the caller's acceptance threshold): files whose name cannot reach it
    are not read and archive members not unpacked. Every job used to read every
    subtitle of a shared folder (3,320 reads, 2-5 s on 3,000 files) only to drop them.
    """
    roots = [video.parent, *subtitle_dirs]
    candidates: list[SubtitleCandidate] = []
    # The title's AniList names: a shared folder may name a file by any of them.
    names = tuple(dict.fromkeys(str(title).strip() for title in titles or () if str(title or "").strip()))

    for path in _iter_files(roots, max_files):
        suffix = path.suffix.casefold()
        if suffix in ARCHIVE_EXTENSIONS:
            archive_candidate = _extract_best_from_zip(
                path, identity, video, cache_dir, prefer_srt, min_score=min_score, titles=names
            )
            if archive_candidate:
                candidates.append(archive_candidate)
            continue
        if min_score is not None and _best_possible_score(
            (path,), identity, video, path.name, prefer_srt, names
        ) < float(min_score):
            continue

        if suffix in TEXT_SUBTITLE_EXTENSIONS:
            verified = is_japanese_subtitle(path)
            if not verified:
                continue
        else:
            verified = has_japanese_marker(path.name)
            if not verified:
                continue

        score, episode = _candidate_score(
            path, identity, video, verified, prefer_srt=prefer_srt, titles=names
        )
        candidates.append(
            SubtitleCandidate(
                path=path,
                source="local",
                score=score,
                name=path.name,
                episode=episode,
                verified_japanese=verified,
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    if verbose:
        for item in candidates[:10]:
            print(f"  local {item.score:6.1f} {item.path}")
    return candidates
