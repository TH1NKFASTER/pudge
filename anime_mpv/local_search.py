from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Iterable

from .filename import parse_anime_filename, release_tokens, title_similarity
from .language import (
    IMAGE_SUBTITLE_EXTENSIONS,
    TEXT_SUBTITLE_EXTENSIONS,
    has_japanese_marker,
    is_japanese_subtitle,
)
from .models import SubtitleCandidate, VideoIdentity
from .subtitle_formats import format_preference_bonus


SUBTITLE_EXTENSIONS = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS
ARCHIVE_EXTENSIONS = {".zip"}


def _candidate_score(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    verified_japanese: bool,
    display_name: str | None = None,
    prefer_srt: bool = True,
) -> tuple[float, int | None]:
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
    elif similarity < 45:
        # An exact episode number alone is not enough: otherwise every globally
        # scanned ``... - 05.srt`` becomes a candidate for every episode 5 and
        # triggers an expensive audio comparison. Keep loosely named sidecars in
        # the video's own directory, but reject unrelated library-wide files.
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
                if not path.is_file() or any(part.startswith(".") for part in path.parts):
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
) -> SubtitleCandidate | None:
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [
                name for name in zf.namelist()
                if not name.endswith("/") and Path(name).suffix.casefold() in SUBTITLE_EXTENSIONS
            ]
            ranked: list[tuple[float, str, int | None]] = []
            for name in names:
                pseudo = Path(name)
                marker = has_japanese_marker(name)
                score, episode = _candidate_score(
                    pseudo, identity, video, marker, display_name=name, prefer_srt=prefer_srt
                )
                ranked.append((score, name, episode))
            ranked.sort(reverse=True, key=lambda row: row[0])

            for raw_score, name, episode in ranked[:8]:
                digest = hashlib.sha1(f"{archive.resolve()}::{name}".encode()).hexdigest()[:16]
                output_dir = cache_dir / "local-archives" / digest
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / Path(name).name
                if not output.exists():
                    with zf.open(name) as src, output.open("wb") as dst:
                        dst.write(src.read())
                verified = is_japanese_subtitle(output) if output.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS else has_japanese_marker(name)
                score, episode = _candidate_score(
                    output, identity, video, verified, display_name=name, prefer_srt=prefer_srt
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
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    return None


def find_local_subtitles(
    video: Path,
    identity: VideoIdentity,
    subtitle_dirs: list[Path],
    cache_dir: Path,
    max_files: int,
    prefer_srt: bool = True,
    verbose: bool = False,
) -> list[SubtitleCandidate]:
    roots = [video.parent, *subtitle_dirs]
    candidates: list[SubtitleCandidate] = []

    for path in _iter_files(roots, max_files):
        suffix = path.suffix.casefold()
        if suffix in ARCHIVE_EXTENSIONS:
            archive_candidate = _extract_best_from_zip(path, identity, video, cache_dir, prefer_srt)
            if archive_candidate:
                candidates.append(archive_candidate)
            continue

        if suffix in TEXT_SUBTITLE_EXTENSIONS:
            verified = is_japanese_subtitle(path)
            if not verified:
                continue
        else:
            verified = has_japanese_marker(path.name)
            if not verified:
                continue

        score, episode = _candidate_score(path, identity, video, verified, prefer_srt=prefer_srt)
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
