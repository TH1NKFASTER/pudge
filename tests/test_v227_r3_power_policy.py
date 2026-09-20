from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig, load_config, write_config
from pudge.power_policy import PowerPolicy
from pudge.work_scheduler import WorkPriority, WorkScheduler


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def monotonic(self) -> float:
        return self.value

    def wall(self) -> float:
        return 1_700_000_000.0 + self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class Pmset:
    def __init__(self, *, percent: int = 80, on_battery: bool = True, thermal: bool = False) -> None:
        self.percent = percent
        self.on_battery = on_battery
        self.thermal = thermal
        self.fail_battery = False
        self.fail_thermal = False
        self.calls = 0

    def __call__(self, args, **_kwargs):
        self.calls += 1
        if args[-1] == "batt":
            if self.fail_battery:
                raise OSError("pmset battery unavailable")
            source = "Battery Power" if self.on_battery else "AC Power"
            return SimpleNamespace(stdout=f"Now drawing from '{source}'\n -InternalBattery-0 ({self.percent}%)")
        if self.fail_thermal:
            raise OSError("pmset thermal unavailable")
        limit = 80 if self.thermal else 100
        return SimpleNamespace(stdout=f"CPU_Speed_Limit = {limit}\nGPU_Speed_Limit = {limit}\n")


def policy(pmset: Pmset, clock: Clock, *, manual: bool = False, auto: bool = True) -> PowerPolicy:
    return PowerPolicy(
        manual_enabled=manual,
        auto_enabled=auto,
        run_command=pmset,
        platform="darwin",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )


def test_auto_enters_below_20_not_at_20() -> None:
    clock, pmset = Clock(), Pmset(percent=21)
    p = policy(pmset, clock)
    assert p.snapshot(refresh=True).mode == "normal"
    pmset.percent = 20
    assert p.snapshot(refresh=True).mode == "normal"
    pmset.percent = 19
    snap = p.snapshot(refresh=True)
    assert snap.mode == "energy_saving"
    assert snap.auto_latched is True
    assert "low_battery" in snap.reasons


def test_auto_hysteresis_19_to_21_to_25() -> None:
    clock, pmset = Clock(), Pmset(percent=19)
    p = policy(pmset, clock)
    assert p.snapshot(refresh=True).mode == "energy_saving"
    for percent in (21, 24):
        pmset.percent = percent
        assert p.snapshot(refresh=True).mode == "energy_saving"
    pmset.percent = 25
    snap = p.snapshot(refresh=True)
    assert snap.mode == "normal"
    assert snap.auto_latched is False


def test_ac_clears_only_auto_latch_manual_survives() -> None:
    clock, pmset = Clock(), Pmset(percent=19)
    p = policy(pmset, clock)
    assert p.snapshot(refresh=True).auto_latched
    pmset.on_battery = False
    assert p.snapshot(refresh=True).mode == "normal"

    p.update_settings(manual_enabled=True, auto_enabled=True)
    snap = p.snapshot(refresh=True)
    assert snap.mode == "energy_saving"
    assert snap.auto_latched is False
    assert snap.reasons[0] == "manual"


def test_auto_off_removes_auto_reason_but_not_manual() -> None:
    clock, pmset = Clock(), Pmset(percent=10)
    p = policy(pmset, clock, manual=True, auto=True)
    assert p.snapshot(refresh=True).auto_latched
    p.update_settings(manual_enabled=True, auto_enabled=False)
    snap = p.snapshot(refresh=True)
    assert snap.mode == "energy_saving"
    assert snap.auto_latched is False
    assert "low_battery" not in snap.reasons
    assert "manual" in snap.reasons


def test_unknown_uses_last_good_briefly_then_does_not_invent_low_battery() -> None:
    clock, pmset = Clock(), Pmset(percent=19)
    p = policy(pmset, clock)
    assert p.snapshot(refresh=True).mode == "energy_saving"
    pmset.fail_battery = True
    clock.advance(31)
    stale = p.snapshot(refresh=True)
    assert stale.stale is True
    assert stale.on_battery is True
    assert stale.battery_percent == 19
    assert stale.mode == "energy_saving"

    clock.advance(PowerPolicy.LAST_GOOD_TTL_SECONDS + 1)
    expired = p.snapshot(refresh=True)
    assert expired.stale is True
    assert expired.on_battery is None
    assert expired.battery_percent is None
    assert expired.mode == "normal"
    assert "unknown" in expired.reasons


