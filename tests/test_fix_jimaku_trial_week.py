"""The bundled Jimaku trial lasts 7 days, and every text says so (was 48 hours)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.jimaku_trial import TRIAL_SECONDS, apply_jimaku_trial
from tests.i18n_support import catalog, en, ru

WEEK = 7 * 24 * 60 * 60


def _config(tmp_path: Path, started_at: float) -> AppConfig:
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.paths.cache_dir.mkdir(parents=True)
    (config.paths.cache_dir / "jimaku-trial.json").write_text(json.dumps({"started_at": started_at}))
    return config


def test_the_trial_is_active_for_a_week(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", "trial-key")
    assert TRIAL_SECONDS == WEEK
    six_days = _config(tmp_path / "a", started_at=1000.0)
    apply_jimaku_trial(six_days, now=1000.0 + 6 * 24 * 3600)
    assert six_days.jimaku.trial_active is True and six_days.jimaku.api_key == "trial-key"
    assert six_days.jimaku.trial_expires_at == 1000.0 + WEEK
    over = _config(tmp_path / "b", started_at=1000.0)
    apply_jimaku_trial(over, now=1000.0 + WEEK + 1)
    assert over.jimaku.trial_active is False and over.jimaku.api_key == ""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_no_text_still_promises_48_hours(lang: str) -> None:
    for key, value in catalog(lang).items():
        values = value.values() if isinstance(value, dict) else [value]
        for text in values:
            assert "48" not in str(text) or "jimaku" not in key.casefold(), (key, text)
    expired = en("jimakuKey.trialExpired") if lang == "en" else ru("jimakuKey.trialExpired")
    assert ("7-day" in expired) if lang == "en" else ("Недельный" in expired)


@pytest.mark.parametrize(
    "lang, remaining, expected",
    [
        ("en", 5 * 24 * 3600 + 3600, "about 5 more days"),
        ("ru", 5 * 24 * 3600 + 3600, "примерно 5 дней"),
        ("en", 10 * 3600, "about 10 more hours"),
    ],
)
def test_the_remaining_trial_is_shown_in_days_when_it_is_long(page, lang, remaining, expected) -> None:
    # The notice counts down to the absolute expiry time against the page's clock.
    now = 1_790_000_000.0
    result = page.run(
        "return jimakuTrialNotice(false);",
        lang=lang,
        now=int(now * 1000),
        state={
            "settings": {
                "jimaku_trial_active": True,
                "jimaku_trial_expires_at": now + remaining,
                "jimaku_trial_remaining_seconds": remaining,
                "jimaku_key_problem": {},
            }
        },
    )
    assert expected in result.value
