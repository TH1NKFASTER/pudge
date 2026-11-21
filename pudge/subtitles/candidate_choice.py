"""Second-candidate check: of the two first Jimaku files, keep the one whose cues meet the voice.

Pudge ranks the candidates by their metadata and keeps the first one that passes the quality
gates. On the benchmark (670 episodes, 2645 single-candidate runs) the best signal Pudge itself
has of how well a finished timing fits the audio is the *onset matched fraction*: the share of
output cue starts with a speech onset (a voice-activity rise after a short silence, from the
episode's own audio) within ``onset_lead.WINDOW_SECONDS`` - the same measurement the onset-lead
step makes. Syncing the second file too and keeping it only when its fraction is higher lowered
the mean loss against the best available file from 0.100 to 0.077 (both halves of the corpus,
dev and holdout, gold and silver alike); a margin, a lead tie-break or a third file did not help
reliably.

A second file with fewer matched cues than the first never wins on its fraction alone: fewer
cues inflate the fraction (a TV capture with 299 of 364 cues on onsets beat an Amazon file with
346 of 445 and lost the episode, Mac benchmark fx174), so the second file must also match at
least as many cues (onset-select-2).

Missing signals (too few cues, no voice activity, a file timed for this exact release) keep the
first file, as does a second file that fails its sync or the quality gate. A first file with
nearly every cue on an onset (``SKIP_SECOND_MATCHED_FRACTION``) is not beaten on that corpus, so
the second sync is skipped there.

The preferred source (onset-select-3): the file Pudge accepts first is often not the best one
to start from. Measured against one common oracle for every candidate (each output scored
against every candidate's silver, and gold where the pack has it; Mac all-candidates run
fx187, 895 episodes, 3487 single-file runs), the file timed to a WEB release, then BD, then TV
(``source_rank``), an SRT, the higher score (``preferred_key``) was the better start: synced
instead of Pudge's own second choice whenever it is not the accepted file, and kept unless the
accepted file matches more onsets (``choose_preferred``), the mean loss fell from 0.081 to 0.073
(halves 0.083 -> 0.079 and 0.079 -> 0.067, dev 0.074 -> 0.066, holdout 0.099 -> 0.091, gold
0.0074 -> 0.0018; 65 episodes better, 39 worse by more than 2 points) with the same two syncs
per episode. Margins and a lead tie-break did not help (fx187: the switch rule above is kept).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import onset_lead

VERSION = "onset-select-3"
SKIP_SECOND_MATCHED_FRACTION = 0.9


def onset_signal(starts: Sequence[float], onsets: Sequence[float]) -> dict[str, object]:
    """The onset measurement of one output, with ``matched_fraction`` (None when unusable)."""
    if len(starts) < onset_lead.MIN_MATCHED_CUES:
        return {"available": False, "reason": "too_few_cues", "cues": len(starts), "matched_fraction": None}
    measurement = dict(onset_lead.onset_lead(starts, onsets))
    cues = int(measurement.get("cues") or 0)
    matched = int(measurement.get("matched_cues") or 0)
    measurement["matched_fraction"] = (
        round(matched / cues, 4) if measurement.get("available") and cues else None
    )
    return measurement


def matched_fraction(signal: dict[str, object] | None) -> float | None:
    if not isinstance(signal, dict):
        return None
    value = signal.get("matched_fraction")
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def needs_second(first: dict[str, object] | None) -> tuple[bool, str]:
    """Whether syncing the second file can change the choice."""
    value = matched_fraction(first)
    if value is None:
        return False, "first_signal_missing"
    if value >= SKIP_SECOND_MATCHED_FRACTION:
        return False, "first_onsets_matched"
    return True, "check_second"


def choose(first: dict[str, object] | None, second: dict[str, object] | None) -> tuple[int, str]:
    """0 keeps the first file, 1 takes the second: only with a strictly higher fraction and at
    least as many matched cues."""
    if second is None:
        return 0, "second_unavailable"
    a, b = matched_fraction(first), matched_fraction(second)
    if a is None or b is None:
        return 0, "signal_missing"
    if b <= a:
        return 0, "first_not_beaten"
    if _matched_cues(second) < _matched_cues(first):
        return 0, "second_fewer_matched_cues"
    return 1, "second_matches_more_onsets"


def _matched_cues(signal: dict[str, object] | None) -> int:
    if not isinstance(signal, dict):
        return 0
    try:
        return int(signal.get("matched_cues") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# The release a subtitle file was timed to, from its name (the sync bench's own rules).
_BD = re.compile(r"(?i)(?<![a-z])(?:bd|bdrip|bd-?rip|blu-?ray|bdmv|bdremux|bd\d{3,4}p?)(?![a-z])")
_WEB = re.compile(r"(?i)(?<![a-z])(?:web|webrip|web-?dl|web-?rip)(?![a-z])")
_TV = re.compile(
    r"(?i)(?<![a-z])(?:tv|hdtv|tvrip|dtv|at-x|atx|tbs|bs11|bs-?11|mbs|tokyo ?mx|ntv|nhk|wowow|"
    r"animax|retimed from tv|raws?)(?![a-z])"
)
# TS (a transport stream capture) and the station codes TX, EX and CX are also words of a
# title ("Hoshi no Michi EX - 01" ranked as a TV timing): they name the release only where
# release tags stand, never among the title's words.
_TV_SHORT_TAGS = frozenset({"ts", "tx", "ex", "cx"})
_TAG_GROUP = re.compile(r"[\[(（【]([^\[\]()（）【】]*)[\])）】]")
_TAG_SPLIT = re.compile(r"[\s,+&/\-]+")
_RESOLUTION = r"(?:\d{3,4}\s?x\s?\d{3,4}|\d{3,4}[pi])"
# Other tags of such a group: anything with a digit (1920x1080, x265, 10bit), a codec,
# container or language name, or a word in Japanese.
_TAG_WORDS = frozenset(
    {
        "aac",
        "ac",
        "flac",
        "dts",
        "opus",
        "hevc",
        "avc",
        "mpeg",
        "mkv",
        "raw",
        "ja",
        "jp",
        "jpn",
        "jap",
        "sub",
        "subs",
        "cc",
    }
)
_TV_SHORT_AFTER_RESOLUTION = re.compile(rf"(?i)(?<![a-z0-9]){_RESOLUTION}[\s,\-]+(?:ts|tx|ex|cx)(?![a-z0-9])")
# "….ts.srt", "….ts.ja.ass": subtitles taken from a transport stream capture.
_TS_CAPTURE_SUFFIX = re.compile(r"(?i)\.ts(?:\.[a-z]{2,3}(?:-[a-z]{2,4})?)?\.(?:srt|ass|ssa|vtt)$")
_SERVICE = re.compile(
    r"(?i)netflix|(?<![a-z])nf(?![a-z])|amazon|(?<![a-z])amzn(?![a-z])|prime ?video|crunchyroll|"
    r"(?<![a-z])cr(?![a-z])|disney|(?<![a-z])dsnp(?![a-z])|abema|hulu|u-?next|bilibili|"
    r"(?<![a-z])b-?global(?![a-z])|d ?anime|danime"
)
SOURCE_RANKS = {"WEB": 3, "BD": 2, "TV": 1, "?": 0}


def _short_tv_tag(file_name: str, text: str) -> bool:
    """TS / TX / EX / CX as a release tag: a tag of its own or among other tags in brackets
    ("(CX)", "(TX 1920x1080 x265 AAC)"), right after the resolution ("1080p TX"), or the
    capture's extension before the subtitle's ("….ts.srt")."""
    for group in _TAG_GROUP.findall(text):
        tokens = [token for token in _TAG_SPLIT.split(group.casefold()) if token]
        if any(token in _TV_SHORT_TAGS for token in tokens) and all(
            token in _TV_SHORT_TAGS
            or token in _TAG_WORDS
            or any(character.isdigit() for character in token)
            or not token.isascii()  # a station's own name beside its code: (テレ東 TX)
            for token in tokens
        ):
            return True
    return bool(_TV_SHORT_AFTER_RESOLUTION.search(text) or _TS_CAPTURE_SUFFIX.search(file_name))


def source_rank(name: str) -> str:
    """``WEB`` / ``BD`` / ``TV`` / ``?``: the release a subtitle file name says it was timed to."""
    file_name = Path(str(name)).name
    text = file_name.replace("_", " ").replace(".", " ")
    if _BD.search(text):
        return "BD"
    if _SERVICE.search(text) or _WEB.search(text):
        return "WEB"
    if _TV.search(text) or _short_tv_tag(file_name, text):
        return "TV"
    return "?"


def preferred_key(candidate: Any) -> tuple[int, bool, float]:
    """WEB > BD > TV > unknown, then an SRT, then the candidate's score."""
    name = str(getattr(candidate, "name", "") or getattr(candidate, "path", ""))
    path = Path(str(getattr(candidate, "path", name)))
    return (
        SOURCE_RANKS[source_rank(name)],
        path.suffix.casefold() == ".srt",
        float(getattr(candidate, "score", 0.0) or 0.0),
    )


def preferred_candidate(candidates: Sequence[Any]) -> Any | None:
    """The preferred file (``preferred_key``); the first of equals keeps Pudge's own order."""
    best = None
    for candidate in candidates:
        if best is None or preferred_key(candidate) > preferred_key(best):
            best = candidate
    return best


def choose_preferred(
    preferred: dict[str, object] | None, accepted: dict[str, object] | None
) -> tuple[bool, str]:
    """True takes the synced preferred file; the accepted file stays when it beats it (``choose``
    with the preferred file first) or when the preferred file has no usable signal."""
    if matched_fraction(preferred) is None:
        return False, "preferred_unavailable"
    index, reason = choose(preferred, accepted)
    if index:
        return False, "accepted_matches_more_onsets"
    return True, "preferred_source" if reason == "first_not_beaten" else f"preferred_source:{reason}"
