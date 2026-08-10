from pathlib import Path

from pudge.syncing import _best_reference_discontinuity_rejection


def test_failed_reference_result_can_drive_fast_reject() -> None:
    failed = [
        {
            "output": "/tmp/bad.srt",
            "alignment_score": 123.0,
            "reference_discontinuity_rejected": True,
        }
    ]
    items = [(Path(str(item.get("output") or "fallback.srt")), item) for item in failed]
    selected = _best_reference_discontinuity_rejection(items)
    assert selected is not None
    path, result = selected
    assert path == Path("/tmp/bad.srt")
    assert result["reference_discontinuity_rejected"] is True
