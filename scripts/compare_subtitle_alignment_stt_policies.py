#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
for value in (PROJECT_ROOT, SCRIPT_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))


def _reexec_with_pudge_runtime_if_needed() -> None:
    """Keep the policy matrix entrypoint when switching to Pudge's Python."""
    try:
        import PIL  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if os.environ.get("PUDGE_STT_POLICY_MATRIX_REEXEC") == "1":
        raise RuntimeError(
            "Pudge STT policy matrix needs Pillow, but the selected Pudge Python does not provide it."
        )
    candidates = [
        PROJECT_ROOT / ".venv-patch-v0725" / "bin" / "python",
        PROJECT_ROOT / ".venv" / "bin" / "python",
        Path.home() / ".local" / "share" / "pudge" / "venv" / "bin" / "python",
    ]
    current = Path(sys.executable).resolve()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            if candidate.resolve() == current:
                continue
            probe = subprocess.run(
                [str(candidate), "-c", "import PIL"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode != 0:
            continue
        env = os.environ.copy()
        env["PUDGE_STT_POLICY_MATRIX_REEXEC"] = "1"
        script = str(Path(__file__).resolve())
        os.execve(str(candidate), [str(candidate), script, *sys.argv[1:]], env)
    raise RuntimeError(
        "Could not find a Pudge Python with Pillow. Expected .venv-patch-v0725, .venv, "
        "or ~/.local/share/pudge/venv/bin/python."
    )


_reexec_with_pudge_runtime_if_needed()

# Reuse discovery and the production A/B runner after runtime bootstrap.
import compare_subtitle_alignment as ab

from pudge.syncing import (
    _stt_text_clock_candidate,
    _subtitle_stt_text_score,
    compare_timing_activity,
    convert_to_plain_srt,
    prepare_japanese_stt_reference,
    subtitle_quality_accepted,
)


POLICIES: tuple[dict[str, Any], ...] = (
    {
        "level": 0,
        "key": "off",
        "title": "STT text clock disabled",
        "meaning": "Control: production pipeline without the semantic STT text clock.",
    },
    {
        "level": 1,
        "key": "last_resort",
        "title": "Current last resort",
        "meaning": "Current production behavior: STT text clock is reached only inside the final Japanese STT fallback.",
    },
    {
        "level": 2,
        "key": "reject_rescue",
        "title": "Rescue rejected result",
        "meaning": "STT may replace the current result only when the normal quality gate rejects it.",
    },
    {
        "level": 3,
        "key": "suspicious_rescue",
        "title": "Rescue suspicious clocks",
        "meaning": "Also allow STT when explicit discontinuity or unreliable local diagnostics make the current clock suspicious.",
    },
    {
        "level": 4,
        "key": "audio_competitor",
        "title": "Compete with audio-only engines",
        "meaning": "Also let accepted STT replace ffsubsync/audio-ALASS results that have no trusted embedded text reference.",
    },
    {
        "level": 5,
        "key": "weak_reference_competitor",
        "title": "Compete with weak embedded reference",
        "meaning": "Also let STT replace an embedded-reference result when that reference is not marked reliable or has weak activity.",
    },
    {
        "level": 6,
        "key": "semantic_advantage",
        "title": "Semantic-score winner",
        "meaning": "Also let STT win against non-high-trust clocks when its Japanese semantic ranking score beats the current result by >= 0.08.",
    },
    {
        "level": 7,
        "key": "strict_peer",
        "title": "Strict peer candidate",
        "meaning": "A strong accepted STT clock becomes a peer candidate against every result except a high-trust embedded reference.",
    },
    {
        "level": 8,
        "key": "preferred_semantic",
        "title": "Preferred semantic clock",
        "meaning": "A very strong STT clock may also replace a trusted embedded reference unless that reference is exceptionally strong.",
    },
    {
        "level": 9,
        "key": "primary_text_clock",
        "title": "Primary accepted text clock",
        "meaning": "Upper bound: whenever the raw semantic STT clock passes score/coverage/activity minima, use it even if the production transition gate rejects it.",
    },
)

POLICY_BY_LEVEL = {int(row["level"]): row for row in POLICIES}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Compare ten increasing STT text-clock influence policies on the same local/benchmark subtitle cases. "
            "Runs expensive transcription once per case and derives a policy frontier showing where semantic STT first changes the result."
        )
    )
    p.add_argument("--config", type=Path, default=ab.DEFAULT_CONFIG_PATH)
    p.add_argument("--corpus", type=Path, action="append", default=[])
    p.add_argument("--search-root", type=Path, action="append", default=[])
    p.add_argument("--output", type=Path, help="default: ~/Downloads/pudge-subtitle-stt-policy-matrix-<timestamp>")
    p.add_argument("--limit", type=int, default=0, help="0 = all cases")
    p.add_argument(
        "--time-limit-minutes",
        type=float,
        default=0.0,
        help="global wall-clock budget; checkpoints are written after every case; 0 = unlimited",
    )
    p.add_argument(
        "--case-timeout-seconds",
        type=float,
        default=45.0,
        help="hard budget for one case on macOS/Linux so a single movie cannot consume the whole run; 0 = disabled",
    )
    p.add_argument("--force", action="store_true", help="ignore alignment caches; shared Whisper transcription is still reused")
    p.add_argument("--local-only", action="store_true")
    p.add_argument("--benchmark-only", action="store_true")
    p.add_argument("--no-open", action="store_true")
    return p


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _CaseTimeout(RuntimeError):
    pass


