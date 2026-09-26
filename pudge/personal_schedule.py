from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RULE_VERSION = 1
RULE_UNITS = frozenset({"hours", "days", "weeks"})


@dataclass(frozen=True, slots=True)
class PersonalReleaseRule:
    version: int = RULE_VERSION
    unit: str = "weeks"
    every: int = 1
    clock: str = "wall"

    @property
    def is_default_weekly(self) -> bool:
        return self.unit == "weeks" and self.every == 1

    def as_dict(self) -> dict[str, object]:
        return {
            "version": int(self.version),
            "unit": self.unit,
            "every": int(self.every),
            "clock": self.clock,
            "custom": not self.is_default_weekly,
        }


@dataclass(frozen=True, slots=True)
class PersonalReleaseItem:
    episode: int
    ordinal: int
    unlock_at_utc: float
    nominal_local_datetime: str
    unlocked: bool = False
    watched_at: float | None = None
    dst_adjusted: bool = False


@dataclass(frozen=True, slots=True)
class PersonalReleaseSchedule:
    schedule_id: int
    anime_id: int
    enabled: bool
    start_local_datetime: str
    timezone_iana: str
    first_episode: int
    revision: int
    rewatch: bool
    items: tuple[PersonalReleaseItem, ...]
    rule: PersonalReleaseRule = PersonalReleaseRule()
    cycle_id: int = 1


class PersonalScheduleRevisionConflict(ValueError):
    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = int(expected_revision)
        self.actual_revision = int(actual_revision)
        super().__init__(
            f"Schedule changed since it was opened (expected revision "
            f"{self.expected_revision}, current {self.actual_revision})"
        )


