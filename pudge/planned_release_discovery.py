"""Durable, read-only torrent discovery for upcoming Planning entries.

This subsystem never starts a download or writes to AniList. Only future entries
observed while still upcoming are armed; pre-existing released entries are not.
"""
from __future__ import annotations

import fcntl
import json
import time
from datetime import date, datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any

from .manager_models import LibraryAnime

_PREFIX = "planned_release_discovery:v1:"
_DELAYS = (15, 30, 60, 120, 240, 480, 720)
_WEEK_SECONDS = 7 * 24 * 60 * 60
_RELIABLE_AIRING_SOURCES = {"next_episode_1", "weekly_backfill"}
_SEARCH_REVISION = 5  # G32h: keep split-stage identity alias in related-title plausibility


def _first_airing_candidate(anime: LibraryAnime) -> tuple[float | None, str]:
    """Return the best known premiere timestamp and how it was derived.

    AniList advances ``nextAiringEpisode`` immediately after an episode airs. If
    Pudge first sees a Planning entry after that transition, episode #1's exact
    timestamp can still be recovered from a regular weekly schedule. The
    ``start_date`` check keeps an irregular/hiatus schedule from being treated as
    weekly merely because a later episode exists.
    """
    next_episode = int(anime.next_airing_episode or 0)
    if next_episode == 1 and anime.next_airing_at:
        return float(anime.next_airing_at), "next_episode_1"

    start_day: date | None = None
    if anime.start_date:
        try:
            start_day = date.fromisoformat(anime.start_date)
        except (TypeError, ValueError):
            start_day = None

    if next_episode > 1 and anime.next_airing_at and start_day is not None:
        recovered = float(anime.next_airing_at) - (next_episode - 1) * _WEEK_SECONDS
        recovered_day = datetime.fromtimestamp(recovered, tz=timezone.utc).date()
        # AniList's start date is calendar-only and may be in the show's local
        # timezone, so a UTC conversion can legitimately land one day either side.
        if abs((recovered_day - start_day).days) <= 1:
            return recovered, "weekly_backfill"

    if start_day is None:
        return None, "unknown"
    # Date-only metadata is deliberately a conservative guess, not an exact
    # premiere clock. It may later be replaced by a recovered weekly timestamp.
    return (
        datetime.combine(start_day, clock_time(12), tzinfo=timezone.utc).timestamp(),
        "date_fallback",
    )


def _first_airing_at(anime: LibraryAnime) -> float | None:
    return _first_airing_candidate(anime)[0]


