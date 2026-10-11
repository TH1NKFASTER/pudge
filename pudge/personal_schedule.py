from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .i18n import err


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
            err("web.scheduleRevisionConflict", expected=self.expected_revision, current=self.actual_revision)
        )


def validate_timezone(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    if not value:
        raise ValueError(err("web.scheduleTimezoneRequired"))
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(err("web.scheduleUnknownTimezone", value=value)) from exc


def parse_local_datetime(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(err("web.scheduleStartRequired"))
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(err("web.scheduleStartFormat")) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(err("web.scheduleIntervalPositive"))
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(err("web.scheduleIntervalPositive"))
        result = int(value)
    else:
        raw = str(value or "").strip()
        if not raw or not raw.isascii() or not raw.isdigit():
            raise ValueError(err("web.scheduleIntervalPositive"))
        result = int(raw)
    if result < 1:
        raise ValueError(err("web.scheduleIntervalPositive"))
    return result


def normalize_schedule_rule(
    unit: str | None = None,
    every: Any = 1,
    *,
    version: int = RULE_VERSION,
) -> PersonalReleaseRule:
    normalized_unit = str(unit or "weeks").strip().casefold()
    if normalized_unit not in RULE_UNITS:
        raise ValueError(err("web.scheduleUnit"))
    interval = _positive_integer(every)
    if int(version) != RULE_VERSION:
        raise ValueError(err("web.scheduleRuleVersion", version=version))
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
    raise ValueError(err("web.scheduleLocalTime"))


def _safe_add(value: datetime, delta: timedelta) -> datetime:
    try:
        return value + delta
    except OverflowError as exc:
        raise ValueError(err("web.scheduleIntervalTooLarge")) from exc


def _safe_delta(**kwargs: int) -> timedelta:
    try:
        return timedelta(**kwargs)
    except OverflowError as exc:
        raise ValueError(err("web.scheduleIntervalTooLarge")) from exc


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


def _local_minute(value: float, zone: ZoneInfo) -> datetime:
    return (
        datetime.fromtimestamp(float(value), UTC)
        .astimezone(zone)
        .replace(tzinfo=None, second=0, microsecond=0)
    )


def reanchored_start_local_datetime(
    previous_items: Iterable[PersonalReleaseItem],
    *,
    previous_rule: PersonalReleaseRule | None,
    rule: PersonalReleaseRule,
    timezone_iana: str,
    now: float,
) -> str | None:
    """Cycle start that re-plans a running schedule after its interval changed.

    Rule (shorter and longer intervals alike): count from the last watched
    episode L, watched at W. Episodes already available stay available. If one
    after L is already available, the next locked one opens at now + interval;
    otherwise L+1 opens at W + interval, or right now when that has passed, so
    at most one new episode opens on the change. Each later episode follows one
    interval after the previous one. The plan is returned as an equivalent cycle
    start, so the start-based planner (and saving again unchanged) reproduces
    it. None keeps the start-date plan: same interval, or nothing watched yet.
    """

    if previous_rule is None or (previous_rule.unit, previous_rule.every) == (rule.unit, rule.every):
        return None
    items = sorted(previous_items, key=lambda item: int(item.ordinal))
    watched = [item for item in items if item.watched_at is not None]
    if not watched:
        return None
    last = watched[-1]
    after = [item for item in items if item.ordinal > last.ordinal]

    def available(item: PersonalReleaseItem) -> bool:
        return bool(item.unlocked) or item.watched_at is not None or item.unlock_at_utc <= now

    locked = [item for item in after if not available(item)]
    if not locked:
        return None
    first = locked[0]
    ready_before = any(available(item) for item in after if item.ordinal < first.ordinal)
    base = float(now) if ready_before else float(last.watched_at or now)
    zone = validate_timezone(timezone_iana)
    if rule.unit == "hours":
        target = base + rule.every * 3600
        if target <= now:
            target = float(now)
        target = float(int(target // 60) * 60)
        start = datetime.fromtimestamp(target - rule.every * 3600 * first.ordinal, UTC)
        return start.astimezone(zone).replace(tzinfo=None).isoformat(timespec="minutes")
    days = rule.every * (7 if rule.unit == "weeks" else 1)
    nominal = _safe_add(_local_minute(base, zone), _safe_delta(days=days))
    if resolve_local_datetime(nominal, timezone_iana).timestamp() <= now:
        nominal = _local_minute(now, zone)
    start_local = _safe_add(nominal, _safe_delta(days=-days * first.ordinal))
    return start_local.isoformat(timespec="minutes")


def preserve_committed_items(
    planned: Iterable[PersonalReleaseItem],
    previous: Iterable[PersonalReleaseItem],
    *,
    now: float | None = None,
) -> list[PersonalReleaseItem]:
    """Keep already-unlocked/watched release boundaries stable while editing.

    With ``now``, an episode whose stored time has passed counts as released
    too (even if nothing marked it open yet), so it keeps its real date.
    """

    committed = {
        int(item.episode): item
        for item in previous
        if bool(item.unlocked)
        or item.watched_at is not None
        or (now is not None and item.unlock_at_utc <= now)
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
                unlocked=bool(old.unlocked) or (now is not None and old.unlock_at_utc <= now),
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
    previous_rule: PersonalReleaseRule | None = None,
    now: float | None = None,
) -> list[dict[str, object]]:
    previous_items = tuple(previous_items)
    if previous_rule is not None and rule is not None and now is not None:
        start_local_datetime = (
            reanchored_start_local_datetime(
                previous_items,
                previous_rule=previous_rule,
                rule=rule,
                timezone_iana=timezone_iana,
                now=now,
            )
            or start_local_datetime
        )
    rows = materialize_schedule_items(
        episodes,
        start_local_datetime=start_local_datetime,
        timezone_iana=timezone_iana,
        rule=rule,
    )
    rows = preserve_committed_items(rows, previous_items, now=now)
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
