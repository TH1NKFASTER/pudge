from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class PowerSnapshot:
    mode: str
    reasons: tuple[str, ...]
    on_battery: bool | None
    battery_percent: int | None
    observed_at: float
    stale: bool
    revision: int
    thermal_limited: bool
    manual_enabled: bool
    auto_enabled: bool
    auto_latched: bool

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


class PowerPolicy:
    """Cached battery/thermal policy shared by GUI and launch-agent schedulers.

    PowerPolicy owns resource observation and low-battery hysteresis. WorkScheduler
    remains responsible for foreground admission, priority ordering, and the
    cross-process heavy-work lock.
    """

    ENTER_PERCENT = 20
    EXIT_PERCENT = 25
    CACHE_SECONDS = 30.0
    LAST_GOOD_TTL_SECONDS = 120.0

    def __init__(
        self,
        *,
        manual_enabled: bool = False,
        auto_enabled: bool = True,
        run_command: Callable[..., Any] | None = None,
        platform: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.manual_enabled = bool(manual_enabled)
        self.auto_enabled = bool(auto_enabled)
        self._run_command = run_command
        self._platform = str(platform or sys.platform)
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._auto_latched = False
        self._cache_at = 0.0
        self._cache: PowerSnapshot | None = None
        self._last_good_battery_at = 0.0
        self._last_good_battery: tuple[bool, int | None] | None = None
        self._last_good_thermal_at = 0.0
        self._last_good_thermal: bool | None = None
        self._revision = 0
        self._semantic: tuple[Any, ...] | None = None

    def update_settings(self, *, manual_enabled: bool, auto_enabled: bool) -> None:
        changed = (
            self.manual_enabled != bool(manual_enabled)
            or self.auto_enabled != bool(auto_enabled)
        )
        self.manual_enabled = bool(manual_enabled)
        self.auto_enabled = bool(auto_enabled)
        if not self.auto_enabled:
            self._auto_latched = False
        if changed:
            self._cache_at = 0.0

    def _run(self, args: list[str]) -> str:
        if self._run_command is None:
            return ""
        completed = self._run_command(
            args,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        return str(getattr(completed, "stdout", "") or "")

    def _observe(self) -> tuple[bool | None, int | None, bool | None, bool]:
        if self._platform != "darwin":
            return False, None, False, True

        now = self._monotonic()
        battery_ok = False
        thermal_ok = False
        on_battery: bool | None = None
        percent: int | None = None
        thermal: bool | None = None

        try:
            battery = self._run(["pmset", "-g", "batt"])
            match = re.search(r"(\d+)%", battery)
            percent = int(match.group(1)) if match else None
            if "Battery Power" in battery:
                on_battery = True
                battery_ok = True
            elif "AC Power" in battery:
                on_battery = False
                battery_ok = True
            if battery_ok:
                self._last_good_battery = (bool(on_battery), percent)
                self._last_good_battery_at = now
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            pass

        try:
            thermal_text = self._run(["pmset", "-g", "therm"])
            limits = [
                int(value)
                for value in re.findall(
                    r"(?:CPU|GPU)_Speed_Limit\s*=\s*(\d+)", thermal_text
                )
            ]
            thermal = bool(limits and min(limits) < 100)
            thermal_ok = True
            self._last_good_thermal = bool(thermal)
            self._last_good_thermal_at = now
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            pass

        if (
            not battery_ok
            and self._last_good_battery is not None
            and now - self._last_good_battery_at <= self.LAST_GOOD_TTL_SECONDS
        ):
            on_battery, percent = self._last_good_battery
        if (
            not thermal_ok
            and self._last_good_thermal is not None
            and now - self._last_good_thermal_at <= self.LAST_GOOD_TTL_SECONDS
        ):
            thermal = self._last_good_thermal

        if not battery_ok and (
            self._last_good_battery is None
            or now - self._last_good_battery_at > self.LAST_GOOD_TTL_SECONDS
        ):
            on_battery, percent = None, None
        if not thermal_ok and (
            self._last_good_thermal is None
            or now - self._last_good_thermal_at > self.LAST_GOOD_TTL_SECONDS
        ):
            thermal = None

        return on_battery, percent, thermal, battery_ok and thermal_ok

    def snapshot(self, *, refresh: bool = False) -> PowerSnapshot:
        now = self._monotonic()
        if (
            not refresh
            and self._cache is not None
            and now - self._cache_at < self.CACHE_SECONDS
        ):
            return self._cache

        on_battery, percent, thermal, fresh = self._observe()
        stale = not fresh

        if not self.auto_enabled:
            self._auto_latched = False
        elif on_battery is False:
            self._auto_latched = False
        elif on_battery is True and percent is not None:
            if percent < self.ENTER_PERCENT:
                self._auto_latched = True
            elif percent >= self.EXIT_PERCENT:
                self._auto_latched = False
            # 20..24 intentionally preserves the previous latch state.
        elif stale:
            battery_last_good_valid = (
                self._last_good_battery is not None
                and now - self._last_good_battery_at <= self.LAST_GOOD_TTL_SECONDS
            )
            if not battery_last_good_valid:
                # Unknown long-term status must not manufacture an automatic
                # low-battery block indefinitely.
                self._auto_latched = False

        energy_saving = self.manual_enabled or self._auto_latched
        reasons: list[str] = []
        if self.manual_enabled:
            reasons.append("manual")
        if self._auto_latched:
            reasons.append("low_battery")
        if thermal is True:
            reasons.append("thermal")
        if stale:
            reasons.append("unknown")

        semantic = (
            energy_saving,
            tuple(reasons),
            on_battery,
            percent,
            thermal,
            self.manual_enabled,
            self.auto_enabled,
            self._auto_latched,
            stale,
        )
        if semantic != self._semantic:
            self._revision += 1
            self._semantic = semantic

        snapshot = PowerSnapshot(
            mode="energy_saving" if energy_saving else "normal",
            reasons=tuple(reasons),
            on_battery=on_battery,
            battery_percent=percent,
            observed_at=self._wall_time(),
            stale=stale,
            revision=self._revision,
            thermal_limited=bool(thermal),
            manual_enabled=self.manual_enabled,
            auto_enabled=self.auto_enabled,
            auto_latched=self._auto_latched,
        )
        self._cache_at = now
        self._cache = snapshot
        return snapshot

    def background_block_reason(self, *, resource: str = "cpu") -> str | None:
        snapshot = self.snapshot()
        if resource in {"cpu", "gpu"} and snapshot.thermal_limited:
            return "thermal"
        if snapshot.mode == "energy_saving":
            return "energy_saving"
        return None
