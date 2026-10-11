"""Tests run as on mains power even on a Mac on battery or in Low Power Mode (Mac-only failures)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from pudge import power_policy
from pudge.work_scheduler import WorkScheduler


def test_the_app_scheduler_does_not_read_the_host_battery_in_tests(tmp_path, monkeypatch) -> None:
    # As on a Mac on battery at 5 % with Low Power Mode on: the scheduler passes the real
    # platform, exactly like the app.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(power_policy.PowerPolicy, "_native_power_state", staticmethod(lambda: (True, True)))
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="Now drawing from 'Battery Power'\n 5%; discharging"),
    )
    scheduler = WorkScheduler(tmp_path)
    assert scheduler.background_wait_reason() is None


def test_power_tests_still_model_a_mac() -> None:
    policy = power_policy.PowerPolicy(
        platform="darwin",
        run_command=lambda *a, **k: SimpleNamespace(
            stdout="Now drawing from 'Battery Power'\n 5%; discharging"
        ),
        native_probe=lambda: (False, False),
    )
    assert policy.snapshot(refresh=True).mode == "energy_saving"