def validate_timezone(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    if not value:
        raise ValueError("Timezone is required")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc


def parse_local_datetime(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Start date/time is required")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Start date/time must be ISO local date/time") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Schedule interval must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("Schedule interval must be a positive integer")
        result = int(value)
    else:
        raw = str(value or "").strip()
        if not raw or not raw.isascii() or not raw.isdigit():
            raise ValueError("Schedule interval must be a positive integer")
        result = int(raw)
    if result < 1:
        raise ValueError("Schedule interval must be a positive integer")
    return result


def normalize_schedule_rule(
    unit: str | None = None,
    every: Any = 1,
    *,
    version: int = RULE_VERSION,
) -> PersonalReleaseRule:
    normalized_unit = str(unit or "weeks").strip().casefold()
    if normalized_unit not in RULE_UNITS:
        raise ValueError("Schedule unit must be hours, days, or weeks")
    interval = _positive_integer(every)
    if int(version) != RULE_VERSION:
        raise ValueError(f"Unsupported personal schedule rule version: {version}")
    return PersonalReleaseRule(
        version=RULE_VERSION,
        unit=normalized_unit,
        every=interval,
        clock="elapsed" if normalized_unit == "hours" else "wall",
    )


def _roundtrip_matches(local_naive: datetime, zone: ZoneInfo, *, fold: int) -> bool:
    candidate = local_naive.replace(tzinfo=zone, fold=fold)
    roundtrip = candidate.astimezone(UTC).astimezone(zone)
    return roundtrip.replace(tzinfo=None) == local_naive and int(roundtrip.fold) == int(fold)


def resolve_local_datetime(local_naive: datetime, timezone_iana: str) -> datetime:
    """Resolve one wall-clock datetime deterministically in an IANA zone.

    Ambiguous local times use the first occurrence (fold=0). Non-existent local
    times are moved forward minute-by-minute to the first valid wall clock.
    """

    zone = validate_timezone(timezone_iana)
    probe = local_naive.replace(second=0, microsecond=0)
    for _ in range(24 * 60 + 1):
        if _roundtrip_matches(probe, zone, fold=0):
            return probe.replace(tzinfo=zone, fold=0)
        if _roundtrip_matches(probe, zone, fold=1):
            return probe.replace(tzinfo=zone, fold=1)
        probe += timedelta(minutes=1)
    raise ValueError("Could not resolve local date/time in timezone")


def _safe_add(value: datetime, delta: timedelta) -> datetime:
    try:
        return value + delta
    except OverflowError as exc:
        raise ValueError("Schedule interval is too large for the selected start date") from exc


def _safe_delta(**kwargs: int) -> timedelta:
    try:
        return timedelta(**kwargs)
    except OverflowError as exc:
        raise ValueError("Schedule interval is too large for the selected start date") from exc


def materialize_schedule_items(
    episodes: Iterable[int],
    *,
    start_local_datetime: str,
    timezone_iana: str,
    rule: PersonalReleaseRule | None = None,
) -> list[PersonalReleaseItem]:
    schedule_rule = rule or PersonalReleaseRule()
    schedule_rule = normalize_schedule_rule(
        schedule_rule.unit,
        schedule_rule.every,
        version=schedule_rule.version,
    )
    start = parse_local_datetime(start_local_datetime)
    zone = validate_timezone(timezone_iana)
    ordered = [int(episode) for episode in episodes]

    # Hours are elapsed-time cadence: resolve the anchor once, then add exact
    # seconds in UTC. Days/weeks are wall-clock calendar cadence and therefore
    # independently resolve each nominal local date through DST transitions.
    anchor = resolve_local_datetime(start, timezone_iana)
    anchor_utc = anchor.astimezone(UTC)
    result: list[PersonalReleaseItem] = []
    for ordinal, episode in enumerate(ordered):
        if schedule_rule.unit == "hours":
            delta = _safe_delta(hours=schedule_rule.every * ordinal)
            actual_utc = _safe_add(anchor_utc, delta)
            actual_local = actual_utc.astimezone(zone).replace(tzinfo=None)
            nominal = actual_local
            dst_adjusted = bool(ordinal == 0 and actual_local != start)
        else:
            days = schedule_rule.every * ordinal * (7 if schedule_rule.unit == "weeks" else 1)
            nominal = _safe_add(start, _safe_delta(days=days))
            resolved = resolve_local_datetime(nominal, timezone_iana)
            actual_utc = resolved.astimezone(UTC)
            dst_adjusted = resolved.replace(tzinfo=None) != nominal
        result.append(
            PersonalReleaseItem(
                episode=episode,
                ordinal=ordinal,
                unlock_at_utc=actual_utc.timestamp(),
                nominal_local_datetime=nominal.isoformat(timespec="minutes"),
                dst_adjusted=dst_adjusted,
            )
        )
    return result


def preserve_committed_items(
    planned: Iterable[PersonalReleaseItem],
    previous: Iterable[PersonalReleaseItem],
) -> list[PersonalReleaseItem]:
    """Keep already-unlocked/watched release boundaries stable while editing."""

    committed = {
        int(item.episode): item
        for item in previous
        if bool(item.unlocked) or item.watched_at is not None
    }
    result: list[PersonalReleaseItem] = []
    for item in planned:
        old = committed.get(int(item.episode))
        if old is None:
            result.append(item)
            continue
        result.append(
            PersonalReleaseItem(
                episode=item.episode,
                ordinal=item.ordinal,
                unlock_at_utc=old.unlock_at_utc,
                nominal_local_datetime=old.nominal_local_datetime,
                unlocked=old.unlocked,
                watched_at=old.watched_at,
                dst_adjusted=old.dst_adjusted,
            )
        )
    return result


def materialize_weekly_items(
    episodes: list[int] | tuple[int, ...],
    *,
    start_local_datetime: str,
    timezone_iana: str,
) -> list[PersonalReleaseItem]:
    """Backward-compatible weekly planner used by older callers/tests."""

    return materialize_schedule_items(
        episodes,
        start_local_datetime=start_local_datetime,
        timezone_iana=timezone_iana,
        rule=PersonalReleaseRule(),
    )


def local_iso_from_epoch(value: float, timezone_iana: str) -> str:
    zone = validate_timezone(timezone_iana)
    return (
        datetime.fromtimestamp(float(value), UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )


def preview_schedule_dates(
    episodes: Iterable[int],
    *,
    start_local_datetime: str,
    timezone_iana: str,
    rule: PersonalReleaseRule | None = None,
    previous_items: Iterable[PersonalReleaseItem] = (),
) -> list[dict[str, object]]:
    rows = materialize_schedule_items(
        episodes,
        start_local_datetime=start_local_datetime,
        timezone_iana=timezone_iana,
        rule=rule,
    )
    rows = preserve_committed_items(rows, previous_items)
    return [
        {
            "episode": row.episode,
            "ordinal": row.ordinal,
            "local": local_iso_from_epoch(row.unlock_at_utc, timezone_iana),
            "nominal_local": row.nominal_local_datetime,
            "unlock_at_utc": row.unlock_at_utc,
            "dst_adjusted": bool(row.dst_adjusted),
            "preserved": bool(row.unlocked or row.watched_at is not None),
            "unlocked": bool(row.unlocked),
            "watched": row.watched_at is not None,
        }
        for row in rows
    ]


def preview_weekly_dates(
    *,
    start_local_datetime: str,
    timezone_iana: str,
    count: int = 3,
) -> list[dict[str, object]]:
    """Backward-compatible ordinal preview for the original weekly UI."""

    episodes = list(range(1, max(1, int(count)) + 1))
    return preview_schedule_dates(
        episodes,
        start_local_datetime=start_local_datetime,
        timezone_iana=timezone_iana,
        rule=PersonalReleaseRule(),
    )
