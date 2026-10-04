"""Conservative constant-clock recovery from independent Japanese word anchors."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any

from ..alignment_guard import _write_exact_srt, validate_alignment_output
from ..subtitle_formats import parse_srt

_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _normalize(text: str) -> str:
    # Multi-language tracks may have a full English translation on another line.
    lines = [line for line in str(text).splitlines() if _JAPANESE.search(line)]
    return "".join(ch for ch in unicodedata.normalize("NFKC", "".join(lines)) if ch.isalnum())


def recover_constant_speech_clock(
    source: Path,
    transcript: Path,
    cache_dir: Path,
) -> tuple[Path | None, dict[str, Any]]:
    """Shift the whole source only when many distinct words agree across time.

    Does not infer missing speech, alter text/duration, or relax the output guard.
    Exact anchors deliberately sacrifice recall to avoid fuzzy false positives.
    """
    cues = parse_srt(source)
    payload = json.loads(transcript.read_text(encoding="utf-8"))
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    chars: list[str] = []
    starts: list[float] = []
    probabilities: list[float] = []
    word_lengths: list[float] = []
    segment_ids: list[int] = []
    last_end = -math.inf
    for segment_id, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        metrics = segment.get("metrics") if isinstance(segment.get("metrics"), dict) else segment
        flags = segment.get("review_flags") or []
        bad = (
            any(flag in flags for flag in ("possible_repetition", "low_log_probability"))
            or float(metrics.get("no_speech_prob", 0) or 0) > 0.90
            or float(metrics.get("avg_logprob", 0) or 0) < -1.0
            or float(metrics.get("compression_ratio", 0) or 0) > 2.4
        )
        words = segment.get("words") or []
        if bad or not isinstance(words, list):
            chars.append("|")
            starts.append(0)
            probabilities.append(0)
            word_lengths.append(0)
            segment_ids.append(-1)
            continue
        for word in words:
            if not isinstance(word, dict):
                continue
            normalized = unicodedata.normalize("NFKC", str(word.get("word") or ""))
            normalized = "".join(ch for ch in normalized if ch.isalnum())
            try:
                start, end = float(word["start"]), float(word["end"])
                probability = float(word.get("probability", 0) or 0)
            except (TypeError, ValueError, KeyError):
                continue
            if not normalized or not all(math.isfinite(v) for v in (start, end, probability)) or end < start:
                continue
            if start - last_end > 5.0 or start < last_end - 0.5:
                chars.append("|")
                starts.append(0)
                probabilities.append(0)
                word_lengths.append(0)
                segment_ids.append(-1)
            for i, ch in enumerate(normalized):
                chars.append(ch)
                starts.append(start + (end - start) * i / len(normalized))
                probabilities.append(probability)
                word_lengths.append(end - start)
                segment_ids.append(segment_id)
            last_end = end
    asr = "".join(chars)
    # Touching repeats often represent a single caption broken by extraction.
    grouped: list[dict[str, Any]] = []
    for index, (start, end, text) in enumerate(cues):
        normalized = _normalize(text)
        if grouped and grouped[-1]["text"] == normalized and abs(start - grouped[-1]["end"]) <= 0.05:
            grouped[-1]["end"] = max(end, grouped[-1]["end"])
        else:
            grouped.append({"index": index, "start": start, "end": end, "text": normalized, "original": text})
    counts = collections.Counter(cue["text"] for cue in grouped)
    anchors: list[dict[str, Any]] = []
    for cue in grouped:
        text = cue["text"]
        if (
            len(text) < 6
            or counts[text] != 1
            or not _JAPANESE.search(text)
            or re.fullmatch(r"\s*[（(].*[）)]\s*", cue["original"], re.DOTALL)
        ):
            continue
        pos = asr.find(text)
        if pos < 0 or asr.find(text, pos + 1) >= 0:
            continue
        mean = statistics.mean(probabilities[pos : pos + len(text)])
        # A high mean can hide a very uncertain first word and a 1s onset error.
        if mean < 0.75 or probabilities[pos] < 0.60 or word_lengths[pos] > 0.8:
            continue
        anchors.append(
            {
                "cue_index": cue["index"],
                "subtitle_start": cue["start"],
                "speech_start": starts[pos],
                "shift_seconds": starts[pos] - cue["start"],
                "mean_word_probability": mean,
                "segment_ids": list(dict.fromkeys(segment_ids[pos : pos + len(text)])),
            }
        )
    meta: dict[str, Any] = {
        "attempted": True,
        "accepted": False,
        "version": "speech-constant-v1",
        "reason": "insufficient_distributed_word_anchors",
        "anchor_count": len(anchors),
        "anchors": anchors,
        "is_ground_truth": False,
    }
    if len(anchors) < 16:
        return None, meta
    duration = max(
        [end for _, end, _ in cues] + [float(s.get("end", 0) or 0) for s in segments if isinstance(s, dict)]
    )
    shifts = [a["shift_seconds"] for a in anchors]
    shift = statistics.median(shifts)
    residuals = [abs(value - shift) for value in shifts]
    quarters = collections.Counter(
        min(3, max(0, int(4 * a["speech_start"] / max(duration, 1)))) for a in anchors
    )
    bin_shifts = [
        statistics.median(
            a["shift_seconds"]
            for a in anchors
            if min(3, max(0, int(4 * a["speech_start"] / max(duration, 1)))) == b
        )
        for b in range(4)
        if quarters[b]
    ]
    inlier_fraction = sum(value <= 0.65 for value in residuals) / len(residuals)
    spread = max(bin_shifts) - min(bin_shifts)
    meta.update(
        {
            "shift_seconds": round(shift, 3),
            "inlier_fraction": round(inlier_fraction, 4),
            "quarter_anchor_counts": [quarters[b] for b in range(4)],
            "quarter_shift_spread_seconds": round(spread, 3),
        }
    )
    if (
        any(quarters[b] < 3 for b in range(4))
        or max(a["speech_start"] for a in anchors) - min(a["speech_start"] for a in anchors) < 0.65 * duration
    ):
        return None, meta
    if inlier_fraction < 0.80 or spread > 0.50 or abs(shift) > 30.0:
        meta["reason"] = "speech_clock_not_constant"
        return None, meta
    if any(start + shift < 0.10 for start, _, _ in cues):
        meta["reason"] = "constant_shift_would_clamp_source"
        return None, meta
    key = hashlib.sha256(source.read_bytes() + transcript.read_bytes() + b"speech-constant-v1").hexdigest()[
        :24
    ]
    output = cache_dir / "speech-constant" / f"{key}.srt"
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_exact_srt([(start + shift, end + shift, text) for start, end, text in cues], output)
    guard = validate_alignment_output(source, output)
    meta["alignment_output_guard"] = guard
    if guard.get("status") != "accepted":
        meta["reason"] = "constant_shift_output_guard_failed"
        return None, meta
    meta.update({"accepted": True, "reason": "distributed_exact_word_clock", "output": str(output)})
    return output, meta