def test_thermal_is_independent_of_battery_auto_toggle() -> None:
    clock, pmset = Clock(), Pmset(percent=80, thermal=True)
    p = policy(pmset, clock, auto=False)
    snap = p.snapshot(refresh=True)
    assert snap.mode == "normal"
    assert snap.thermal_limited is True
    assert p.background_block_reason(resource="cpu") == "thermal"
    assert p.background_block_reason(resource="io") is None


def test_scheduler_user_and_playback_priority_bypass_energy_saving_background_does_not(
    tmp_path: Path,
) -> None:
    clock, pmset = Clock(), Pmset(percent=10)
    p = policy(pmset, clock)
    p.snapshot(refresh=True)
    scheduler = WorkScheduler(tmp_path, power_policy=p)
    assert scheduler.background_block_reason() == "energy_saving"
    assert scheduler.background_allowed() is False
    assert scheduler.background_allowed(priority=WorkPriority.USER) is True
    assert scheduler.background_allowed(priority=WorkPriority.PLAYBACK) is True
    lease = scheduler.acquire_heavy(
        "user-explicit",
        blocking=False,
        priority=WorkPriority.USER,
        foreground_sensitive=True,
    )
    assert lease is not None
    lease.release()



def test_scheduler_admission_log_uses_exact_power_reason(tmp_path: Path) -> None:
    clock, pmset = Clock(), Pmset(percent=10)
    p = policy(pmset, clock)
    p.snapshot(refresh=True)

    rows: list[tuple[str, tuple[object, ...]]] = []

    class Logger:
        def info(self, message: str, *args: object) -> None:
            rows.append((message, args))

    scheduler = WorkScheduler(tmp_path, power_policy=p, logger=Logger())
    assert scheduler.acquire_heavy("background-ocr", blocking=False) is None
    rendered = [message % args for message, args in rows]
    assert any("reason=energy_saving" in row for row in rendered)


def test_fresh_policy_after_restart_enters_immediately_at_19_percent() -> None:
    clock, pmset = Clock(), Pmset(percent=19)
    first = policy(pmset, clock)
    assert first.snapshot(refresh=True).mode == "energy_saving"

    restarted = policy(pmset, clock)
    snap = restarted.snapshot(refresh=True)
    assert snap.mode == "energy_saving"
    assert snap.auto_latched is True

def test_two_process_policies_have_same_config_and_independent_latches() -> None:
    clock = Clock()
    pmset_a, pmset_b = Pmset(percent=19), Pmset(percent=19)
    first = policy(pmset_a, clock)
    second = policy(pmset_b, clock)
    assert first.snapshot(refresh=True).mode == "energy_saving"
    assert second.snapshot(refresh=True).mode == "energy_saving"
    pmset_a.on_battery = False
    assert first.snapshot(refresh=True).mode == "normal"
    assert second.snapshot(refresh=True).mode == "energy_saving"


def test_power_config_roundtrip_and_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = AppConfig()
    assert cfg.power.manual_energy_saving is False
    assert cfg.power.auto_battery_energy_saving is True
    cfg.power.manual_energy_saving = True
    cfg.power.auto_battery_energy_saving = False
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.power.manual_energy_saving is True
    assert loaded.power.auto_battery_energy_saving is False


def test_power_ui_contract_present() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="s_power_manual"' in html
    assert 'id="s_power_auto"' in html
    assert "power_manual_energy_saving:c('s_power_manual')" in html
    assert "power_auto_battery_energy_saving:c('s_power_auto')" in html
    assert "pywebview.api.power_status" in html
    assert "energy-saving" in html
    assert "if(!ui.windowActive||ui.page!=='current')return;" in html
    assert "if(energySavingActive())return;" in html


def test_scheduler_background_allowed_override_remains_admission_seam(
    tmp_path: Path, monkeypatch
) -> None:
    clock, pmset = Clock(), Pmset(percent=10)
    scheduler = WorkScheduler(tmp_path, power_policy=policy(pmset, clock))

    # Existing manager/tests intentionally override background_allowed() when a
    # higher layer has already made the admission decision.  That override must
    # suppress battery/thermal subprocess probes all the way through heavy-work
    # acquisition rather than being bypassed by a direct PowerPolicy call.
    monkeypatch.setattr(scheduler, "background_allowed", lambda **_kwargs: True)

    assert scheduler.background_wait_reason() is None
    lease = scheduler.acquire_heavy("override-seam", blocking=False)
    assert lease is not None
    lease.release()
    assert pmset.calls == 0


def test_scheduler_wait_reason_preserves_precise_power_reason(tmp_path: Path) -> None:
    clock, pmset = Clock(), Pmset(percent=10)
    scheduler = WorkScheduler(tmp_path, power_policy=policy(pmset, clock))

    assert scheduler.background_wait_reason() == "energy_saving"
    assert pmset.calls == 2
