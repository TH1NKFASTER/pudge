"""Durable, read-only torrent discovery for upcoming Planning entries.

This subsystem never starts a download or writes to AniList. Only future entries
observed while still upcoming are armed, and entries added to the list before a
premiere at most 30 days ago; other pre-existing released entries are not.

Phases: "waiting" (seen while unreleased, premiere not known yet), "armed"
(searched with backoff), "found" (offer card), "ignored" (already released when
first seen, dismissed, or left Planning during a search).
"""

from __future__ import annotations

import fcntl
import json
import time
from datetime import date, datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any

from .manager_models import LibraryAnime
from .providers.nyaa import NoReleaseSourceError

_PREFIX = "planned_release_discovery:v1:"
_DELAYS = (15, 30, 60, 120, 240, 480, 720)
_WEEK_SECONDS = 7 * 24 * 60 * 60
_RELIABLE_AIRING_SOURCES = {"next_episode_1", "weekly_backfill"}
_RECENT_PREMIERE_SECONDS = 24 * 60 * 60
_SEARCH_REVISION = 7  # season packs of a whole season released at once (Netflix drops)
# AniList statuses that prove a title is already out, even without a date.
_RELEASED_MEDIA_STATUSES = {"FINISHED", "RELEASING", "HIATUS"}
# Old "ignored" movies are re-armed only for recent premieres: no offer spam.
_MOVIE_REVIEW_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
# An entry added to Planning before its premiere but first seen by Pudge only after it
# (added on the AniList site or phone, Pudge opened days later) is still searched when the
# premiere is this recent; older ones stay grandfathered (a first start offers no backlog).
_PLANNED_BEFORE_PREMIERE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


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


def _dismissed(db: Any, media_id: int) -> bool:
    return db.get_state(f"{_PREFIX}dismissed:{int(media_id)}", "") == "1"


def _arm_state(first_airing: float, airing_source: str, current: float) -> dict[str, Any]:
    return {
        "phase": "armed",
        "airing_at": first_airing,
        "airing_source": airing_source,
        # A premiere already in the past is searched right away.
        "next_check": max(first_airing + 15 * 60, current),
        "attempts": 0,
        "search_revision": _SEARCH_REVISION,
    }


def _movie_review_due(db: Any, anime: LibraryAnime, state: dict[str, Any]) -> bool:
    """An "ignored" movie from before Pudge recorded when it first saw an entry.

    Such entries were grandfathered even when they were added to the list long
    before the premiere (the Madoka movie). They are decided once, from AniList's
    list add time; until a sync has filled that time they stay undecided.
    """
    return (
        state.get("phase") == "ignored"
        # New grandfathered entries carry seen_released_at; reviewed ones reviewed_at.
        and not state.get("seen_released_at")
        and not state.get("reviewed_at")
        and str(anime.format or "").upper() == "MOVIE"
        and float(getattr(anime, "list_added_at", 0) or 0) > 0
        and _first_airing_at(anime) is not None
        and not _dismissed(db, anime.media_id)
    )


def _planned_before_premiere(anime: LibraryAnime, first_airing: float | None, current: float) -> bool:
    """Added to the AniList list before a premiere at most 30 days ago (add time known)."""
    added = float(getattr(anime, "list_added_at", 0) or 0)
    return bool(
        first_airing is not None
        and 0 < added < first_airing
        and current - first_airing <= _PLANNED_BEFORE_PREMIERE_MAX_AGE_SECONDS
    )


def _planned_review_due(db: Any, anime: LibraryAnime, state: dict[str, Any], current: float) -> bool:
    """An entry grandfathered as "released when first seen" although it was planned before
    a recent premiere (versions that did not look at the list add time): decided once."""
    return (
        state.get("phase") == "ignored"
        and bool(state.get("seen_released_at"))
        and not state.get("reviewed_at")
        and _planned_before_premiere(anime, _first_airing_at(anime), current)
        and not _dismissed(db, anime.media_id)
    )