class _case_deadline:
    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self._old_handler = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, "setitimer"):
            return self
        self._old_handler = signal.getsignal(signal.SIGALRM)
        def _alarm(_signum, _frame):
            raise _CaseTimeout(f"case exceeded {self.seconds:.1f}s")
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0 and hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old_handler)
        return False


def _probe_candidate_output(probe: dict[str, Any]) -> str | None:
    raw = probe.get("output") or probe.get("rejected_output")
    return str(raw) if raw else None


def _raw_stt_eligible(probe: dict[str, Any]) -> bool:
    text = probe.get("text_alignment") if isinstance(probe.get("text_alignment"), dict) else {}
    activity = probe.get("activity") if isinstance(probe.get("activity"), dict) else {}
    return bool(
        probe.get("available")
        and _probe_candidate_output(probe)
        and _float(text.get("score")) >= 0.62
        and _float(text.get("coverage")) >= 0.35
        and _float(activity.get("weighted")) >= 0.20
    )


def _candidate_for_run(case: dict[str, Any], run: dict[str, Any]):
    raw = str(run.get("selected_path") or "")
    for candidate in case["candidates"]:
        try:
            if raw and Path(candidate.path).resolve() == Path(raw).resolve():
                return candidate
        except OSError:
            continue
    return case["candidates"][0] if case.get("candidates") else None


