from pudge.syncing import _local_speech_shift_estimate


def test_monotonic_matching_finds_ordered_cold_open_shift() -> None:
    starts = [10.0, 16.0, 23.0, 31.0, 40.0, 48.0]
    reference = [15.6, 21.6, 28.6, 36.6, 45.6, 53.6]
    result = _local_speech_shift_estimate(starts, reference)
    assert result["accepted"] is True
    assert result["matching"] == "monotonic_one_to_one"
    assert abs(float(result["shift_seconds"]) - 5.6) <= 0.11


def test_monotonic_matching_does_not_reuse_one_whisper_segment() -> None:
    starts = [10.0, 10.2, 10.4, 10.6, 10.8]
    reference = [11.0, 20.0, 30.0, 40.0]
    result = _local_speech_shift_estimate(starts, reference, max_shift_seconds=2.0)
    best = result["best"]
    assert int(best["matched"]) <= 1
