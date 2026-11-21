"""Reading speed needs at least 5 minutes of reading in a day/week before it shows a rate,
and the average is all characters over all reading time (not a mean of daily rates)."""

from __future__ import annotations

import pytest

from pudge.consumption_statistics import _MIN_RATE_SECONDS, ConsumptionStatistics
from test_feature_reading_speed_weekly_load import _library, _local, _reading


def test_floor_is_five_minutes():
    assert _MIN_RATE_SECONDS == 300.0


def test_short_days_get_no_rate_and_average_weights_by_characters(tmp_path):
    db, ledger, book, _other, _audio = _library(tmp_path)
    _reading(ledger, book, _local(2026, 9, 28, 8), [(30, 150)] * 8)  # 4 min, 1200 chars
    _reading(ledger, book, _local(2026, 9, 29, 8), [(30, 50)] * 20)  # 10 min, 1000 chars
    _reading(ledger, book, _local(2026, 9, 30, 8), [(30, 300)] * 40)  # 20 min, 12000 chars
    result = ConsumptionStatistics(db).query(
        {"period": "custom", "start_date": "2026-09-28", "end_date": "2026-09-30"},
        now=_local(2026, 10, 9, 12),
    )
    days = {row["day"]: row for row in result["reading_speed"]["days"]}
    assert days["2026-09-28"]["cpm"] is None  # under 5 minutes
    assert days["2026-09-29"]["cpm"] == pytest.approx(100.0)
    assert days["2026-09-30"]["cpm"] == pytest.approx(600.0)
    # 14200 characters in 34 minutes; a mean of the daily rates would be 350.
    assert result["reading_speed"]["total"]["cpm"] == pytest.approx(14200 / 34)
