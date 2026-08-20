from __future__ import annotations

from enum import StrEnum


class EpisodeState(StrEnum):
    """User-visible lifecycle of one local video.

    Subtitle worker stages (discovering, OCR, aligning, and so on) belong to the
    job state, not to this durable media state.
    """

    LOCAL = "local"
    WAITING_SUBTITLES = "waiting_subtitles"
    WAITING_TEXT_SUBTITLES = "waiting_text_subtitles"
    READY = "ready"
    WATCHED = "watched"
    DROPPED = "dropped"


EPISODE_STATE_ORDER: dict[str, int] = {
    EpisodeState.DROPPED: 0,
    EpisodeState.LOCAL: 1,
    EpisodeState.WAITING_SUBTITLES: 2,
    EpisodeState.WAITING_TEXT_SUBTITLES: 3,
    EpisodeState.READY: 4,
    EpisodeState.WATCHED: 5,
}

_ALLOWED: dict[str, set[str]] = {
    EpisodeState.LOCAL: {
        EpisodeState.WAITING_SUBTITLES,
        EpisodeState.WAITING_TEXT_SUBTITLES,
        EpisodeState.READY,
        EpisodeState.WATCHED,
        EpisodeState.DROPPED,
    },
    EpisodeState.WAITING_SUBTITLES: {
        EpisodeState.LOCAL,
        EpisodeState.WAITING_TEXT_SUBTITLES,
        EpisodeState.READY,
        EpisodeState.WATCHED,
        EpisodeState.DROPPED,
    },
    EpisodeState.WAITING_TEXT_SUBTITLES: {
        EpisodeState.WAITING_SUBTITLES,
        EpisodeState.READY,
        EpisodeState.WATCHED,
        EpisodeState.DROPPED,
    },
    EpisodeState.READY: {
        EpisodeState.WAITING_SUBTITLES,
        EpisodeState.WAITING_TEXT_SUBTITLES,
        EpisodeState.WATCHED,
        EpisodeState.DROPPED,
    },
    EpisodeState.WATCHED: {EpisodeState.READY, EpisodeState.DROPPED},
    EpisodeState.DROPPED: {EpisodeState.LOCAL},
}


def normalize_episode_state(value: object) -> str:
    candidate = str(value or EpisodeState.LOCAL).strip().casefold()
    return candidate if candidate in EPISODE_STATE_ORDER else EpisodeState.LOCAL


def transition_episode_state(
    current: object,
    requested: object,
    *,
    trigger: str = "explicit",
) -> str:
    """Resolve a state transition without allowing lossy background rewrites.

    A library scan is observational. It may discover a more useful state but
    must never erase Ready, Watched, Dropped, or a confirmed bitmap fallback.
    Explicit subtitle repair is allowed to move backwards because it carries a
    concrete reason and is recorded in ``episode_state_history``.
    """

    old = normalize_episode_state(current)
    new = normalize_episode_state(requested)
    if old == new:
        return old
    if trigger == "scan":
        if old in {EpisodeState.WATCHED, EpisodeState.DROPPED}:
            return old
        if old == EpisodeState.READY and new in {
            EpisodeState.LOCAL,
            EpisodeState.WAITING_SUBTITLES,
        }:
            return old
        if old == EpisodeState.WAITING_TEXT_SUBTITLES and new in {
            EpisodeState.LOCAL,
            EpisodeState.WAITING_SUBTITLES,
        }:
            return old
    if trigger in {"subtitle_ready", "bitmap_detected"} and old in {
        EpisodeState.WATCHED,
        EpisodeState.DROPPED,
    }:
        return old
    return new if new in _ALLOWED.get(old, set()) else old


def stronger_episode_state(left: object, right: object) -> str:
    a = normalize_episode_state(left)
    b = normalize_episode_state(right)
    return a if EPISODE_STATE_ORDER[a] >= EPISODE_STATE_ORDER[b] else b


def watched_by_anilist_progress(
    episode: int | None,
    progress: int | None,
    *,
    total_episodes: int | None = None,
    media_format: str | None = None,
) -> bool:
    """Return whether AniList progress already covers a local episode.

    This is intentionally a presentation/synchronization fact, not a local
    playback timestamp.  A file downloaded after the user watched the episode
    elsewhere must not reappear as Ready, but it also must not enter Pudge's
    timed deletion queue as though it had just been watched locally.
    """

    try:
        watched_through = max(0, int(progress or 0))
    except (TypeError, ValueError):
        return False
    if watched_through < 1:
        return False
    if episode is not None:
        try:
            local_episode = int(episode)
        except (TypeError, ValueError):
            return False
        return local_episode >= 1 and local_episode <= watched_through
    try:
        single_entry = int(total_episodes or 0) == 1
    except (TypeError, ValueError):
        single_entry = False
    return single_entry or str(media_format or "").upper() == "MOVIE"