def _decode(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


class PlannedReleaseDiscovery:
    def __init__(self, manager: Any):
        self.manager = manager
        self.db = manager.db

    @staticmethod
    def _key(media_id: int) -> str:
        return f"{_PREFIX}{int(media_id)}"

    def _state(self, media_id: int) -> dict[str, Any]:
        return _decode(self.db.get_state(self._key(media_id), ""))

    def _save(self, media_id: int, state: dict[str, Any]) -> None:
        self.db.set_state(self._key(media_id), json.dumps(state, separators=(",", ":")))

    def dismiss(self, media_id: int) -> None:
        """Consume an offer even if an earlier network search completes late."""
        # A separate tombstone wins over an in-flight search that acquired the
        # discovery lock before the user changed the AniList status.
        self.db.set_state(f"{_PREFIX}dismissed:{int(media_id)}", "1")
        self._save(media_id, {"phase": "ignored"})

    def offers(self) -> list[dict[str, Any]]:
        """Read only: UI polling cannot resurrect completed/removed offers."""
        result: list[dict[str, Any]] = []
        for anime in self.db.anime_list(("PLANNING",)):
            state = self._state(anime.media_id)
            if state.get("phase") != "found" or self.db.get_state(
                f"{_PREFIX}dismissed:{anime.media_id}", ""
            ) == "1":
                continue
            result.append({
                "media_id": anime.media_id,
                "title": anime.title,
                "cover": str(anime.cover_url or ""),
                "found_at": state.get("found_at"),
            })
        return sorted(result, key=lambda row: (float(row["found_at"] or 0), row["media_id"]))

    def tick(self, *, now: float | None = None, max_searches: int = 2) -> dict[str, int]:
        """Arm future releases and search due ones under a cross-process lock."""
        current = time.time() if now is None else float(now)
        lock_path = Path(self.manager.config.paths.cache_dir) / "planned-release-discovery.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        searched = found = armed = 0
        with lock_path.open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"armed": 0, "searched": 0, "found": 0}
            try:
                planned = self.db.anime_list(("PLANNING",))
                for anime in planned:
                    first_airing, airing_source = _first_airing_candidate(anime)
                    state = self._state(anime.media_id)
                    if state.get("phase") == "armed" and airing_source not in _RELIABLE_AIRING_SOURCES:
                        # AniList can stop exposing episode #1 after the premiere.
                        # Keep a persisted exact/recovered clock unless current
                        # metadata can provide another reliable timestamp.
                        try:
                            first_airing = float(state["airing_at"])
                        except (KeyError, TypeError, ValueError):
                            pass
                        else:
                            airing_source = str(state.get("airing_source") or "persisted")
                    if not state:
                        if first_airing is None:
                            # Missing dates are not proof of an old release. Wait
                            # for an authoritative future first-airing timestamp.
                            continue
                        if first_airing <= current:
                            # Grandfather existing released entries. A later
                            # metadata refresh must never arm them retroactively.
                            self._save(anime.media_id, {"phase": "ignored"})
                            continue
                        state = {"phase": "armed", "airing_at": first_airing,
                                 "airing_source": airing_source,
                                 "next_check": first_airing + 15 * 60, "attempts": 0,
                                 "search_revision": _SEARCH_REVISION}
                        self._save(anime.media_id, state)
                        armed += 1
                    if state.get("phase") != "armed" or first_airing is None:
                        continue
                    # Exact/recovered metadata supersedes a date-only guess. This
                    # also migrates G27 states created during AniList's Ep1 -> Ep2
                    # transition (the JoJo SBR 2nd/3rd STAGE failure mode).
                    if (
                        first_airing != state.get("airing_at")
                        or state.get("airing_source") != airing_source
                    ):
                        old_airing = state.get("airing_at")
                        state["airing_at"] = first_airing
                        state["airing_source"] = airing_source
                        state["next_check"] = first_airing + 15 * 60
                        self._save(anime.media_id, state)
                        if old_airing != first_airing:
                            self.manager.logger.info(
                                "UPDATE step=planning.release_discovery media_id=%s "
                                "reason=first_airing_recovered next_episode=%s "
                                "old_airing_at=%s new_airing_at=%s source=%s",
                                anime.media_id,
                                anime.next_airing_episode,
                                old_airing,
                                first_airing,
                                airing_source,
                            )
                    if not self.manager.config.nyaa.enabled:
                        continue
                    # Search transport changes can make a previously unreachable
                    # release discoverable without changing episode aliases. Reset
                    # only an existing failed backoff once per transport revision;
                    # brand-new armed entries keep their normal +15 minute delay.
                    if state.get("search_revision") != _SEARCH_REVISION:
                        previous_revision = state.get("search_revision")
                        had_failed_attempts = max(0, int(state.get("attempts", 0))) > 0
                        state["search_revision"] = _SEARCH_REVISION
                        if had_failed_attempts:
                            state["attempts"] = 0
                            state["next_check"] = current
                        self._save(anime.media_id, state)
                        if had_failed_attempts:
                            self.manager.logger.info(
                                "UPDATE step=planning.release_discovery media_id=%s "
                                "reason=search_transport_changed previous=%s current=%s",
                                anime.media_id,
                                previous_revision,
                                _SEARCH_REVISION,
                            )
                    episode = None if anime.format == "MOVIE" else 1
                    if episode is not None:
                        resolver = getattr(self.manager, "_release_episode_context_from_graph", None)
                        if callable(resolver):
                            try:
                                aliases, _titles = resolver(anime, episode)
                            except Exception:
                                aliases = ()
                            search_aliases = [
                                int(value) for value in aliases
                                if int(value) > 0 and int(value) != episode
                            ]
                            if search_aliases and state.get("search_aliases") != search_aliases:
                                previous_aliases = state.get("search_aliases")
                                state["search_aliases"] = search_aliases
                                state["attempts"] = 0
                                state["next_check"] = current
                                self._save(anime.media_id, state)
                                self.manager.logger.info(
                                    "UPDATE step=planning.release_discovery media_id=%s "
                                    "reason=episode_aliases_changed previous=%s current=%s",
                                    anime.media_id,
                                    previous_aliases,
                                    search_aliases,
                                )
                    if searched >= max_searches or current < float(state.get("next_check", 0)):
                        continue
                    searched += 1
                    try:
                        releases = self.manager.search_releases(
                            anime.media_id, episode=episode,
                            batch=anime.format == "MOVIE", automatic=True,
                            allow_anilist_network=False,
                        )
                        acceptable = [
                            release for release in releases
                            if self.manager._release_is_allowed_for_auto(release)
                            and (episode is None or self.manager._release_has_safe_episode_identity(release))
                        ]
                    except Exception as exc:
                        self.manager.logger.warning(
                            "RETRY step=planning.release_discovery media_id=%s error=%r",
                            anime.media_id, exc,
                        )
                        acceptable = []
                    latest = self.db.get_anime(anime.media_id)
                    if latest is None or latest.status != "PLANNING":
                        # An in-flight search must not resurrect a Watch/Drop action.
                        self._save(anime.media_id, {"phase": "ignored"})
                        continue
                    if acceptable:
                        state.update(phase="found", found_at=current)
                        found += 1
                    else:
                        attempt = max(0, int(state.get("attempts", 0)))
                        age = max(0, current - float(state.get("airing_at", current)))
                        delay = 1440 if age >= 7 * 86400 else _DELAYS[min(attempt, len(_DELAYS) - 1)]
                        state["next_check"] = current + delay * 60
                        state["attempts"] = attempt + 1
                    self._save(anime.media_id, state)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return {"armed": armed, "searched": searched, "found": found}
