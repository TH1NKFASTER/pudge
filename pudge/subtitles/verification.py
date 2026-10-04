"""Operational speech verification failures, separate from candidate rejection."""
from collections.abc import Mapping

REASONS = {
    "stt_reference_unavailable", "stt_timeout", "stt_unavailable",
    "audio_extract_failed", "japanese_audio_stream_unavailable",
    "stt_invalid_result", "stt_too_few_segments", "stt_disabled",
    "stt_worker_failed",
}


def verification_failure(result):
    """Keep the worker's precise reason even inside a generic STT wrapper."""
    if not isinstance(result, Mapping):
        return None
    found = []
    def visit(value):
        if isinstance(value, Mapping):
            reason = str(value.get("reason") or "")
            if reason in REASONS:
                found.append((reason, str(value.get("error") or "")[-1000:]))
            for nested in value.values():
                if isinstance(nested, (Mapping, list)):
                    visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(result)
    if not found:
        return None
    reason, error = next((row for row in found if row[0] != "stt_reference_unavailable"), next((row for row in reversed(found) if row[1]), found[0]))
    return {"reason": reason, "retryable": reason not in {"japanese_audio_stream_unavailable", "stt_disabled", "stt_unavailable"}, "error": error}


def annotate_verification(result):
    if not result.get("sync_was_successful"):
        failure = verification_failure(result)
        if failure:
            result["verification_failure"] = failure
    return result


def unchanged_candidates_can_skip(previous, current, outcome):
    return bool(previous and previous == current and outcome == "terminal")
