from __future__ import annotations

from pudge.manager import _subtitle_source_retry_delay_seconds


def test_finished_media_uses_daily_subtitle_source_backoff() -> None:
    assert _subtitle_source_retry_delay_seconds(
        poll_minutes=10,
        media_status="FINISHED",
    ) == 24 * 3600


def test_airing_and_unknown_media_keep_short_subtitle_poll() -> None:
    assert _subtitle_source_retry_delay_seconds(
        poll_minutes=10,
        media_status="RELEASING",
    ) == 10 * 60
    assert _subtitle_source_retry_delay_seconds(
        poll_minutes=10,
        media_status=None,
    ) == 10 * 60


def test_finished_backoff_respects_a_larger_explicit_minimum() -> None:
    assert _subtitle_source_retry_delay_seconds(
        poll_minutes=10,
        media_status="FINISHED",
        minimum_seconds=48 * 3600,
    ) == 48 * 3600