def _prepare_probe_source(candidate: Any, cache: Path, config: Any) -> tuple[Path | None, dict[str, Any]]:
    if candidate is None:
        return None, {"available": False, "accepted": False, "reason": "no_selected_candidate"}
    source = Path(candidate.path)
    if source.suffix.casefold() not in {".ass", ".ssa"}:
        return source, {"converted": False, "reason": "native"}
    converted, conversion = convert_to_plain_srt(
        source,
        cache,
        ffmpeg_path=config.tools.ffmpeg,
        force=False,
        verbose=False,
    )
    if converted.suffix.casefold() != ".srt":
        return None, {
            "available": False,
            "accepted": False,
            "reason": "source_conversion_failed",
            "conversion": conversion,
        }
    return converted, {"converted": True, "reason": conversion.get("reason")}


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_stt(case: dict[str, Any], config: Any, cache: Path, current: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    reference, stt = prepare_japanese_stt_reference(
        Path(case["video"]),
        cache,
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
        model=config.sync.japanese_stt_model,
        timeout_seconds=config.sync.japanese_stt_timeout_seconds,
        force=False,
    )
    if reference is None:
        return {
            "attempted": True,
            "available": False,
            "accepted": False,
            "reason": "stt_reference_unavailable",
            "stt": stt,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    candidate = _candidate_for_run(case, current)
    source, source_meta = _prepare_probe_source(candidate, cache, config)
    if source is None:
        return {
            "attempted": True,
            "available": False,
            "accepted": False,
            "reason": source_meta.get("reason"),
            "source": source_meta,
            "stt": stt,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    output, diagnostics = _stt_text_clock_candidate(
        source,
        reference,
        cache,
        model=config.sync.japanese_stt_model,
    )
    diagnostics = dict(diagnostics)
    diagnostics["attempted"] = True
    diagnostics["reference"] = str(reference)
    diagnostics["reference_sha256"] = _file_sha256(reference)
    diagnostics["source"] = str(source)
    diagnostics["source_sha256"] = _file_sha256(source)
    diagnostics["source_meta"] = source_meta
    diagnostics["candidate_identity"] = {
        "name": str(candidate.name or ""),
        "source": str(candidate.source or ""),
        "path": str(candidate.path),
        "path_sha256": _file_sha256(Path(candidate.path)),
    }
    diagnostics["benchmark_identity"] = case.get("benchmark_identity")
    diagnostics["stt"] = stt
    diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
    diagnostics["output"] = str(output) if output is not None else diagnostics.get("output")

    baseline_path = Path(str(current.get("final_path") or ""))
    if baseline_path.is_file() and baseline_path.suffix.casefold() == ".srt":
        diagnostics["baseline_text_alignment"] = _subtitle_stt_text_score(baseline_path, reference)
        diagnostics["baseline_activity"] = compare_timing_activity(baseline_path, reference)
    else:
        diagnostics["baseline_text_alignment"] = {"available": False, "reason": "baseline_not_srt"}
        diagnostics["baseline_activity"] = {"available": False, "reason": "baseline_not_srt"}
    return diagnostics


def _clock_facts(current: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    result = current.get("result") if isinstance(current.get("result"), dict) else {}
    engine = str(result.get("engine") or "")
    diagnostics = result.get("segment_diagnostics") if isinstance(result.get("segment_diagnostics"), dict) else {}
    ref_activity = result.get("reference_activity") if isinstance(result.get("reference_activity"), dict) else {}
    if not ref_activity and isinstance(probe.get("baseline_activity"), dict):
        ref_activity = probe["baseline_activity"]
    baseline_text = probe.get("baseline_text_alignment") if isinstance(probe.get("baseline_text_alignment"), dict) else {}
    stt_text = probe.get("text_alignment") if isinstance(probe.get("text_alignment"), dict) else {}
    stt_activity = probe.get("activity") if isinstance(probe.get("activity"), dict) else {}

    baseline_rank = _float(baseline_text.get("ranking_score"), _float(baseline_text.get("score")))
    stt_rank = _float(stt_text.get("ranking_score"), _float(stt_text.get("score")))
    baseline_activity = _float(ref_activity.get("weighted"))
    stt_activity_value = _float(stt_activity.get("weighted"))
    stt_score = _float(stt_text.get("score"))
    stt_coverage = _float(stt_text.get("coverage"))

    current_accepted = bool(current.get("accepted"))
    reference_reliable = bool(result.get("reference_alignment_reliable"))
    reference_engine = "embedded-reference" in engine or reference_reliable
    local_unreliable = bool(diagnostics) and diagnostics.get("reliable") is False
    discontinuity = bool(result.get("reference_discontinuity_rejected"))
    suspicious = bool(
        not current_accepted
        or discontinuity
        or local_unreliable
        or result.get("reason") in {"embedded_reference_output_invalid", "alignment_failed"}
    )
    audio_only = bool(not reference_engine and engine in {"ffsubsync", "alass", "alass+ffsubsync"})
    weak_reference = bool(reference_engine and (not reference_reliable or baseline_activity < 0.72))
    semantic_advantage = bool(
        stt_text.get("available")
        and baseline_text.get("available")
        and stt_rank >= baseline_rank + 0.08
        and stt_coverage >= 0.40
    )
    raw_stt_eligible = _raw_stt_eligible(probe)
    strict_stt = bool(
        raw_stt_eligible
        and stt_score >= 0.70
        and stt_coverage >= 0.50
        and stt_activity_value >= 0.45
    )
    preferred_stt = bool(
        raw_stt_eligible
        and stt_score >= 0.76
        and stt_coverage >= 0.58
        and stt_activity_value >= 0.55
    )
    high_trust_reference = bool(
        reference_reliable
        and baseline_activity >= 0.84
        and result.get("reference_output_structure", {}).get("reason") == "ok"
        if isinstance(result.get("reference_output_structure"), dict)
        else reference_reliable and baseline_activity >= 0.84
    )
    exceptional_reference = bool(high_trust_reference and baseline_activity >= 0.93)
    return {
        "engine": engine,
        "current_accepted": current_accepted,
        "reference_reliable": reference_reliable,
        "reference_engine": reference_engine,
        "local_unreliable": local_unreliable,
        "discontinuity": discontinuity,
        "suspicious": suspicious,
        "audio_only": audio_only,
        "weak_reference": weak_reference,
        "baseline_text_rank": round(baseline_rank, 4),
        "stt_text_rank": round(stt_rank, 4),
        "semantic_advantage": semantic_advantage,
        "stt_score": round(stt_score, 4),
        "stt_coverage": round(stt_coverage, 4),
        "baseline_activity": round(baseline_activity, 4),
        "stt_activity": round(stt_activity_value, 4),
        "production_gate_accepted": bool(probe.get("accepted")),
        "raw_stt_eligible": raw_stt_eligible,
        "strict_stt": strict_stt,
        "preferred_stt": preferred_stt,
        "high_trust_reference": high_trust_reference,
        "exceptional_reference": exceptional_reference,
    }


def _decision(level: int, current: dict[str, Any], probe: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    facts = _clock_facts(current, probe)
    if level <= 1:
        return False, "production_control", facts
    if not bool(facts.get("raw_stt_eligible")) or not _probe_candidate_output(probe):
        return False, "stt_raw_candidate_not_eligible", facts

    if level >= 2 and not facts["current_accepted"]:
        return True, "rejected_result_rescue", facts
    if level >= 3 and facts["suspicious"]:
        return True, "suspicious_clock_rescue", facts
    if level >= 4 and facts["audio_only"]:
        return True, "audio_engine_competitor", facts
    if level >= 5 and facts["weak_reference"]:
        return True, "weak_embedded_reference_competitor", facts
    if level >= 6 and facts["semantic_advantage"] and not facts["high_trust_reference"]:
        return True, "semantic_score_advantage", facts
    if level >= 7 and facts["strict_stt"] and not facts["high_trust_reference"]:
        return True, "strict_peer_candidate", facts
    if level >= 8 and facts["preferred_stt"] and not facts["exceptional_reference"]:
        return True, "preferred_semantic_clock", facts
    if level >= 9:
        return True, "primary_accepted_text_clock", facts
    return False, "policy_gate_kept_current", facts


def _stt_run_from_current(current: dict[str, Any], probe: dict[str, Any], level: int, reason: str, facts: dict[str, Any]) -> dict[str, Any]:
    result = {
        "reason": "applied",
        "accepted": True,
        "sync_was_successful": True,
        "engine": "japanese-stt+text-clock",
        "output": str(_probe_candidate_output(probe)),
        "selection_reason": f"stt_policy_{level}:{reason}",
        "stt_policy_level": level,
        "stt_policy_key": POLICY_BY_LEVEL[level]["key"],
        "stt_policy_reason": reason,
        "stt_policy_facts": facts,
        "stt_text_alignment": probe,
    }
    accepted, quality_reason = subtitle_quality_accepted(result)
    return {
        "elapsed_seconds": current.get("elapsed_seconds"),
        "accepted": bool(accepted),
        "quality_reason": quality_reason,
        "selected_name": current.get("selected_name"),
        "selected_path": current.get("selected_path"),
        "final_path": str(_probe_candidate_output(probe)),
        "result": result,
        "stt_influenced_result": True,
        "stt_policy_reason": reason,
    }


def _policy_runs(off: dict[str, Any], current: dict[str, Any], probe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {
        "p0": deepcopy(off),
        "p1": deepcopy(current),
    }
    runs["p0"]["stt_influenced_result"] = False
    runs["p1"]["stt_influenced_result"] = bool(
        "text-clock" in str((current.get("result") or {}).get("engine") or "")
        or "text_clock" in str((current.get("result") or {}).get("selection_reason") or "")
    )
    for level in range(2, 10):
        use_stt, reason, facts = _decision(level, current, probe)
        if use_stt:
            runs[f"p{level}"] = _stt_run_from_current(current, probe, level, reason, facts)
        else:
            row = deepcopy(current)
            row["stt_influenced_result"] = False
            row["stt_policy_reason"] = reason
            row["stt_policy_facts"] = facts
            runs[f"p{level}"] = row
    return runs


def _same_run(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if bool(a.get("accepted")) != bool(b.get("accepted")):
        return False
    left = str(a.get("final_path") or "")
    right = str(b.get("final_path") or "")
    timing = ab._timing_diff(left, right)
    if timing.get("comparable"):
        return bool(timing.get("same_structure", True)) and int(timing.get("changed_cues") or 0) == 0
    ar = a.get("result") if isinstance(a.get("result"), dict) else {}
    br = b.get("result") if isinstance(b.get("result"), dict) else {}
    return (a.get("selected_name"), ar.get("engine"), ar.get("reason")) == (
        b.get("selected_name"), br.get("engine"), br.get("reason")
    )


def _case_frontier(runs: dict[str, dict[str, Any]]) -> int | None:
    current = runs["p1"]
    for level in range(2, 10):
        if not _same_run(current, runs[f"p{level}"]):
            return level
    return None


def _oracle(path: str | None, oracle: Path | None) -> dict[str, Any] | None:
    return ab._oracle_report(path, oracle)


def _oracle_delta(base: dict[str, Any] | None, variant: dict[str, Any] | None) -> str:
    if not base or not variant or not base.get("evaluable") or not variant.get("evaluable"):
        return "unknown"
    base_good, variant_good = bool(base.get("good_alignment")), bool(variant.get("good_alignment"))
    if variant_good and not base_good:
        return "improved"
    if base_good and not variant_good:
        return "worse"
    bp = _float(base.get("p90_abs_error_seconds"))
    vp = _float(variant.get("p90_abs_error_seconds"))
    if bp - vp >= 0.25:
        return "improved"
    if vp - bp >= 0.25:
        return "worse"
    return "same"


def _write_policy_summary(path: Path, summary_rows: list[dict[str, Any]]) -> None:
    fields = [
        "level", "key", "title", "cases", "accepted", "stt_selected", "changed_vs_current",
        "oracle_improved", "oracle_worse", "oracle_same", "oracle_unknown",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)


def _write_frontier_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "kind", "title", "media_id", "episode", "video", "stt_available", "stt_accepted",
        "stt_raw_eligible", "stt_production_gate_accepted", "stt_reject_reason", "stt_gate_failures", "stt_score", "stt_coverage", "stt_activity",
        "stt_anchor_count", "stt_segment_count", "stt_shift_spread_seconds",
        "stt_audio_stream_index", "stt_audio_language", "stt_audio_title", "stt_audio_selection_reason",
        "video_content_fingerprint", "audio_sha256", "reference_sha256", "source_sha256",
        "current_engine", "current_accepted", "first_policy_level", "first_policy_key", "first_policy_reason",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            probe = row["stt_probe"]
            current = row["policies"]["p1"]
            facts = row.get("facts") or {}
            level = row.get("first_policy_level")
            reason = None
            if level is not None:
                reason = row["policies"][f"p{level}"].get("stt_policy_reason")
            writer.writerow({
                "kind": row.get("kind"), "title": row.get("title"), "media_id": row.get("media_id"),
                "episode": row.get("episode"), "video": row.get("video"),
                "stt_available": probe.get("available"), "stt_accepted": probe.get("accepted"),
                "stt_raw_eligible": facts.get("raw_stt_eligible"),
                "stt_production_gate_accepted": facts.get("production_gate_accepted"),
                "stt_reject_reason": probe.get("reject_reason") or probe.get("reason"),
                "stt_gate_failures": json.dumps(probe.get("gate_failures") or [], ensure_ascii=False),
                "stt_score": facts.get("stt_score"), "stt_coverage": facts.get("stt_coverage"),
                "stt_activity": facts.get("stt_activity"),
                "stt_anchor_count": probe.get("anchor_count") or probe.get("matched_anchor_count"),
                "stt_segment_count": probe.get("segment_count"),
                "stt_shift_spread_seconds": probe.get("shift_spread_seconds"),
                "stt_audio_stream_index": ((probe.get("stt") or {}).get("audio_stream") or {}).get("index"),
                "stt_audio_language": ((probe.get("stt") or {}).get("audio_stream") or {}).get("language"),
                "stt_audio_title": ((probe.get("stt") or {}).get("audio_stream") or {}).get("title"),
                "stt_audio_selection_reason": ((probe.get("stt") or {}).get("audio_stream") or {}).get("selection_reason"),
                "video_content_fingerprint": (probe.get("stt") or {}).get("video_content_fingerprint"),
                "audio_sha256": (probe.get("stt") or {}).get("audio_sha256"),
                "reference_sha256": probe.get("reference_sha256") or (probe.get("stt") or {}).get("reference_sha256"),
                "source_sha256": probe.get("source_sha256"),
                "current_engine": facts.get("engine") or (current.get("result") or {}).get("engine"),
                "current_accepted": current.get("accepted"),
                "first_policy_level": level,
                "first_policy_key": POLICY_BY_LEVEL[level]["key"] if level is not None else None,
                "first_policy_reason": reason,
            })


def _interleave_case_kinds(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mix local and benchmark/stress cases so short runs exercise both populations."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(str(case.get("kind") or "other"), []).append(case)
    preferred = [key for key in ("benchmark", "library") if buckets.get(key)]
    preferred.extend(key for key in buckets if key not in preferred)
    if len(preferred) <= 1:
        return list(cases)
    out: list[dict[str, Any]] = []
    positions = {key: 0 for key in preferred}
    while True:
        added = False
        for key in preferred:
            pos = positions[key]
            rows = buckets[key]
            if pos < len(rows):
                out.append(rows[pos])
                positions[key] = pos + 1
                added = True
        if not added:
            break
    return out


def _checkpoint(output: Path, results: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> None:
    (output / "partial-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "case-status.json").write_text(json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8")
    if results:
        _write_frontier_csv(output / "case-frontier.partial.csv", results)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = ab.load_config(args.config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = (args.output or (Path.home() / "Downloads" / f"pudge-subtitle-stt-policy-matrix-{stamp}")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "run.log"

    cases: list[dict[str, Any]] = []
    if not args.benchmark_only:
        cases.extend(ab._local_cases(config))
    if not args.local_only:
        cases.extend(ab._benchmark_cases(config, args.corpus, args.search_root))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for case in cases:
        key = (str(Path(case["video"]).resolve()), tuple(sorted(str(Path(c.path).resolve()) for c in case["candidates"])))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(case)
    cases = _interleave_case_kinds(deduped)
    cases = cases[: args.limit or None]

    results: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    run_started = time.monotonic()
    deadline = (
        run_started + max(0.0, float(args.time_limit_minutes)) * 60.0
        if float(args.time_limit_minutes) > 0
        else None
    )
    stopped_by_time_limit = False
    with log_path.open("w", encoding="utf-8") as log:
        print(f"Cases: {len(cases)}", file=log, flush=True)
        print("Policies:", file=log)
        for policy in POLICIES:
            print(f"  P{policy['level']} {policy['key']}: {policy['meaning']}", file=log)
        for index, case in enumerate(cases, 1):
            if deadline is not None and time.monotonic() >= deadline:
                stopped_by_time_limit = True
                message = (
                    f"TIME LIMIT reached after {time.monotonic() - run_started:.1f}s; "
                    f"writing partial results for {len(results)} completed cases"
                )
                print(message)
                print(message, file=log, flush=True)
                break
            key = ab._case_key(case)
            case_root = output / "cache" / key
            shared_stt = case_root / "shared-stt"
            off_cache = case_root / "p0-off"
            current_cache = case_root / "p1-current"
            probe_cache = case_root / "stt-probe"
            for cache in (off_cache, current_cache, probe_cache):
                ab._link_shared_stt(cache, shared_stt)
            line = f"[{index}/{len(cases)}] {case['kind']} {case.get('title')} E{case.get('episode')} :: {Path(case['video']).name}"
            print(line)
            print(line, file=log, flush=True)

            case_started = time.monotonic()
            remaining = max(0.0, deadline - case_started) if deadline is not None else 0.0
            case_budget = float(args.case_timeout_seconds)
            if remaining > 0 and (case_budget <= 0 or remaining < case_budget):
                case_budget = remaining
            try:
                with _case_deadline(case_budget):
                    off = ab._run_method(case, config, off_cache, text_clock=False, force=args.force)
                    current = ab._run_method(case, config, current_cache, text_clock=True, force=args.force)
                    probe = _probe_stt(case, config, probe_cache, current)
                    runs = _policy_runs(off, current, probe)
                    frontier = _case_frontier(runs)
                    facts = _clock_facts(current, probe)
                    oracles = {f"p{level}": _oracle(runs[f"p{level}"].get("final_path"), case.get("oracle")) for level in range(10)}
                    oracle_vs_current = {f"p{level}": _oracle_delta(oracles["p1"], oracles[f"p{level}"]) for level in range(10)}
                    row = {
                        "id": case["id"], "kind": case["kind"], "title": case.get("title"),
                        "media_id": case.get("media_id"), "episode": case.get("episode"),
                        "video": str(case["video"]), "source": case.get("source"),
                        "candidate_paths": [str(c.path) for c in case["candidates"]],
                        "stt_probe": probe, "facts": facts, "policies": runs,
                        "oracles": oracles, "oracle_vs_current": oracle_vs_current,
                        "first_policy_level": frontier,
                    }
                results.append(row)
                statuses.append({"id": case["id"], "status": "completed", "elapsed_seconds": round(time.monotonic()-case_started, 3)})
                print(
                    f"    STT available={probe.get('available')} production_accept={probe.get('accepted')} "
                    f"raw_eligible={facts.get('raw_stt_eligible')} score={facts.get('stt_score')} "
                    f"coverage={facts.get('stt_coverage')} frontier={frontier}",
                    file=log, flush=True,
                )
            except _CaseTimeout as exc:
                statuses.append({"id": case["id"], "status": "timeout", "elapsed_seconds": round(time.monotonic()-case_started, 3), "error": str(exc)})
                print(f"    TIMEOUT {exc}; continuing", file=log, flush=True)
            except Exception as exc:
                statuses.append({"id": case["id"], "status": "error", "elapsed_seconds": round(time.monotonic()-case_started, 3), "error": f"{type(exc).__name__}: {exc}"})
                print(f"    ERROR {type(exc).__name__}: {exc}; continuing", file=log, flush=True)
            _checkpoint(output, results, statuses)

    summary_rows: list[dict[str, Any]] = []
    for level in range(10):
        key = f"p{level}"
        policy = POLICY_BY_LEVEL[level]
        oracle_counts = {name: 0 for name in ("improved", "worse", "same", "unknown")}
        for row in results:
            oracle_counts[row["oracle_vs_current"][key]] += 1
        summary_rows.append({
            "level": level,
            "key": policy["key"],
            "title": policy["title"],
            "cases": len(results),
            "accepted": sum(1 for row in results if row["policies"][key].get("accepted")),
            "stt_selected": sum(1 for row in results if row["policies"][key].get("stt_influenced_result")),
            "changed_vs_current": sum(1 for row in results if not _same_run(row["policies"]["p1"], row["policies"][key])),
            "oracle_improved": oracle_counts["improved"],
            "oracle_worse": oracle_counts["worse"],
            "oracle_same": oracle_counts["same"],
            "oracle_unknown": oracle_counts["unknown"],
        })

    frontier_counts = {
        str(level): sum(1 for row in results if row.get("first_policy_level") == level)
        for level in range(2, 10)
    }
    frontier_counts["none"] = sum(1 for row in results if row.get("first_policy_level") is None)
    summary = {
        "schema": "pudge-subtitle-stt-policy-matrix-v1",
        "created_at": time.time(),
        "cases": len(results),
        "cases_discovered": len(cases),
        "partial": bool(stopped_by_time_limit),
        "stopped_by_time_limit": bool(stopped_by_time_limit),
        "requested_time_limit_minutes": float(args.time_limit_minutes),
        "elapsed_seconds": round(time.monotonic() - run_started, 3),
        "benchmark_identity_rejected": len(ab.BENCHMARK_IDENTITY_REJECTIONS),
        "stt_available": sum(1 for row in results if row["stt_probe"].get("available")),
        "stt_accepted": sum(1 for row in results if row["stt_probe"].get("accepted")),
        "stt_raw_eligible": sum(1 for row in results if (row.get("facts") or {}).get("raw_stt_eligible")),
        "case_timeouts": sum(1 for row in statuses if row.get("status") == "timeout"),
        "case_errors": sum(1 for row in statuses if row.get("status") == "error"),
        "case_timeout_seconds": float(args.case_timeout_seconds),
        "frontier_counts": frontier_counts,
        "policies": list(POLICIES),
        "policy_summary": summary_rows,
        "note": (
            "P0/P1 are real production runs. P2-P9 are experimental offline arbitration over one shared raw semantic "
            "STT candidate that passes score/coverage/activity minima even when the production transition safety gate rejects it. "
            "This intentionally measures policy authority separately from the production gate; production behavior is unchanged."
        ),
        "output": str(output),
    }

    (output / "all-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "case-status.json").write_text(json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "benchmark-identity-rejections.json").write_text(
        json.dumps(ab.BENCHMARK_IDENTITY_REJECTIONS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_policy_summary(output / "policy-summary.csv", summary_rows)
    _write_frontier_csv(output / "case-frontier.csv", results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Log: {log_path}")
    print(f"Policy summary: {output / 'policy-summary.csv'}")
    print(f"Case frontier: {output / 'case-frontier.csv'}")
    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", "-R", str(output / "policy-summary.csv")], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
