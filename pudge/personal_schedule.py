from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class PersonalReleaseItem:
    episode: int
    ordinal: int
    unlock_at_utc: float
    nominal_local_datetime: str
    unlocked: bool = False
    watched_at: float | None = None


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


def materialize_weekly_items(
    episodes: list[int] | tuple[int, ...],
    *,
    start_local_datetime: str,
    timezone_iana: str,
) -> list[PersonalReleaseItem]:
    start = parse_local_datetime(start_local_datetime)
    validate_timezone(timezone_iana)
    result: list[PersonalReleaseItem] = []
    for ordinal, episode in enumerate(episodes):
        nominal = start + timedelta(days=7 * ordinal)
        resolved = resolve_local_datetime(nominal, timezone_iana)
        result.append(
            PersonalReleaseItem(
                episode=int(episode),
                ordinal=ordinal,
                unlock_at_utc=resolved.astimezone(UTC).timestamp(),
                nominal_local_datetime=nominal.isoformat(timespec="minutes"),
            )
        )
    return result




def local_iso_from_epoch(value: float, timezone_iana: str) -> str:
    zone = validate_timezone(timezone_iana)
    return (
        datetime.fromtimestamp(float(value), UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )

def preview_weekly_dates(
    *,
    start_local_datetime: str,
    timezone_iana: str,
    count: int = 3,
) -> list[dict[str, object]]:
    episodes = list(range(1, max(1, int(count)) + 1))
    rows = materialize_weekly_items(
        episodes,
        start_local_datetime=start_local_datetime,
        timezone_iana=timezone_iana,
    )
    return [
        {
            "ordinal": row.ordinal,
            "local": local_iso_from_epoch(row.unlock_at_utc, timezone_iana),
            "nominal_local": row.nominal_local_datetime,
            "unlock_at_utc": row.unlock_at_utc,
        }
        for row in rows
    ]
