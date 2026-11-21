"""Subtitle rebuild rules: prepare again the episodes an older aligner got wrong.

Each rule recognises the diagnostic shape of a fixed defect in the latest selected
subtitle of an episode (``details.alignment`` of its subtitle history row) and, when
the selection was made by an algorithm older than the fix, prepares the episode again.

``run_pending_rules`` evaluates every pending rule in one pass over the prepared
episodes and their latest selection (one query, each details row decoded once) and
records completion per rule. A pass with nothing pending reads a few state rows:

- ``generation`` rules run once (their state key holds the generation, as before);
- ``selection`` rules record the newest history row they have seen and later look only
  at episodes whose latest selection is newer (a new selection, or one recovered from
  history); a per-episode marker makes each selection count once.

A new rule runs on the next maintenance pass by itself; change a rule's ``generation``
when its predicate changes so that it looks at every episode again.

The rebuild itself is ``AnimeManager._requeue_rebuild``: a Ready or Watched episode
keeps playing its subtitles while they are prepared again, and keeps them if that fails.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from . import audio_timeline
from .database import Database
from .i18n import err

_TIMELINE_VERSION = re.compile(r"\btimeline-v(\d+)\.(\d+)")


def timeline_version(alignment: dict[str, Any]) -> tuple[int, int] | None:
    """``(6, 21)`` for ``timeline-v6.21-agreed-crossover-fine-offset``; None without one."""
    match = _TIMELINE_VERSION.match(str(alignment.get("timeline_algorithm") or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _older_than(alignment: dict[str, Any], fixed_in: tuple[int, int]) -> bool:
    version = timeline_version(alignment)
    return version is not None and version < fixed_in


@dataclass(frozen=True)
class Selection:
    """One prepared episode with its latest selected subtitle history row."""

    item: Any
    latest: dict[str, Any]

    @property
    def details(self) -> dict[str, Any]:
        details = self.latest.get("details")
        return details if isinstance(details, dict) else {}

    @property
    def alignment(self) -> dict[str, Any]:
        alignment = self.details.get("alignment")
        return alignment if isinstance(alignment, dict) else {}

    @property
    def source(self) -> str:
        return str(self.latest.get("source") or "").casefold()

    @property
    def history_id(self) -> int:
        try:
            return int(self.latest.get("id") or 0)
        except (TypeError, ValueError):
            return 0


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# --- rule predicates (the shapes are the ones the per-rule passes matched before) ----------


def _false_positive_tail_clock(selection: Selection) -> bool:
    # v6.14: a one/two-window positive tail transition after a strongly established clock.
    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    segments = alignment.get("timeline_segments")
    if not isinstance(segments, list) or len(segments) < 2:
        return False
    left, right = _dict(segments[-2]), _dict(segments[-1])
    tail_guard = _dict(alignment.get("timeline_weak_tail_guard"))
    try:
        left_offset = float(left.get("offset_seconds") or 0.0)
        right_offset = float(right.get("offset_seconds") or 0.0)
        left_support = int(left.get("support") or 0)
        right_support = int(right.get("support") or 0)
        boundary_ratio = float(tail_guard.get("boundary_ratio") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(
        right_offset > left_offset
        and abs(right_offset - left_offset) >= 15.0
        and left_support >= 6
        and 1 <= right_support <= 2
        and boundary_ratio >= 0.80
    )


def _decreasing_gap(selection: Selection) -> bool:
    # An early decreasing boundary moved forward by >=8 s through repeated monotonic
    # stabilisation (seen in timeline-v6.16; v6.17 anchors the decreasing gap).
    if selection.source != "jimaku" or not _older_than(selection.alignment, (6, 17)):
        return False
    refinements = selection.alignment.get("timeline_monotonic_refinements")
    if not isinstance(refinements, list):
        return False
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in refinements:
        if not isinstance(row, dict) or not bool(row.get("applied")):
            continue
        if str(row.get("reason") or "") != "decreasing_boundary_extended_for_monotonicity":
            continue
        try:
            index = int(row.get("boundary_index") or 0)
        except (TypeError, ValueError):
            continue
        groups.setdefault(index, []).append(row)
    for rows in groups.values():
        try:
            first_old = float(rows[0].get("old_source_time") or 0.0)
            last_new = float(rows[-1].get("new_source_time") or first_old)
            left_offset = float(rows[0].get("left_offset_seconds") or 0.0)
            right_offset = float(rows[0].get("right_offset_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        if 0.0 < first_old <= 300.0 and left_offset - right_offset >= 4.0 and last_new - first_old >= 8.0:
            return True
    return False


def _local_edge(selection: Selection) -> bool:
    # A large opening-edge mismatch that was not checked against the Japanese audio
    # (seen in timeline-v6.17; v6.19 verifies it).
    if selection.source != "jimaku" or not _older_than(selection.alignment, (6, 19)):
        return False
    risk = selection.alignment.get("timeline_early_edit_audio_verification")
    if not isinstance(risk, dict) or bool(risk.get("required")):
        return False
    try:
        cold_delta = abs(float(risk.get("cold_start_delta_seconds") or 0.0))
    except (TypeError, ValueError):
        return False
    return 4.0 <= cold_delta <= 15.0


def _weak_opening_clock_kept(alignment: dict[str, Any]) -> dict[str, Any] | None:
    """The opening-gap reacquire left a strongly supported later clock unused."""
    if alignment.get("engine") != "embedded-reference+timeline":
        return None
    gap = alignment.get("timeline_opening_gap_reacquire")
    if (
        not isinstance(gap, dict)
        or gap.get("applied")
        or gap.get("reason") != "later_clock_not_clearly_better"
    ):
        return None
    try:
        strong = (
            float(gap.get("gap_seconds") or 0) >= 45.0
            and int(gap.get("candidate_support") or 0) >= 8
            and abs(float(gap["candidate_offset_seconds"]) - float(gap["current_offset_seconds"])) >= 4.0
        )
    except (KeyError, TypeError, ValueError):
        return None
    return gap if strong else None


def _shared_opening(selection: Selection) -> bool:
    # v6.19 kept a weak post-opening clock although a later clock had strong support
    # (Re:Zero; v6.20 checks unique shared text first).
    return (
        timeline_version(selection.alignment) == (6, 19)
        and _weak_opening_clock_kept(selection.alignment) is not None
    )


def _shared_opening_before_v6_19(selection: Selection) -> bool:
    # The same shape in older versions mostly belongs to correct fits whose later clock
    # starts minutes after the opening: in 150 synthetic episodes each on v6.0 and v6.14,
    # 11 of its 12 matches were right. A later clock that already fits the minute after
    # the opening better, by line activity and by timing error, shows the old clock was
    # carried too far: Re:Zero E19 has +0.007 and +0.28 s there; each right fit had at
    # least one of the two negative.
    if not _older_than(selection.alignment, (6, 19)):
        return False
    gap = _weak_opening_clock_kept(selection.alignment)
    if gap is None:
        return False
    try:
        return float(gap["activity_gain"]) > 0.0 and float(gap["mean_error_gain_seconds"]) > 0.0
    except (KeyError, TypeError, ValueError):
        return False


# Before v6.20 every crossover boundary is not suspect: only one far from the cost-based
# estimate of the same boundary (FX Senshi Kurumi-chan E03: 952.5 s against 1029.8 s; the
# fixed aligner puts it at 1022.5 s, 7 s from that estimate).
_OLD_CROSSOVER_DISAGREEMENT_SECONDS = 30.0


def _early_crossover(selection: Selection) -> bool:
    # The clock change went to the first spot where the new clock was marginally better,
    # a minute before the cut (FX Senshi Kurumi-chan E03; v6.21 keeps the crossover the
    # whole scan agrees with).
    alignment = selection.alignment
    if alignment.get("engine") != "embedded-reference+timeline" or not _older_than(alignment, (6, 21)):
        return False
    boundaries = alignment.get("timeline_boundaries")
    if not isinstance(boundaries, list):
        return False
    version = timeline_version(alignment) or (0, 0)
    for row in boundaries:
        refinement = _dict(row.get("refinement")) if isinstance(row, dict) else {}
        if refinement.get("method") != "fixed_offset_crossover":
            continue
        if version >= (6, 20):
            return True
        try:
            disagreement = abs(float(row["source_time"]) - float(refinement["fallback_source_time"]))
        except (KeyError, TypeError, ValueError):
            continue
        if disagreement >= _OLD_CROSSOVER_DISAGREEMENT_SECONDS:
            return True
    return False


def _bridged_singleton_tail(selection: Selection) -> bool:
    # The last clock was one window reached through a bridge (Dorohedoro E05: the last two
    # minutes 4-5 s early; v6.22 keeps the episode's clock there).
    alignment = selection.alignment
    if alignment.get("engine") != "embedded-reference+timeline" or not _older_than(alignment, (6, 22)):
        return False
    segments = alignment.get("timeline_segments")
    if not isinstance(segments, list) or len(segments) < 3:
        return False
    bridge, tail = segments[-2], segments[-1]
    return bool(
        isinstance(bridge, dict)
        and isinstance(tail, dict)
        and bridge.get("kind") == "transition_bridge"
        and str(tail.get("support")) == "1"
    )


def _speech_map_without_late_cut(selection: Selection) -> bool:
    # The early-edit speech map was used for the whole episode although the timeline had
    # a later cut (FX Senshi Kurumi-chan E03; new selections record ``timeline_late_cut``).
    alignment = selection.alignment
    if (
        alignment.get("selection_reason") != "early_edit_japanese_speech_verification"
        or "timeline_late_cut" in alignment
    ):
        return False
    attempt = alignment.get("embedded_timeline_attempt")
    boundaries = attempt.get("timeline_boundaries") if isinstance(attempt, dict) else None
    for row in boundaries if isinstance(boundaries, list) else []:
        try:
            if (
                float(row["source_time"]) >= 420.0
                and abs(float(row["right_offset_seconds"]) - float(row["left_offset_seconds"])) >= 2.0
            ):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def _unverified_timeline_edit(selection: Selection) -> bool:
    from .subtitles.validation import unverified_timeline_edit

    return unverified_timeline_edit(selection.alignment)


def _stt_available(manager: Any) -> bool:
    from .subtitles.stt import japanese_stt_available

    sync = getattr(manager.config, "sync", None)
    return japanese_stt_available(enabled=bool(sync and sync.japanese_stt_fallback))


def _stt_timeline_clock_conflict(selection: Selection) -> bool:
    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    scaffold = _dict(alignment.get("embedded_opening_clock_scaffold"))
    old_conflict = str(scaffold.get("reason_detail") or "") == "speech_clock_disagrees_with_post_plateau"
    reacquire = _dict(_dict(alignment.get("embedded_timeline_attempt")).get("timeline_opening_gap_reacquire"))
    new_conflict = bool(
        str(alignment.get("selection_reason") or "") == "early_edit_japanese_speech_verification"
        and reacquire.get("applied")
        and str(reacquire.get("reason") or "") == "dominant_clock_reacquired_after_opening_gap"
    )
    return old_conflict or new_conflict


def _stt_opening_plateau_onset_only(selection: Selection) -> bool:
    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    plateau = _dict(alignment.get("stt_opening_plateau_refinement"))
    try:
        gap_seconds = float(plateau.get("gap_seconds") or 0.0)
    except (TypeError, ValueError):
        gap_seconds = 0.0
    return bool(
        str(alignment.get("engine") or "").startswith("japanese-stt+alass+opening-plateau")
        and plateau.get("applied")
        and gap_seconds >= 45.0
        and "pre_semantic" not in plateau
        and "post_semantic" not in plateau
    )


def _double_opening_scaffold(selection: Selection) -> bool:
    # Cold opens shifted twice: by ALASS at the OP and again by the scaffold.
    from .subtitles.validation import alass_transition_matching_timeline_boundary

    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    scaffold = alignment.get("embedded_opening_clock_scaffold")
    embedded = alignment.get("embedded_timeline_attempt")
    safety = alignment.get("stt_alass_transition_safety")
    if not isinstance(scaffold, dict) or not scaffold.get("applied"):
        return False
    if scaffold.get("opening_alass_transition") is not None:
        return False
    if not isinstance(embedded, dict) or not isinstance(safety, dict):
        return False
    return (
        alass_transition_matching_timeline_boundary(
            safety.get("transitions"), embedded.get("timeline_boundaries")
        )
        is not None
    )


def _unsafe_stt_transition_map(selection: Selection) -> bool:
    # A cached STT selection containing a large unsupported clock jump.
    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    if not str(alignment.get("engine") or "").startswith("japanese-stt+alass"):
        return False
    safety = _dict(alignment.get("stt_alass_transition_safety"))
    if not safety or bool(safety.get("accepted")):
        return False
    largest = 0.0
    transitions = safety.get("transitions")
    for row in transitions if isinstance(transitions, list) else []:
        if not isinstance(row, dict) or bool(row.get("gap_supported")):
            continue
        try:
            largest = max(largest, abs(float(row.get("jump_seconds") or 0.0)))
        except (TypeError, ValueError):
            continue
    return largest >= 20.0


def _opening_gap_ambiguity(selection: Selection) -> bool:
    # Risky v5.9 opening-gap maps; v6.0 reacquires the clock after the cut.
    if selection.source != "jimaku" or not _older_than(selection.alignment, (6, 0)):
        return False
    risk = _dict(selection.alignment.get("timeline_early_edit_audio_verification"))
    reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
    return "opening_gap_clock_ambiguity" in {str(value) for value in reasons}


def _large_cold_open(selection: Selection) -> bool:
    # Cold-open or sparse-gap timeline fixes (the reverted v6.2 20 s cold start included).
    if selection.source == "manual":
        return False
    alignment = selection.alignment
    cold_start = _dict(alignment.get("timeline_cold_start"))
    try:
        cold_delta = abs(float(cold_start.get("delta_seconds") or 0.0))
    except (TypeError, ValueError):
        cold_delta = 0.0
    boundaries = alignment.get("timeline_boundaries")
    if str(cold_start.get("reason") or "") == "edge_hint_not_local" and 15.0 < cold_delta <= 20.0:
        return True
    if (
        timeline_version(alignment) == (6, 2)
        and bool(cold_start.get("applied"))
        and 15.0 < cold_delta <= 20.0
    ):
        return True
    for row in boundaries if isinstance(boundaries, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            jump = abs(float(row.get("jump_seconds") or 0.0))
        except (TypeError, ValueError):
            continue
        method = str(_dict(row.get("refinement")).get("method") or "")
        if jump >= 4.0 and method == "fixed_offset_crossover_across_silence":
            return True
    return False


def _configured_ffprobe(manager: Any) -> str | None:
    tools = getattr(getattr(manager, "config", None), "tools", None)
    command = str(getattr(tools, "ffprobe", "") or "ffprobe")
    resolved = shutil.which(command) if "/" not in command else command
    if resolved and Path(resolved).is_file():
        return str(resolved)
    return audio_timeline.ffprobe_beside(str(getattr(tools, "ffmpeg", "") or "ffmpeg"))


def _ffprobe_available(manager: Any) -> bool:
    return _configured_ffprobe(manager) is not None


def _late_audio_track(manager: Any) -> Callable[[Selection], bool]:
    """Selections aligned before the playback timeline (audio_timeline) of a file whose
    audio track starts late (every audio-derived time was early by the delay) or before
    the video (the opening window was early by the video's lead).

    One ffprobe per prepared, aligned episode without the ``audio_timeline`` mark, in the
    maintenance pass (the agent or the CLI, never the window).
    """
    ffprobe = _configured_ffprobe(manager)

    def applies(selection: Selection) -> bool:
        if ffprobe is None or selection.source == "manual" or not selection.alignment:
            return False
        try:
            if int(selection.alignment.get("audio_timeline") or 0) >= 1:
                return False  # prepared with the playback timeline already
        except (TypeError, ValueError):
            pass
        video = Path(selection.item.video_path)
        if not video.is_file():
            return False
        from .subtitles.stt import _select_japanese_audio_stream

        try:
            index, _details = _select_japanese_audio_stream(video, ffprobe)
            found = audio_timeline.timeline(video, ffprobe)
        except Exception:  # noqa: BLE001 - an unreadable file is not rebuilt for this
            return False
        if found is None:
            return False
        # The Japanese track (several unlabelled ones: the first was read) starts late,
        # or the audio starts before the video (the opening window lost that lead).
        return bool(found.delay(index=index) or found.video_lead())

    return applies


def _audio_gap_track(manager: Any) -> Callable[[Selection], bool]:
    """Selections aligned before gaps inside the audio track were filled (audio_timeline
    VERSION 2) of a file whose Japanese track has such a gap (a remux seam): the STT
    clock, ALASS and windows across it were early by the gap after it.

    One packet scan per prepared, aligned episode without the mark (about 1 s for a
    24-minute episode), once, in the maintenance pass.
    """
    ffprobe = _configured_ffprobe(manager)

    def applies(selection: Selection) -> bool:
        if ffprobe is None or selection.source == "manual" or not selection.alignment:
            return False
        try:
            if int(selection.alignment.get("audio_timeline") or 0) >= audio_timeline.VERSION:
                return False
        except (TypeError, ValueError):
            pass
        video = Path(selection.item.video_path)
        if not video.is_file():
            return False
        from .subtitles.stt import _select_japanese_audio_stream

        try:
            index, _details = _select_japanese_audio_stream(video, ffprobe)
            if audio_timeline.timeline(video, ffprobe) is None:
                return False
            gaps = audio_timeline.inner_gaps(video, ffprobe, index=index)
        except Exception:  # noqa: BLE001 - an unreadable file is not rebuilt for this
            return False
        return bool(gaps)

    return applies


def _never(_selection: Selection) -> bool:
    return False


def _known_source_repair(selection: Selection) -> bool:
    item = selection.item
    if item.media_id != 185874 or item.episode != 5 or selection.source != "jimaku":
        return False
    if (
        "nanakoraws] bleach sennen kessen-hen s01e45"
        not in str(selection.latest.get("candidate_name") or "").casefold()
    ):
        return False
    return not bool(_dict(selection.alignment.get("source_repair")).get("applied"))


# Timeline clocks are put to 0.01 s from v6.24 (they kept the 0.25-1 s window grid before),
# and Japanese+English files rank as fallbacks since the same change.
_PRECISE_CLOCKS_VERSION = (6, 24)


def _candidate_language_purity(selection: Selection) -> str:
    candidate = selection.details.get("details")
    purity = str(candidate.get("language_purity") or "") if isinstance(candidate, dict) else ""
    if purity:
        return purity
    from .subtitle_formats import subtitle_filename_language_profile

    name = str(selection.latest.get("candidate_name") or selection.details.get("name") or "")
    return str(subtitle_filename_language_profile(name).get("purity") or "") if name else ""


def _imprecise_clock_or_mixed_language(selection: Selection) -> bool:
    """A Jimaku selection timed on the window grid, or a Japanese+other-language file.

    Manual selections are never touched. A timeline result without its version (saved
    before versions were recorded) counts as old once; a "couldn't sync" episode is
    retried by its own backoff anyway.
    """
    if selection.source != "jimaku":
        return False
    alignment = selection.alignment
    if "timeline" in str(alignment.get("engine") or ""):
        version = timeline_version(alignment)
        if version is None or version < _PRECISE_CLOCKS_VERSION:
            return True
    return _candidate_language_purity(selection).startswith("mixed_")


def _non_dialogue_text_shown(selection: Selection) -> bool:
    """A prepared subtitle that shows ASS vector drawings (``m 0 0 l 670 0 ...``) or
    release credits as text: prepared before the ASS conversion removed them, so the
    timing was fitted with those lines too. One read of the prepared file."""
    if selection.source == "manual":
        return False
    subtitle = getattr(selection.item, "subtitle_path", None)
    if subtitle is None:
        return False
    path = Path(subtitle)
    if path.suffix.casefold() != ".srt" or not path.is_file():
        return False
    from .subtitle_formats import srt_has_non_dialogue_lines

    return srt_has_non_dialogue_lines(path)


def _small_cut_candidate(selection: Selection) -> bool:
    """Timed to an embedded non-Japanese reference on clocks 1.2-10 s apart, before the
    small-cut step (Aoashi S2 E07: a row of ~1 s cuts covered by two clocks 4 s apart)."""
    if selection.source == "manual":
        return False
    alignment = selection.alignment
    if not alignment or "small_cut_correction" in alignment:
        return False
    language = str(alignment.get("timing_reference_language") or "").casefold()
    if not alignment.get("timing_reference") or language in {"", "ja", "jp", "jpn"}:
        return False
    if str(alignment.get("engine") or "").startswith("japanese-stt"):
        return False
    runs = alignment.get("final_timing_runs")
    offsets: list[float] = []
    for run in runs if isinstance(runs, list) else []:
        try:
            offsets.append(float(_dict(run).get("offset_seconds")))
        except (TypeError, ValueError):
            continue
    return any(1.2 <= abs(b - a) <= 10.0 for a, b in zip(offsets, offsets[1:]))


def _staircase_map_unchecked(selection: Selection) -> bool:
    """ALASS timed to an embedded non-Japanese reference on clocks 1.2-30 s apart, before the
    Japanese speech check of such maps (Aoashi S2 E06: six minutes 2-6 s early)."""
    from .subtitles.staircase_check import is_staircase_map

    if selection.source == "manual":
        return False
    alignment = selection.alignment
    if not alignment or "staircase_speech_check" in alignment:
        return False
    if not str(alignment.get("engine") or "").startswith("alass"):
        return False
    language = str(alignment.get("timing_reference_language") or "").casefold()
    if not alignment.get("timing_reference") or language in {"", "ja", "jp", "jpn"}:
        return False
    return is_staircase_map(alignment)


# --- registry ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionRebuild:
    id: str
    applies: Callable[[Selection], bool]
    reason: str  # i18n key of the job reason
    kind: str  # "generation" (once) or "selection" (newer selections later too)
    scope: str = "all"  # "all", "unwatched" (not Watched) or "ready" (Ready only)
    state_key: str = ""  # generation rules: state key holding the generation
    generation: str = "1"
    marker: str = ""  # selection rules: per-episode marker key prefix
    priority: int = 0
    available: Callable[[Any], bool] | None = None
    summary: str = ""  # log line for a generation rule that queued episodes
    # A predicate that needs the manager (its tools); used instead of ``applies``.
    bind: Callable[[Any], Callable[[Selection], bool]] | None = None

    @property
    def progress_key(self) -> str:
        return self.state_key or f"subtitle_rebuild_rule:{self.id}"


RULES: tuple[SelectionRebuild, ...] = (
    SelectionRebuild(
        "tail_clock",
        _false_positive_tail_clock,
        "jobs.requeueLateJump",
        "generation",
        state_key="subtitle_tail_clock_guard_generation",
        summary="Subtitles: rechecking suspicious late timing jumps: {n}",
    ),
    SelectionRebuild(
        "decreasing_gap",
        _decreasing_gap,
        "jobs.requeueEditBoundary",
        "generation",
        state_key="subtitle_decreasing_gap_timeline_upgrade_generation",
        summary="Subtitles: rebuilding stretched edit boundaries: {n}",
    ),
    SelectionRebuild(
        "local_edge",
        _local_edge,
        "jobs.requeueEdgeHints",
        "generation",
        state_key="subtitle_local_edge_timeline_upgrade_generation",
        summary="Subtitles: rechecking the opening clock against the Japanese audio: {n}",
    ),
    SelectionRebuild(
        "shared_opening",
        _shared_opening,
        "jobs.requeueOpeningAnchors",
        "selection",
        marker="subtitle_shared_opening_v17:",
    ),
    SelectionRebuild(
        "shared_opening_before_v6_19",
        _shared_opening_before_v6_19,
        "jobs.requeueOpeningAnchors",
        "selection",
        scope="unwatched",
        marker="subtitle_shared_opening_v17:",
    ),
    SelectionRebuild(
        "early_crossover",
        _early_crossover,
        "jobs.requeueTimelineCrossover",
        "selection",
        scope="unwatched",
        marker="subtitle_crossover_v21:",
    ),
    SelectionRebuild(
        "bridged_singleton_tail",
        _bridged_singleton_tail,
        "jobs.requeueBridgedTail",
        "selection",
        scope="unwatched",
        marker="subtitle_bridged_tail_v22:",
    ),
    SelectionRebuild(
        "speech_late_cut",
        _speech_map_without_late_cut,
        "jobs.requeueSpeechLateCut",
        "selection",
        scope="ready",
        marker="subtitle_late_cut_v1:",
        priority=120,
    ),
    SelectionRebuild(
        "unverified_timeline_edit",
        _unverified_timeline_edit,
        "jobs.requeueTimelineUnverified",
        "selection",
        marker="subtitle_unverified_timeline_v16:",
        available=_stt_available,
    ),
    SelectionRebuild(
        "stt_timeline_conflict",
        _stt_timeline_clock_conflict,
        "jobs.requeueSttTimeline",
        "generation",
        state_key="subtitle_stt_timeline_conflict_generation",
        generation="2",
        summary="Subtitles: rechecking the STT/timeline choice: {n}",
    ),
    SelectionRebuild(
        "stt_opening_plateau",
        _stt_opening_plateau_onset_only,
        "jobs.requeueSttOpening",
        "generation",
        state_key="subtitle_stt_plateau_semantic_generation",
        summary="Subtitles: rechecking STT timing around the opening: {n}",
    ),
    SelectionRebuild(
        "double_opening_scaffold",
        _double_opening_scaffold,
        "jobs.requeueOpeningScaffold",
        "generation",
        state_key="subtitle_double_opening_scaffold_generation",
        summary="Subtitles: rechecking cold-open timing before the opening: {n}",
    ),
    SelectionRebuild(
        "unsafe_stt_transition",
        _unsafe_stt_transition_map,
        "jobs.requeueSttTransition",
        "generation",
        state_key="subtitle_stt_transition_safety_generation",
        summary="Subtitles: rechecking unsafe STT transitions: {n}",
    ),
    SelectionRebuild(
        "opening_gap",
        _opening_gap_ambiguity,
        "jobs.requeueOpeningTiming",
        "generation",
        state_key="subtitle_opening_gap_timeline_upgrade_generation",
        summary="Subtitles: rebuilding timing around the opening: {n}",
    ),
    SelectionRebuild(
        "large_cold_open",
        _large_cold_open,
        "jobs.requeueSubtitleBoundary",
        "generation",
        state_key="subtitle_large_cold_open_generation",
        # 5: a cold open after a sound caption (Black Clover S2 E07) takes its spoken clock.
        generation="5",
        summary="Subtitles: rebuilding timing edit boundaries: {n}",
    ),
    SelectionRebuild(
        "known_source_repair",
        _known_source_repair,
        "jobs.requeueIncompleteSource",
        "generation",
        state_key="subtitle_known_source_repair_generation",
        summary="Subtitles: restoring incomplete sources: {n}",
    ),
    SelectionRebuild(
        "late_audio_track",
        _never,
        "jobs.requeueAudioDelay",
        "generation",
        scope="unwatched",
        state_key="subtitle_audio_playback_timeline_generation",
        available=_ffprobe_available,
        summary="Subtitles: preparing again episodes whose audio and video start apart: {n}",
        bind=_late_audio_track,
    ),
    SelectionRebuild(
        "audio_gap_track",
        _never,
        "jobs.requeueAudioGap",
        "generation",
        scope="unwatched",
        state_key="subtitle_audio_inner_gap_generation",
        available=_ffprobe_available,
        summary="Subtitles: preparing again episodes whose audio track has a gap inside: {n}",
        bind=_audio_gap_track,
    ),
    SelectionRebuild(
        "precise_clocks",
        _imprecise_clock_or_mixed_language,
        "jobs.requeuePreciseClocks",
        "generation",
        scope="unwatched",
        state_key="subtitle_precise_clock_generation",
        summary="Subtitles: preparing again with exact timing and Japanese-only text: {n}",
    ),
    SelectionRebuild(
        "non_dialogue_text",
        _non_dialogue_text_shown,
        "jobs.requeueNonDialogueText",
        "generation",
        scope="unwatched",
        state_key="subtitle_non_dialogue_text_generation",
        summary="Subtitles: preparing again without drawings and credits shown as text: {n}",
    ),
    SelectionRebuild(
        "small_cuts",
        _small_cut_candidate,
        "jobs.requeueSmallCuts",
        "generation",
        scope="unwatched",
        state_key="subtitle_small_cuts_generation",
        summary="Subtitles: preparing again where small cuts sat between two clocks: {n}",
    ),
    SelectionRebuild(
        "staircase_speech",
        _staircase_map_unchecked,
        "jobs.requeueStaircaseSpeech",
        "generation",
        scope="unwatched",
        state_key="subtitle_staircase_speech_generation",
        available=_stt_available,
        summary="Subtitles: checking clocks a few seconds apart against the Japanese speech: {n}",
    ),
)
RULES_BY_ID = {rule.id: rule for rule in RULES}


# --- pass ----------------------------------------------------------------------------------


def _selections(db: Any, *, after_id: int) -> Iterator[Selection]:
    if isinstance(db, Database):
        for item, latest in db.latest_selected_subtitle_rows(after_id=after_id):
            yield Selection(item, latest)
        return
    # Test doubles and other database objects: one query per episode.
    for item in db.episodes():
        if item.subtitle_path is None:
            continue
        latest = db.latest_selected_subtitle(item.video_path)
        if not latest:
            continue
        selection = Selection(item, latest)
        if after_id and selection.history_id <= after_id:
            continue
        yield selection


def _max_history_id(db: Any) -> int:
    return db.subtitle_history_max_id() if isinstance(db, Database) else 0


def _in_scope(rule: SelectionRebuild, item: Any) -> bool:
    state = str(getattr(item, "state", "") or "")
    if state == "dropped":
        return False
    if rule.scope == "unwatched":
        return state != "watched"
    if rule.scope == "ready":
        return state == "ready"
    return True


def _marker_key(rule: SelectionRebuild, item: Any) -> str:
    return rule.marker + hashlib.sha256(str(item.video_path).encode()).hexdigest()[:24]


def video_marker_keys(video: Path) -> list[str]:
    """Every per-episode rebuild marker key of ``video`` (dropped with a deleted video)."""
    item = SimpleNamespace(video_path=video)
    return list(dict.fromkeys(_marker_key(rule, item) for rule in RULES if rule.marker))


def _read_progress(db: Any, rule: SelectionRebuild) -> int | None:
    """History watermark of a completed selection rule; None when it has not run."""
    generation, _sep, watermark = str(db.get_state(rule.progress_key, "") or "").partition(":")
    if generation != rule.generation or not watermark.isdigit():
        return None
    return int(watermark)


def _evaluate(manager: Any, plan: list[tuple[SelectionRebuild, int]]) -> dict[str, int]:
    db = manager.db
    counts = {rule.id: 0 for rule, _after in plan}
    predicates = {
        rule.id: rule.bind(manager) if rule.bind is not None else rule.applies for rule, _after in plan
    }
    for selection in _selections(db, after_id=min(after for _rule, after in plan)):
        item = selection.item
        for rule, after in plan:
            if after and selection.history_id <= after:
                continue
            if not _in_scope(rule, item) or not predicates[rule.id](selection):
                continue
            marker_key = _marker_key(rule, item) if rule.marker else ""
            marker = str(selection.latest.get("id") or "legacy")
            if marker_key and db.get_state(marker_key, "") == marker:
                continue
            if manager._requeue_rebuild(item, err(rule.reason), priority=rule.priority):
                counts[rule.id] += 1
                if marker_key:
                    db.set_state(marker_key, marker)
            # One rule per episode and pass: the first match prepares it again.
            break
    return counts


def _finish(manager: Any, plan: list[tuple[SelectionRebuild, int]], counts: dict[str, int], top: int) -> None:
    db = manager.db
    for rule, _after in plan:
        if rule.kind == "generation":
            db.set_state(rule.progress_key, rule.generation)
            if counts[rule.id] and rule.summary:
                manager.log(rule.summary.format(n=counts[rule.id]))
        else:
            db.set_state(rule.progress_key, f"{rule.generation}:{top}")
    logger = getattr(manager, "logger", None)
    if logger is not None and any(counts.values()):
        logger.info(
            "REQUEUE step=subtitle.rebuild_rules %s",
            " ".join(f"{rule_id}={count}" for rule_id, count in counts.items() if count),
        )


def run_rule(manager: Any, rule_id: str) -> int:
    """One rule over every prepared episode (a generation rule only while not done)."""
    rule = RULES_BY_ID[rule_id]
    db = manager.db
    if rule.kind == "generation" and db.get_state(rule.progress_key, "") == rule.generation:
        return 0
    if rule.available is not None and not rule.available(manager):
        return 0
    top = _max_history_id(db)
    plan = [(rule, 0)]
    counts = _evaluate(manager, plan)
    _finish(manager, plan, counts, top)
    return counts[rule.id]


def run_pending_rules(manager: Any) -> dict[str, int]:
    """Every rule that has work, in one pass; returns the episodes queued per rule."""
    db = manager.db
    top = _max_history_id(db)
    plan: list[tuple[SelectionRebuild, int]] = []
    for rule in RULES:
        if rule.kind == "generation":
            if db.get_state(rule.progress_key, "") != rule.generation:
                # Not marked done while it cannot look (e.g. ffprobe not found yet).
                if rule.available is not None and not rule.available(manager):
                    continue
                plan.append((rule, 0))
            continue
        watermark = _read_progress(db, rule)
        if watermark is not None and watermark == top:
            continue
        if watermark is not None and watermark > top:
            watermark = None  # the history went back (a restored database): look again
        if rule.available is not None and not rule.available(manager):
            continue
        plan.append((rule, watermark or 0))
    if not plan:
        return {}
    counts = _evaluate(manager, plan)
    _finish(manager, plan, counts, top)
    return counts