def discovery_work_due(db: Any, *, now: float | None = None) -> bool:
    """Cheap check for the scheduled agent: does a Planning entry need the tick?

    True when an entry was never seen and has a known premiere (it may need
    arming), a waiting entry's premiere became known, an old ignored movie can be
    reviewed, or an armed entry's next search is due. Ignored, found and undated
    entries do not wake the agent (the next regular run records undated ones).
    """
    current = time.time() if now is None else float(now)
    for anime in db.anime_list(("PLANNING",)):
        state = _decode(db.get_state(f"{_PREFIX}{int(anime.media_id)}", ""))
        if not state or state.get("phase") == "waiting":
            if _first_airing_at(anime) is not None:
                return True
            continue
        if _movie_review_due(db, anime, state) or _planned_review_due(db, anime, state, current):
            return True
        if state.get("phase") == "armed":
            try:
                if float(state.get("next_check", 0)) <= current:
                    return True
            except (TypeError, ValueError):
                return True
    return False


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

    def _has_local_episodes(self, media_id: int) -> bool:
        """Whether the library already has a file of this entry."""
        episodes = getattr(self.db, "episodes", None)
        if not callable(episodes):
            return False
        try:
            return any(Path(item.video_path).is_file() for item in episodes(int(media_id)))
        except (OSError, TypeError, ValueError):
            return False

    def offers(self) -> list[dict[str, Any]]:
        """Read only: UI polling cannot resurrect completed/removed offers."""
        result: list[dict[str, Any]] = []
        for anime in self.db.anime_list(("PLANNING",)):
            state = self._state(anime.media_id)
            if state.get("phase") != "found" or _dismissed(self.db, anime.media_id):
                continue
            if self._has_local_episodes(anime.media_id):
                continue  # downloaded or dropped meanwhile: it is in the library already
            result.append(
                {
                    "media_id": anime.media_id,
                    "title": anime.title,
                    "cover": str(anime.cover_url or ""),
                    "found_at": state.get("found_at"),
                }
            )
        return sorted(result, key=lambda row: (float(row["found_at"] or 0), row["media_id"]))

    def _season_packs(self, anime: LibraryAnime) -> list[Any]:
        """Acceptable season packs of a finished entry (what Watch would download)."""
        releases = self.manager.search_releases(
            anime.media_id,
            episode=None,
            batch=True,
            automatic=True,
            allow_anilist_network=False,
        )
        safe_pack = getattr(self.manager, "_release_is_safe_batch_candidate", None)
        packs = [
            release
            for release in releases
            if self.manager._release_is_allowed_for_auto(release)
            and (not callable(safe_pack) or safe_pack(anime.media_id, release))
        ]
        self.manager.logger.info(
            "RESULT step=planning.release_discovery.season_pack media_id=%s candidates=%s acceptable=%s",
            anime.media_id,
            len(releases),
            len(packs),
        )
        return packs

    def tick(self, *, now: float | None = None, max_searches: int = 2) -> dict[str, int]:
        """Arm future releases and search due ones under a cross-process lock."""
        current = time.time() if now is None else float(now)
        lock_path = Path(self.manager.config.paths.cache_dir) / "planned-release-discovery.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        searched = found = armed = 0
        no_source = False
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
                    media_status = str(anime.media_status or "").upper()
                    if not state:
                        if first_airing is None:
                            if media_status in _RELEASED_MEDIA_STATUSES:
                                # Out already, just undated: wait for a future
                                # first-airing timestamp as before.
                                continue
                            # Remember the unreleased sighting. Skipping without a
                            # state made a later past date grandfather the entry.
                            self._save(anime.media_id, {"phase": "waiting", "seen_unreleased_at": current})
                            continue
                        planned_before_premiere = _planned_before_premiere(anime, first_airing, current)
                        if (
                            first_airing <= current - _RECENT_PREMIERE_SECONDS
                            and media_status != "NOT_YET_RELEASED"
                            and not planned_before_premiere
                        ):
                            # Grandfather existing released entries. A later
                            # metadata refresh must never arm them retroactively.
                            # An entry added on premiere day (e.g. after the
                            # noon-UTC date-only guess) is still searched.
                            self._save(anime.media_id, {"phase": "ignored", "seen_released_at": current})
                            continue
                        state = _arm_state(first_airing, airing_source, current)
                        self._save(anime.media_id, state)
                        armed += 1
                        if planned_before_premiere and first_airing <= current - _RECENT_PREMIERE_SECONDS:
                            self.manager.logger.info(
                                "UPDATE step=planning.release_discovery media_id=%s "
                                "reason=planned_before_premiere list_added_at=%s airing_at=%s",
                                anime.media_id,
                                anime.list_added_at,
                                first_airing,
                            )
                    elif state.get("phase") == "waiting":
                        if first_airing is None:
                            continue
                        added = float(anime.list_added_at or 0)
                        if added and added >= first_airing + _RECENT_PREMIERE_SECONDS:
                            # A placeholder row (no date yet) of a title added to
                            # the list after its premiere: it was out already.
                            self._save(anime.media_id, {"phase": "ignored", "seen_released_at": current})
                            continue
                        # Seen while unreleased: search even if the premiere date
                        # AniList filled in later is already in the past.
                        state = {
                            **_arm_state(first_airing, airing_source, current),
                            "seen_unreleased_at": state.get("seen_unreleased_at"),
                        }
                        self._save(anime.media_id, state)
                        armed += 1
                        self.manager.logger.info(
                            "UPDATE step=planning.release_discovery media_id=%s reason=premiere_known "
                            "airing_at=%s source=%s",
                            anime.media_id,
                            first_airing,
                            airing_source,
                        )
                    elif _movie_review_due(self.db, anime, state) and first_airing is not None:
                        added = float(anime.list_added_at or 0)
                        recent = current - first_airing <= _MOVIE_REVIEW_MAX_AGE_SECONDS
                        if added < first_airing and recent:
                            state = _arm_state(first_airing, airing_source, current)
                            self._save(anime.media_id, state)
                            armed += 1
                        else:
                            self._save(anime.media_id, {"phase": "ignored", "reviewed_at": current})
                        self.manager.logger.info(
                            "UPDATE step=planning.release_discovery media_id=%s reason=ignored_movie_review "
                            "list_added_at=%s airing_at=%s armed=%s",
                            anime.media_id,
                            added,
                            first_airing,
                            state.get("phase") == "armed",
                        )
                    elif first_airing is not None and _planned_review_due(self.db, anime, state, current):
                        state = _arm_state(first_airing, airing_source, current)
                        self._save(anime.media_id, state)
                        armed += 1
                        self.manager.logger.info(
                            "UPDATE step=planning.release_discovery media_id=%s reason=planned_before_premiere_review "
                            "list_added_at=%s airing_at=%s",
                            anime.media_id,
                            anime.list_added_at,
                            first_airing,
                        )
                    if state.get("phase") != "armed" or first_airing is None:
                        continue
                    if self._has_local_episodes(anime.media_id):
                        # Already in the library (a dropped or watched-folder copy): nothing to offer.
                        self._save(anime.media_id, {"phase": "ignored", "local_at": current})
                        self.manager.logger.info(
                            "SKIP step=planning.release_discovery media_id=%s reason=local_episodes",
                            anime.media_id,
                        )
                        continue
                    # Exact/recovered metadata supersedes a date-only guess. This
                    # also migrates G27 states created during AniList's Ep1 -> Ep2
                    # transition (the JoJo SBR 2nd/3rd STAGE failure mode).
                    if first_airing != state.get("airing_at") or state.get("airing_source") != airing_source:
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
                            except Exception as exc:  # noqa: BLE001 - aliases are optional
                                self.manager.logger.warning(
                                    "FALLBACK step=planning.release_discovery.aliases media_id=%s error=%r",
                                    anime.media_id,
                                    exc,
                                )
                                aliases = ()
                            search_aliases = [
                                int(value) for value in aliases if int(value) > 0 and int(value) != episode
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
                    if no_source or searched >= max_searches or current < float(state.get("next_check", 0)):
                        continue
                    searched += 1
                    try:
                        releases = self.manager.search_releases(
                            anime.media_id,
                            episode=episode,
                            batch=anime.format == "MOVIE",
                            automatic=True,
                            allow_anilist_network=False,
                        )
                        acceptable = [
                            release
                            for release in releases
                            if self.manager._release_is_allowed_for_auto(release)
                            and (episode is None or self.manager._release_has_safe_episode_identity(release))
                        ]
                        if not acceptable and episode is not None and media_status == "FINISHED":
                            # The whole season came out at once (a Netflix drop): often
                            # there is only a season pack, which an episode search drops.
                            acceptable = self._season_packs(anime)
                    except NoReleaseSourceError:
                        # No source in Settings → Sources: no backoff, searched once one is added.
                        self.manager.logger.info(
                            "SKIP step=planning.release_discovery reason=no_release_source"
                        )
                        no_source = True
                        searched -= 1
                        continue
                    except Exception as exc:
                        self.manager.logger.warning(
                            "RETRY step=planning.release_discovery media_id=%s error=%r",
                            anime.media_id,
                            exc,
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
