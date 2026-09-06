from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..subtitle_formats import parse_srt, plain_subtitle_text, write_srt

_REPAIR_VERSION = "known-source-repair-v1"

# This upstream TVA subtitle is structurally incomplete for the WEB episode: it
# starts after two spoken lines that are present in the WEB cut.  Keep this as
# data, not as a global timing heuristic.  The timestamps are taken from the
# already-selected embedded WEB English timing reference after validating its
# opening dialogue shape.
_BLEACH_E45_MEDIA_ID = 185874
_BLEACH_E45_EPISODE = 5
_BLEACH_E45_NAME_TOKEN = "nanakoraws] bleach sennen kessen-hen s01e45"
_BLEACH_E45_REQUIRED_SOURCE_TEXT = ("ふさわしい力だ", "私には届かぬ")
_BLEACH_E45_OPENING_TEXT = (
    "（ユーハバッハ）月牙天衝と王虚の閃光の融合…",
    "あらゆるものの融合によって生まれたお前に…\nふさわしい力だ。",
    "だが　それでもなお…",
    "私には届かぬ！",
)


def _matches_bleach_e45(
    *, media_id: int | None, episode: int | None, candidate_name: str
) -> bool:
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
    # Validate the semantic shape without depending on exact fansub punctuation.
    if "getsuga" not in texts[0] or "gran rey" not in texts[0]:
        return None
    if "fitting power" not in texts[1] and "fitting" not in texts[1]:
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
    reference are required before any text is changed.
    """

    if subtitle.suffix.casefold() != ".srt" or not subtitle.is_file():
        return subtitle, {"applied": False, "reason": "unsupported_or_missing"}
    if not _matches_bleach_e45(
        media_id=media_id, episode=episode, candidate_name=candidate_name
    ):
        return subtitle, {"applied": False, "reason": "no_matching_rule"}
    if timing_reference is None or not timing_reference.is_file():
        return subtitle, {"applied": False, "reason": "timing_reference_missing"}

    try:
        source_cues = parse_srt(subtitle)
    except (OSError, ValueError):
        return subtitle, {"applied": False, "reason": "source_parse_failed"}
    source_text = "\n".join(plain_subtitle_text(cue[2]) for cue in source_cues[:4])
    if not all(token in source_text for token in _BLEACH_E45_REQUIRED_SOURCE_TEXT):
        return subtitle, {"applied": False, "reason": "source_signature_mismatch"}

    reference_opening = _bleach_reference_opening(timing_reference)
    if reference_opening is None:
        return subtitle, {"applied": False, "reason": "reference_signature_mismatch"}

    repaired_opening = [
        (float(ref[0]), float(ref[1]), text)
        for ref, text in zip(reference_opening, _BLEACH_E45_OPENING_TEXT)
    ]
    cutoff = 60.0
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
    output = (cache_dir / "source-repairs" / f"v1-{digest}.srt").expanduser()
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
