from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..subtitle_formats import parse_srt, plain_subtitle_text, write_srt

_REPAIR_VERSION = "known-source-repair-v2"

# This upstream TVA subtitle is structurally incomplete for the WEB episode: it
# starts after two spoken lines that are present in the WEB cut, and its first
# lines sit on the TV cut's clock.  Keep this as data, not as a global timing
# heuristic.  The timestamps are taken from the already-selected embedded WEB
# English timing reference after validating its opening dialogue shape.
#
# The repository holds no subtitle text: the known source sentences are
# recognised by the SHA-256 of their normalized text (``_sentence_key``) and the
# repaired opening is built from the user's own file, each sentence moved to the
# reference cue it is spoken in.  The two lines the TVA file never had cannot be
# restored from it, so those reference cues stay without Japanese text.
_BLEACH_E45_MEDIA_ID = 185874
_BLEACH_E45_EPISODE = 5
_BLEACH_E45_NAME_TOKEN = "nanakoraws] bleach sennen kessen-hen s01e45"
# sha256(sentence key) -> index of the opening reference cue the sentence is spoken in.
_BLEACH_E45_SOURCE_SENTENCES = {
    "681bb6f3464b9c1cb551bbec70ccdc2df0264ad50d4bddf7f7a2454833267211": 1,
    "a5e7a7e7bc8baba204202a57b387451cd665e1e1d371f34b0ca1e4509038b45c": 2,
    "1f989bc9be7d3d5d65b1986f555d449575c28b571f4879f48b7df9836ccdaeb6": 3,
}
# Both of these must be in the source's opening before anything is changed.
_BLEACH_E45_REQUIRED_SENTENCES = (
    "681bb6f3464b9c1cb551bbec70ccdc2df0264ad50d4bddf7f7a2454833267211",
    "1f989bc9be7d3d5d65b1986f555d449575c28b571f4879f48b7df9836ccdaeb6",
)
_OPENING_CUTOFF_SECONDS = 60.0
# A sentence ends after these marks (continuation arrows included); the mark stays with it.
_SENTENCE_END_RE = re.compile(r"(?<=[。．！？!?…➡➨])(?![。．！？!?…➡➨」』）)])")


def _sentence_key(text: str) -> str:
    """Text without width/case differences, spaces, punctuation and symbols."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] not in "PSZC")


def _sentence_hash(text: str) -> str:
    return hashlib.sha256(_sentence_key(text).encode("utf-8")).hexdigest()


def _opening_sentences(cues: list[tuple[float, float, str]]) -> list[str]:
    sentences: list[str] = []
    for start, _end, text in cues:
        if start >= _OPENING_CUTOFF_SECONDS:
            continue
        for line in plain_subtitle_text(text).splitlines():
            sentences.extend(part.strip() for part in _SENTENCE_END_RE.split(line) if _sentence_key(part))
    return sentences


def _matches_bleach_e45(*, media_id: int | None, episode: int | None, candidate_name: str) -> bool:
    return (
        media_id == _BLEACH_E45_MEDIA_ID
        and episode == _BLEACH_E45_EPISODE
        and _BLEACH_E45_NAME_TOKEN in candidate_name.casefold()
    )


def _bleach_reference_opening(reference: Path) -> list[tuple[float, float, str]] | None:
    try:
        cues = parse_srt(reference)
    except (OSError, ValueError):
        return None
    speech = [cue for cue in cues if cue[0] < 40.0 and plain_subtitle_text(cue[2]).strip()]
    if len(speech) < 4:
        return None
    first = speech[:4]
    texts = [plain_subtitle_text(cue[2]).casefold() for cue in first]
    # Validate the shape without depending on exact fansub wording: the technique
    # names in the first cue, one key word in the second, and the cue times.
    if "getsuga" not in texts[0] or "gran rey" not in texts[0]:
        return None
    if "fitting" not in texts[1]:
        return None
    if not (16.0 <= first[0][0] <= 20.0 and 32.0 <= first[3][1] <= 35.0):
        return None
    return first


def apply_known_source_repair(
    subtitle: Path,
    cache_dir: Path,
    *,
    media_id: int | None,
    episode: int | None,
    candidate_name: str,
    timing_reference: Path | None,
    force: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Repair explicitly known incomplete subtitle sources.

    Rules are deliberately narrow: exact media/episode/source identity plus
    structural validation of both the Japanese source and the selected timing
    reference are required before any cue is moved.  The output holds only the
    user's own subtitle text.
    """

    if subtitle.suffix.casefold() != ".srt" or not subtitle.is_file():
        return subtitle, {"applied": False, "reason": "unsupported_or_missing"}
    if not _matches_bleach_e45(media_id=media_id, episode=episode, candidate_name=candidate_name):
        return subtitle, {"applied": False, "reason": "no_matching_rule"}
    if timing_reference is None or not timing_reference.is_file():
        return subtitle, {"applied": False, "reason": "timing_reference_missing"}

    try:
        source_cues = parse_srt(subtitle)
    except (OSError, ValueError):
        return subtitle, {"applied": False, "reason": "source_parse_failed"}
    sentences = [(_sentence_hash(text), text) for text in _opening_sentences(source_cues)]
    found = {digest for digest, _text in sentences}
    if not all(digest in found for digest in _BLEACH_E45_REQUIRED_SENTENCES):
        return subtitle, {"applied": False, "reason": "source_signature_mismatch"}

    reference_opening = _bleach_reference_opening(timing_reference)
    if reference_opening is None:
        return subtitle, {"applied": False, "reason": "reference_signature_mismatch"}

    # The user's own sentences, each on the reference cue it is spoken in.
    by_slot: dict[int, list[str]] = {}
    for digest, text in sentences:
        slot = _BLEACH_E45_SOURCE_SENTENCES.get(digest)
        if slot is not None and slot < len(reference_opening):
            by_slot.setdefault(slot, []).append(text)
    repaired_opening = [
        (float(reference_opening[slot][0]), float(reference_opening[slot][1]), "\n".join(texts))
        for slot, texts in sorted(by_slot.items())
    ]
    cutoff = _OPENING_CUTOFF_SECONDS
    retained = [cue for cue in source_cues if cue[0] >= cutoff]
    if not retained:
        return subtitle, {"applied": False, "reason": "no_post_opening_cues"}

    stat = subtitle.stat()
    ref_stat = timing_reference.stat()
    digest = hashlib.sha1(
        (
            f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{timing_reference.resolve()}:{ref_stat.st_size}:{ref_stat.st_mtime_ns}:"
            f"{_REPAIR_VERSION}:bleach-e45"
        ).encode()
    ).hexdigest()[:20]
    output = (cache_dir / "source-repairs" / f"v2-{digest}.srt").expanduser()
    if force:
        output.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired_opening + retained, output, preserve_order=True)

    return output, {
        "applied": True,
        "reason": "known_incomplete_source_repaired",
        "version": _REPAIR_VERSION,
        "rule": "bleach-e45-nanako-tva-leading-dialogue",
        "timing_reference": str(timing_reference),
        "replaced_source_cues": sum(1 for cue in source_cues if cue[0] < cutoff),
        "inserted_opening_cues": len(repaired_opening),
        "opening_start": repaired_opening[0][0],
        "opening_end": repaired_opening[-1][1],
        "output": str(output),
    }
