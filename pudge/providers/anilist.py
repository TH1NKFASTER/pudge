from __future__ import annotations

import contextlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import contextvars
import hashlib
import json
import logging
import os
import threading
import time

import httpx

from ..filename import normalize_title, title_similarity
from ..branding import APP_NAME
from ..models import AniListAnime, VideoIdentity
from ..manager_models import LibraryAnime
from ..i18n import err


QUERY = """
query ($search: String!) {
  Page(page: 1, perPage: 12) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native userPreferred }
      synonyms
      seasonYear
      episodes
      format
    }
  }
}
"""

VIEWER_QUERY = """
query {
  Viewer {
    id
    name
  }
}
"""

LIST_ENTRY_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    title { romaji english native userPreferred }
    synonyms
    seasonYear
    startDate { year month day }
    endDate { year month day }
    episodes
    format
    status
    meanScore
    duration
    siteUrl
    coverImage { extraLarge large medium }
    studios { nodes { name isAnimationStudio } }
    nextAiringEpisode { episode airingAt }
    mediaListEntry {
      id
      progress
      status
      score(format: POINT_10)
      createdAt
    }
  }
}
"""


RELATIONS_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    title { romaji english native userPreferred }
    synonyms
    seasonYear
    startDate { year month day }
    episodes
    format
    relations {
      edges {
        relationType
        node {
          id
          title { romaji english native userPreferred }
          synonyms
          seasonYear
          startDate { year month day }
          episodes
          format
        }
      }
    }
  }
}
"""


FULL_RELATION_GRAPH_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      type
      title { romaji english native userPreferred }
      episodes
      format
      seasonYear
      startDate { year month day }
      status
      siteUrl
      coverImage { extraLarge large medium }
      studios { nodes { name isAnimationStudio } }
      mediaListEntry { status progress }
      relations {
        edges {
          relationType
          node {
            id
            type
            format
            title { romaji english native userPreferred }
          }
        }
      }
    }
  }
}
"""

# The same page with each neighbour's own fields and edges nested in: one
# request covers two levels of the franchise, so a long chain (Conan, Lupin,
# Gintama movies) costs about half as many requests.
FULL_RELATION_GRAPH_NESTED_QUERY = """
fragment GraphNode on Media {
  id
  type
  title { romaji english native userPreferred }
  episodes
  format
  seasonYear
  startDate { year month day }
  status
  siteUrl
  coverImage { extraLarge large medium }
  studios { nodes { name isAnimationStudio } }
  mediaListEntry { status progress }
}

fragment GraphEdges on Media {
  relations {
    edges {
      relationType
      node {
        id
        type
        format
        title { romaji english native userPreferred }
      }
    }
  }
}

query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      ...GraphNode
      relations {
        edges {
          relationType
          node {
            ...GraphNode
            ...GraphEdges
          }
        }
      }
    }
  }
}
"""

RELATION_LIST_ENTRIES_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      mediaListEntry { status progress }
    }
  }
}
"""

LIBRARY_QUERY = """
query ($userId: Int!) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      status
      entries {
        progress
        status
        score(format: POINT_10)
        createdAt
        media {
          id
          title { romaji english native userPreferred }
          synonyms
          episodes
          format
          seasonYear
          startDate { year month day }
          status
          endDate { year month day }
          meanScore
          duration
          siteUrl
          coverImage { extraLarge large medium }
          studios { nodes { name isAnimationStudio } }
          nextAiringEpisode { episode airingAt }
        }
      }
    }
  }
}
"""

MINIMAL_LIST_ENTRY_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    mediaListEntry {
      id
      progress
      status
      score(format: POINT_10)
    }
  }
}
"""

AIRING_SCHEDULE_QUERY = """
query ($mediaId: Int!, $episode: Int!) {
  AiringSchedule(mediaId: $mediaId, episode: $episode) {
    episode
    airingAt
  }
}
"""

RELATED_MEDIA_ADDITION_NOTIFICATIONS_QUERY = """
query ($page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    notifications(type: RELATED_MEDIA_ADDITION, resetNotificationCount: false) {
      ... on RelatedMediaAdditionNotification {
        id
        type
        mediaId
        context
        createdAt
        media {
          id
          type
          status
          format
          siteUrl
          startDate { year month day }
          mediaListEntry { status }
          title { romaji english native userPreferred }
          coverImage { extraLarge large medium }
        }
      }
    }
  }
}
"""


SAVE_STATUS_MUTATION = """
mutation ($mediaId: Int!, $status: MediaListStatus!) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status) {
    id
    progress
    status
    score(format: POINT_10)
  }
}
"""

SAVE_SCORE_MUTATION = """
mutation ($mediaId: Int!, $scoreRaw: Int!) {
  SaveMediaListEntry(mediaId: $mediaId, scoreRaw: $scoreRaw) {
    id
    progress
    status
    score(format: POINT_10)
  }
}
"""

DELETE_LIST_ENTRY_MUTATION = """
mutation ($id: Int!) {
  DeleteMediaListEntry(id: $id) { deleted }
}
"""

SAVE_PROGRESS_MUTATION = """
mutation ($mediaId: Int!, $progress: Int!, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) {
    id
    progress
    status
  }
}
"""


class AniListError(RuntimeError):
    pass


class AniListHTTPError(AniListError):
    def __init__(self, message: str, *, status_code: int, detail: str = "", refusal: bool = False) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        # AniList's own words (GraphQL errors[].message), for the UI and the log.
        self.detail = str(detail or "")
        # A 403 that refuses every request (the API switched off, a block): the
        # shared cool-down. Any other 403 concerns only that request.
        self.refusal = bool(refusal)


class AniListAuthError(AniListHTTPError):
    """AniList rejected the access token (expired after a year, revoked or mistyped)."""


class AniListNotFoundError(AniListError):
    """The anime does not exist on AniList (any more): merged or deleted there."""


class AniListNotFoundHTTPError(AniListHTTPError, AniListNotFoundError):
    """AniList's HTTP 404 ("Not Found.", e.g. Media of a merged id): both an HTTP error with its
    status and the "gone" error callers handle (that branch only ran for a 200 with null)."""


class AniListNetworkError(AniListError):
    """AniList could not be reached at all (no network, DNS, timeout)."""


def entry_error_reason(exc: BaseException) -> str:
    """Why a queued change failed for this one entry only ("" when AniList as a whole failed).

    A 404 / missing Media means the entry is gone for good. Another 400 rejects
    this request's input. Everything else (403, 429, 5xx, no network, a bad token)
    affects every request, so a queue flush stops instead of trying the next one.
    """
    if isinstance(exc, AniListNotFoundError):
        return "not_found"
    if isinstance(exc, AniListAuthError) or not isinstance(exc, AniListHTTPError):
        return ""
    if exc.status_code == 404:
        return "not_found"
    if exc.status_code == 400 or (exc.status_code == 403 and not exc.refusal):
        return "rejected"
    return ""


ANILIST_PROJECT_URL = "https://github.com/TH1NKFASTER/pudge"


def _user_agent() -> str:
    from .. import __version__

    product = "".join(ch for ch in str(APP_NAME or "Pudge") if ch.isalnum() or ch in "._-") or "Pudge"
    return f"{product[:1].upper()}{product[1:]}/{__version__} (+{ANILIST_PROJECT_URL})"


# One User-Agent for every AniList request (API, light novels/manga, covers), so
# AniList can tell Pudge apart and reach its author instead of blocking a bare
# library default.
ANILIST_USER_AGENT = _user_agent()


def _token_hash(token: str) -> str:
    value = str(token or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


# --------------------------------------------------------------------------
# Rate limiting shared by the window, the agent and CLI processes.
#
# AniList counts requests per minute on its side and reports what is left in
# X-RateLimit-Remaining, so that header is the shared counter: when it runs low
# every caller slows down, and a 429's Retry-After is written to disk so other
# processes wait instead of hitting the block too. A 403 (the API switched off,
# an IP or Cloudflare block) starts a cool-down of 15, then 30, then 60 minutes
# that every client honours without sending anything; any answered request ends it.
_LOGGER = logging.getLogger(__name__)
_RATE_LIMIT_DIR: Path | None = None
_RATE_LIMIT_LOCK = threading.Lock()
_PROCESS_COOLDOWN_UNTIL = 0.0
_PROCESS_COOLDOWN_STATUS = 0
_PROCESS_COOLDOWN_MESSAGE = ""
_PROCESS_FORBIDDEN_STREAK = 0
_FORBIDDEN_SEEN = False
ANILIST_MAX_INLINE_WAIT_SECONDS = 65.0
# A click in the window never waits for AniList's rate limit: it fails (or queues) at once.
ANILIST_MAX_INTERACTIVE_WAIT_SECONDS = 2.0
ANILIST_LOW_REMAINING = 5
ANILIST_FORBIDDEN_COOLDOWN_SECONDS = (15 * 60.0, 30 * 60.0, 60 * 60.0)
# Background bulk work (relation graphs, numbering chains) stays under the
# documented degraded-mode limit of 30 requests a minute, whatever the headers say.
ANILIST_BULK_INTERVAL_SECONDS = 2.0
_BULK_LAST_REQUEST_AT = 0.0
_INTERACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pudge_anilist_interactive", default=False
)
_BULK: contextvars.ContextVar[bool] = contextvars.ContextVar("pudge_anilist_bulk", default=False)
# Requests a user action may still send through an active 403 cool-down (one probe).
_PROBE: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("pudge_anilist_probe", default=None)
_VIEWER_IDS: dict[str, int] = {}


@contextmanager
def interactive_requests() -> Iterator[None]:
    """AniList calls made for a click in the window: no inline rate-limit waits."""
    token = _INTERACTIVE.set(True)
    try:
        yield
    finally:
        _INTERACTIVE.reset(token)


@contextmanager
def probe_requests() -> Iterator[None]:
    """A user asked for AniList now (Sync, the Settings check): one request may pass a 403 cool-down.

    If AniList answers it, the cool-down ends at once; if it refuses again, the
    cool-down keeps running unchanged and the rest of the call is refused locally.
    """
    token = _PROBE.set([1])
    try:
        yield
    finally:
        _PROBE.reset(token)


@contextmanager
def bulk_requests() -> Iterator[None]:
    """Background bulk AniList work: paced to at most 30 requests a minute."""
    token = _BULK.set(True)
    try:
        yield
    finally:
        _BULK.reset(token)


def request_modes() -> dict[str, bool]:
    """This thread's AniList request modes, for work handed to another process."""
    probe = _PROBE.get()
    return {
        "interactive": bool(_INTERACTIVE.get()),
        "bulk": bool(_BULK.get()),
        "probe": bool(probe and probe[0] > 0),
    }


@contextmanager
def requests_in_modes(modes: dict[str, Any] | None) -> Iterator[None]:
    """The request modes ``request_modes()`` gave in the other process, here."""
    modes = modes if isinstance(modes, dict) else {}
    with contextlib.ExitStack() as stack:
        if modes.get("interactive"):
            stack.enter_context(interactive_requests())
        if modes.get("bulk"):
            stack.enter_context(bulk_requests())
        if modes.get("probe"):
            stack.enter_context(probe_requests())
        yield


def configure_rate_limit_dir(cache_dir: Path | str | None) -> None:
    """Share AniList 429 cool-downs between processes through ``cache_dir``."""
    global _RATE_LIMIT_DIR
    _RATE_LIMIT_DIR = Path(cache_dir).expanduser() / "anilist-api" if cache_dir else None


def _read_marker(name: str) -> dict[str, Any]:
    if _RATE_LIMIT_DIR is None:
        return {}
    try:
        payload = json.loads((_RATE_LIMIT_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_marker(name: str, payload: dict[str, Any] | None) -> None:
    if _RATE_LIMIT_DIR is None:
        return
    marker = _RATE_LIMIT_DIR / name
    try:
        if payload is None:
            marker.unlink(missing_ok=True)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(marker)
    except OSError as exc:
        _LOGGER.warning("FALLBACK step=anilist.rate_limit reason=marker_write_failed error=%r", exc)


def _cooldown_state(now: float | None = None) -> tuple[float, int, str, int]:
    """(seconds left, HTTP status that started it, AniList's message, 403 streak)."""
    global _FORBIDDEN_SEEN
    moment = time.time() if now is None else float(now)
    until, status, message = _PROCESS_COOLDOWN_UNTIL, _PROCESS_COOLDOWN_STATUS, _PROCESS_COOLDOWN_MESSAGE
    streak = _PROCESS_FORBIDDEN_STREAK
    payload = _read_marker("rate-limit.json")
    if status == 403 and _RATE_LIMIT_DIR is not None and str(payload.get("status") or "") != "403":
        # Another process's probe found AniList answering again and ended the shared cool-down.
        until, status, message, streak = 0.0, 0, "", 0
    try:
        shared_until = float(payload.get("until") or 0.0)
        streak = max(streak, int(payload.get("streak") or 0))
    except (TypeError, ValueError):
        shared_until = 0.0
    if shared_until > until:
        until = shared_until
        status = int(payload.get("status") or 0) if str(payload.get("status") or "0").isdigit() else 0
        message = str(payload.get("message") or "")
    if streak or status == 403:
        _FORBIDDEN_SEEN = True
    return max(0.0, until - moment), int(status or 0), message, streak


def _set_cooldown(seconds: float, *, status: int, message: str = "", streak: int = 0) -> float:
    global _PROCESS_COOLDOWN_UNTIL, _PROCESS_COOLDOWN_STATUS, _PROCESS_COOLDOWN_MESSAGE
    seconds = max(1.0, min(3600.0, float(seconds)))
    until = time.time() + seconds
    with _RATE_LIMIT_LOCK:
        if until >= _PROCESS_COOLDOWN_UNTIL:
            _PROCESS_COOLDOWN_UNTIL = until
            _PROCESS_COOLDOWN_STATUS = int(status)
            _PROCESS_COOLDOWN_MESSAGE = str(message or "")
    payload: dict[str, Any] = {"until": until, "status": int(status)}
    if message:
        payload["message"] = str(message)[:300]
    if streak:
        payload["streak"] = int(streak)
    _write_marker("rate-limit.json", payload)
    return seconds


def _set_forbidden_cooldown(message: str) -> float:
    """Start (or lengthen) the shared 403 cool-down: 15, 30, then 60 minutes."""
    global _PROCESS_FORBIDDEN_STREAK, _FORBIDDEN_SEEN
    streak = _cooldown_state()[3] + 1
    with _RATE_LIMIT_LOCK:
        _PROCESS_FORBIDDEN_STREAK = streak
        _FORBIDDEN_SEEN = True
    steps = ANILIST_FORBIDDEN_COOLDOWN_SECONDS
    return _set_cooldown(steps[min(streak, len(steps)) - 1], status=403, message=message, streak=streak)


def _note_answered() -> None:
    """AniList answered a request: a 403 cool-down and its streak are over."""
    global _PROCESS_FORBIDDEN_STREAK, _FORBIDDEN_SEEN, _PROCESS_COOLDOWN_STATUS
    if not _FORBIDDEN_SEEN:
        return
    global _PROCESS_COOLDOWN_UNTIL
    with _RATE_LIMIT_LOCK:
        _FORBIDDEN_SEEN = False
        _PROCESS_FORBIDDEN_STREAK = 0
        if _PROCESS_COOLDOWN_STATUS == 403:
            # A probe got through the cool-down and AniList answered: it is over.
            _PROCESS_COOLDOWN_STATUS = 0
            _PROCESS_COOLDOWN_UNTIL = 0.0
    payload = _read_marker("rate-limit.json")
    if int(payload.get("streak") or 0) or str(payload.get("status") or "") == "403":
        if str(payload.get("status") or "") == "403":
            _write_marker("rate-limit.json", None)
        else:
            payload.pop("streak", None)
            _write_marker("rate-limit.json", payload)
    _LOGGER.info("DONE step=anilist.forbidden_cooldown reason=answered")


def anilist_refusal() -> dict[str, Any] | None:
    """The active 403 cool-down, for the window: AniList's message and minutes left."""
    remaining, status, message, _streak = _cooldown_state()
    if remaining <= 0 or status != 403:
        return None
    return {"status": 403, "message": message, "minutes": max(1, int((remaining + 59) // 60))}


def raise_if_refusing() -> None:
    """Raise at once (no request, no wait) while AniList is cooling down after a 403 or a 429."""
    remaining, status, message, _streak = _cooldown_state()
    if remaining <= 0:
        return
    minutes = max(1, int((remaining + 59) // 60))
    if status == 403:
        raise AniListHTTPError(
            err("anilist.refusedCooldown", message=message or "HTTP 403", minutes=minutes),
            status_code=403,
            detail=message,
            refusal=True,
        )
    raise AniListHTTPError(err("anilist.rateLimited", minutes=minutes), status_code=429)


def _mark_token_invalid(token_hash: str, detail: str) -> None:
    if not token_hash:
        return
    _write_marker("token-invalid.json", {"token": token_hash, "at": time.time(), "detail": detail[:200]})
    _LOGGER.warning("FAIL step=anilist.token reason=invalid detail=%r", detail[:200])


def token_marked_invalid(access_token: str) -> bool:
    """AniList rejected this exact token earlier and no request with it has worked since."""
    digest = _token_hash(access_token)
    return bool(digest) and _read_marker("token-invalid.json").get("token") == digest


def _note_token_works(token_hash: str) -> None:
    if token_hash and _RATE_LIMIT_DIR is not None and (_RATE_LIMIT_DIR / "token-invalid.json").exists():
        if _read_marker("token-invalid.json").get("token") == token_hash:
            _write_marker("token-invalid.json", None)


def _cached_viewer_id(token_hash: str) -> int | None:
    if not token_hash:
        return None
    if token_hash in _VIEWER_IDS:
        return _VIEWER_IDS[token_hash]
    try:
        value = int(_read_marker("viewer.json").get(token_hash) or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        _VIEWER_IDS[token_hash] = value
        return value
    return None


def _remember_viewer_id(token_hash: str, viewer_id: int) -> None:
    if not token_hash or viewer_id < 1:
        return
    _VIEWER_IDS[token_hash] = int(viewer_id)
    # Only hashes of tokens are stored; the newest few accounts are kept.
    payload = {key: value for key, value in _read_marker("viewer.json").items() if key != token_hash}
    payload[token_hash] = int(viewer_id)
    _write_marker("viewer.json", dict(list(payload.items())[-4:]))


def _forbidden_refuses_everything(response: Any, messages: list[str], query: str) -> bool:
    """A 403 that refuses AniList as a whole, not just this one request.

    The documented "API temporarily disabled" message, a Cloudflare/HTML block
    page, or a 403 on the Viewer or list query (which any account may read).
    """
    if any("temporarily disabled" in text.casefold() for text in messages):
        return True
    headers = getattr(response, "headers", None) or {}
    content_type = str(headers.get("content-type") or "").casefold()
    try:
        body = str(response.text or "").lstrip()[:200].casefold()
    except Exception:  # noqa: BLE001 - an unreadable body is not JSON either
        body = ""
    if not messages and ("html" in content_type or body.startswith("<") or "cloudflare" in body):
        return True
    return "Viewer" in query or "MediaListCollection" in query


def note_response(response: Any, *, access_token: str = "", query: str = "") -> None:
    """Feed a response from a client outside AniListClient into the shared cool-downs."""
    status = int(getattr(response, "status_code", 200) or 200)
    if status == 429:
        headers = getattr(response, "headers", None) or {}
        seconds = _set_cooldown(_retry_after_seconds(headers.get("Retry-After", "")), status=429)
        _LOGGER.warning("RATE_LIMIT step=anilist.http status=429 retry_after_s=%.0f", seconds)
    elif status == 403:
        messages = _error_messages(response)
        if _forbidden_refuses_everything(response, messages, query):
            _set_forbidden_cooldown("; ".join(messages)[:200] or "Forbidden")
    elif status < 500:
        # Any other answer (also a 404 or 400) means AniList is reachable again.
        _note_answered()
        if status < 400:
            _note_token_works(_token_hash(access_token))


def _retry_after_seconds(value: str, default: float = 60.0) -> float:
    try:
        return max(1.0, min(3600.0, float(str(value or "").strip())))
    except ValueError:
        return default


def _header_int(headers: Any, name: str) -> int | None:
    try:
        value = headers.get(name)
        return int(str(value).strip()) if value is not None and str(value).strip() else None
    except (TypeError, ValueError, AttributeError):
        return None


def _wait_for_cooldown(*, interactive: bool = False) -> bool:
    """Wait for (or refuse during) a cool-down; True when this request is a user's probe through a 403 one."""
    wait, status, _message, _streak = _cooldown_state()
    if wait <= 0:
        return False
    probe = _PROBE.get()
    if status == 403 and probe and probe[0] > 0:
        probe[0] -= 1
        _LOGGER.info("PROBE step=anilist.forbidden_cooldown remaining_s=%.0f", wait)
        return True
    limit = ANILIST_MAX_INTERACTIVE_WAIT_SECONDS if interactive else ANILIST_MAX_INLINE_WAIT_SECONDS
    if status == 403 or wait > limit:
        raise_if_refusing()
    _LOGGER.info("WAIT step=anilist.rate_limit wait_s=%.1f", wait)
    time.sleep(wait)
    return False


def _pace_bulk() -> None:
    """Space background bulk requests of this process ANILIST_BULK_INTERVAL_SECONDS apart."""
    global _BULK_LAST_REQUEST_AT
    with _RATE_LIMIT_LOCK:
        now = time.monotonic()
        wait = _BULK_LAST_REQUEST_AT + ANILIST_BULK_INTERVAL_SECONDS - now
        _BULK_LAST_REQUEST_AT = max(now, _BULK_LAST_REQUEST_AT + ANILIST_BULK_INTERVAL_SECONDS)
    if wait > 0:
        time.sleep(wait)


def _error_messages(response: Any) -> list[str]:
    """AniList's GraphQL ``errors[].message`` texts of a response ([] for HTML or no body)."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - Cloudflare HTML, empty body
        return []
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return []
    messages: list[str] = []
    for item in errors:
        text = str(item.get("message") or "").strip() if isinstance(item, dict) else ""
        if text and text not in messages:
            messages.append(text)
    return messages


def _pace_after(response: Any) -> None:
    """Slow down while AniList reports few requests left in this minute."""
    headers = getattr(response, "headers", None)
    remaining = _header_int(headers, "X-RateLimit-Remaining")
    if remaining is None or remaining > ANILIST_LOW_REMAINING:
        return
    limit = max(1, _header_int(headers, "X-RateLimit-Limit") or 90)
    if remaining <= 0:
        # The next request would be refused: let a few of this minute's requests age out.
        _set_cooldown(10 * 60.0 / limit, status=200)
        return
    time.sleep(60.0 / limit)


def _as_title_list(media: dict[str, Any]) -> list[str]:
    title = media.get("title") or {}
    values = [title.get("romaji"), title.get("english"), title.get("native"), title.get("userPreferred")]
    return list(dict.fromkeys(str(v) for v in values if v))


_FRANCHISE_NOISE_TERMS = {
    "anime",
    "chapter",
    "episode",
    "episodes",
    "final",
    "movie",
    "part",
    "season",
    "series",
    "special",
    "the",
}


def _franchise_title_parts(media: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return meaningful title terms and compact aliases for franchise checks."""
    terms: set[str] = set()
    compact: set[str] = set()
    for title in _as_title_list(media):
        normalized = normalize_title(title)
        if not normalized:
            continue
        tokens = [token for token in normalized.split() if token]
        joined = "".join(tokens)
        if len(joined) >= 5:
            compact.add(joined)
        for token in tokens:
            if len(token) >= 5 and token not in _FRANCHISE_NOISE_TERMS:
                terms.add(token)
    return terms, compact


def _parent_matches_root_franchise(
    root_media: dict[str, Any] | None,
    parent_node: dict[str, Any],
) -> bool:
    """Reject crossover parents that would pull an unrelated franchise.

    AniList can model joke crossovers with multiple PARENT edges.  Starting
    from Monogatari, for example, Nisekoimonogatari points both to
    Bakemonogatari and Nisekoi.  A parent is allowed to expand only when its
    titles share a meaningful franchise term with the graph root.
    """
    if not isinstance(root_media, dict):
        return True
    root_terms, root_compact = _franchise_title_parts(root_media)
    parent_terms, parent_compact = _franchise_title_parts(parent_node)
    if not root_terms or not parent_compact:
        return False
    if any(term in alias for term in root_terms for alias in parent_compact):
        return True
    return any(term in alias for term in parent_terms for alias in root_compact)


def _as_date_string(value: Any) -> str | None:
    """Preserve AniList partial dates (year or year-month).

    AniList commonly omits the exact day for upcoming or recently announced
    anime. Dropping the whole date made same-year relation ordering fall back to
    December 31 and could place a summer follow-up before a spring TV series.
    """
    if not isinstance(value, dict):
        return None
    try:
        year = int(value.get("year") or 0)
        month = int(value.get("month") or 0)
        day = int(value.get("day") or 0)
    except (TypeError, ValueError):
        return None
    if year <= 0:
        return None
    if month <= 0:
        return f"{year:04d}"
    if not 1 <= month <= 12:
        return None
    if day <= 0:
        return f"{year:04d}-{month:02d}"
    if not 1 <= day <= 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _list_added_at(entry: dict[str, Any]) -> float:
    """MediaList.createdAt (unix seconds) of a list entry; 0 when missing."""
    try:
        return max(0.0, float(entry.get("createdAt") or 0))
    except (TypeError, ValueError):
        return 0.0


def _primary_studio(media: dict[str, Any]) -> str:
    nodes = (media.get("studios") or {}).get("nodes") or []
    valid = [node for node in nodes if isinstance(node, dict) and node.get("name")]
    for node in valid:
        if node.get("isAnimationStudio") is True:
            return str(node["name"])
    return str(valid[0]["name"]) if valid else ""


def _relation_watched(node: dict[str, Any]) -> tuple[str, int, bool]:
    entry = node.get("mediaListEntry") or {}
    list_status = str(entry.get("status") or "").upper()
    progress = int(entry.get("progress") or 0)
    episodes = int(node["episodes"]) if node.get("episodes") else None
    watched = list_status in {"COMPLETED", "REPEATING"} or bool(episodes and progress >= episodes)
    return list_status, progress, watched


def _relation_node_payload(node: dict[str, Any], relation_type: str) -> dict[str, Any] | None:
    if str(node.get("type") or "ANIME").upper() != "ANIME" or not node.get("id"):
        return None
    media_id = int(node["id"])
    titles = _as_title_list(node)
    cover = node.get("coverImage") or {}
    list_status, progress, watched = _relation_watched(node)
    return {
        "relation_type": relation_type,
        "media_id": media_id,
        "title": titles[0] if titles else str(media_id),
        "site_url": str(node.get("siteUrl") or f"https://anilist.co/anime/{media_id}"),
        "format": str(node.get("format")) if node.get("format") else None,
        "season_year": int(node["seasonYear"]) if node.get("seasonYear") else None,
        "start_date": _as_date_string(node.get("startDate")),
        "studio": _primary_studio(node),
        "episodes": int(node["episodes"]) if node.get("episodes") else None,
        "cover_url": str(cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""),
        "media_status": str(node.get("status") or ""),
        "list_status": list_status,
        "progress": progress,
        "watched": watched,
    }


def _as_anime(media: dict[str, Any], *, score: float = 0.0) -> AniListAnime:
    episodes = media.get("episodes")
    return AniListAnime(
        id=int(media["id"]),
        titles=_as_title_list(media),
        synonyms=[str(value) for value in (media.get("synonyms") or []) if value],
        season_year=int(media["seasonYear"]) if media.get("seasonYear") else None,
        episodes=int(episodes) if episodes else None,
        format=str(media.get("format")) if media.get("format") else None,
        score=score,
    )


class AniListClient:
    def __init__(self, endpoint: str, access_token: str = "", *, timeout: float = 20.0) -> None:
        self.endpoint = endpoint
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": ANILIST_USER_AGENT,
        }
        if access_token.strip():
            headers["Authorization"] = f"Bearer {access_token.strip()}"
        self._token_hash = _token_hash(access_token)
        self.client = httpx.Client(
            timeout=max(1.0, float(timeout)),
            follow_redirects=True,
            headers=headers,
        )
        # A client made for a click in the window never sleeps for AniList's
        # rate limit; interactive_requests() does the same for a whole call.
        self.interactive = False
        self.last_list_entries: dict[int, dict[str, Any]] = {}
        self.last_list_media: dict[int, LibraryAnime] = {}
        self.last_list_incomplete = False

    def close(self) -> None:
        self.client.close()

    def _http_failure(self, response: Any, *, probing: bool = False, query: str = "") -> AniListHTTPError:
        status = int(response.status_code)
        messages = _error_messages(response)
        detail = "; ".join(messages)[:200]
        try:
            body = " ".join(str(response.text or "").split())[:300]
        except Exception:  # noqa: BLE001 - only for the log
            body = ""
        headers = getattr(response, "headers", None) or {}
        server = str(headers.get("server") or "")
        ray = str(headers.get("cf-ray") or "")
        # Why AniList refused: the API switched off (JSON message), a manual block
        # or Cloudflare (HTML, server=cloudflare with a cf-ray id).
        _LOGGER.warning(
            "FAIL step=anilist.http status=%s server=%r cf_ray=%r body=%r", status, server, ray, body
        )
        if status not in {403, 429} and status < 500:
            # AniList answered (a 404 or a 400 too): a 403 cool-down is over.
            _note_answered()
        if status == 404:
            message = (
                err("anilist.httpMessage", status=status, message=detail)
                if detail
                else err("anilist.http", status=status, endpoint=self.endpoint)
            )
            return AniListNotFoundHTTPError(message, status_code=status, detail=detail)
        if self._token_hash and (
            status == 401 or (status == 400 and any("invalid token" in text.casefold() for text in messages))
        ):
            _mark_token_invalid(self._token_hash, detail or f"HTTP {status}")
            return AniListAuthError(err("anilist.tokenInvalid"), status_code=status, detail=detail)
        if status == 403:
            reason = detail or (f"Forbidden ({server})" if server else "Forbidden")
            refusal = _forbidden_refuses_everything(response, messages, query)
            if refusal and probing:
                # A user's probe through the cool-down: AniList still refuses; it runs on as it was.
                _LOGGER.warning("BACKOFF step=anilist.http status=403 probe=failed reason=%r", reason)
            elif refusal:
                seconds = _set_forbidden_cooldown(reason)
                _LOGGER.warning(
                    "BACKOFF step=anilist.http status=403 cooldown_s=%.0f reason=%r", seconds, reason
                )
            return AniListHTTPError(
                err("anilist.refused", status=status, message=reason),
                status_code=403,
                detail=reason,
                refusal=refusal,
            )
        if detail:
            return AniListHTTPError(
                err("anilist.httpMessage", status=status, message=detail), status_code=status, detail=detail
            )
        return AniListHTTPError(
            err("anilist.http", status=status, endpoint=self.endpoint), status_code=status
        )

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        transient_statuses = {500, 502, 503, 504}
        interactive = bool(getattr(self, "interactive", False)) or _INTERACTIVE.get()
        last_error: Exception | None = None
        rate_limited_retries = 0
        attempt = 0
        while attempt < 2:
            probing = _wait_for_cooldown(interactive=interactive)
            if _BULK.get() and not interactive:
                _pace_bulk()
            try:
                response = self.client.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                )
                if int(getattr(response, "status_code", 200) or 200) == 429:
                    seconds = _set_cooldown(
                        _retry_after_seconds(response.headers.get("Retry-After", "")),
                        status=429,
                    )
                    _LOGGER.warning("RATE_LIMIT step=anilist.http status=429 retry_after_s=%.0f", seconds)
                    if (
                        not interactive
                        and rate_limited_retries == 0
                        and seconds <= ANILIST_MAX_INLINE_WAIT_SECONDS
                    ):
                        rate_limited_retries += 1
                        continue
                    raise AniListHTTPError(
                        err("anilist.rateLimited", minutes=max(1, int((seconds + 59) // 60))),
                        status_code=429,
                    )
                _pace_after(response)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = int(exc.response.status_code)
                if status in transient_statuses and attempt == 0:
                    attempt += 1
                    time.sleep(0.25)
                    continue
                raise self._http_failure(exc.response, probing=probing, query=query) from exc
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 0:
                    attempt += 1
                    time.sleep(0.25)
                    continue
                raise AniListNetworkError(err("anilist", error=exc)) from exc
            except ValueError as exc:
                raise AniListError(err("anilist.invalidJson", error=exc)) from exc

            _note_answered()
            _note_token_works(self._token_hash)
            if not isinstance(payload, dict):
                # A JSON list or string (a proxy or portal page): AttributeError escaped
                # every caller, which handle AniListError only.
                raise AniListError(err("anilist.noData"))
            if payload.get("errors"):
                raise AniListError(err("anilist.returnedError", error=payload["errors"]))
            data = payload.get("data")
            if not isinstance(data, dict):
                raise AniListError(err("anilist.noData"))
            return data

        raise AniListNetworkError(err("anilist", error=last_error))

    def viewer(self) -> dict[str, Any]:
        viewer = self._post(VIEWER_QUERY).get("Viewer")
        if not isinstance(viewer, dict):
            raise AniListError(err("anilist.viewerUnknown"))
        if viewer.get("id"):
            _remember_viewer_id(self._token_hash, int(viewer["id"]))
        return viewer

    def viewer_id(self) -> int:
        """The token owner's AniList user id; asked once per token, not on every sync."""
        cached = _cached_viewer_id(self._token_hash)
        if cached:
            return cached
        return int(self.viewer()["id"])

    def list_entry(self, media_id: int) -> dict[str, Any] | None:
        """The viewer's current list entry for one anime (None when it is not on the list)."""
        return self._minimal_list_entry(int(media_id))

    def related_media_addition_notifications(
        self, *, page: int = 1, per_page: int = 20
    ) -> list[dict[str, Any]]:
        """Return AniList RELATED_MEDIA_ADDITION notifications without marking them read."""
        data = self._post(
            RELATED_MEDIA_ADDITION_NOTIFICATIONS_QUERY,
            {
                "page": max(1, int(page)),
                "perPage": max(1, min(50, int(per_page))),
            },
        )
        page_payload = data.get("Page") or {}
        rows = page_payload.get("notifications") or []
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id") or not row.get("mediaId"):
                continue
            media = row.get("media") or {}
            if not isinstance(media, dict):
                media = {}
            titles = _as_title_list(media)
            cover = media.get("coverImage") or {}
            result.append(
                {
                    "id": int(row["id"]),
                    "type": str(row.get("type") or "RELATED_MEDIA_ADDITION"),
                    "media_id": int(row["mediaId"]),
                    "media_type": str(media.get("type") or ""),
                    "title": titles[0] if titles else str(row.get("mediaId")),
                    "context": str(row.get("context") or ""),
                    "created_at": int(row.get("createdAt") or 0),
                    "cover": str(cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""),
                    "site_url": str(
                        media.get("siteUrl") or f"https://anilist.co/anime/{int(row['mediaId'])}"
                    ),
                    "media_status": str(media.get("status") or ""),
                    "format": str(media.get("format") or ""),
                    "start_date": _as_date_string(media.get("startDate")),
                    "list_status": str((media.get("mediaListEntry") or {}).get("status") or "").upper(),
                }
            )
        return result

    def library(self) -> list[LibraryAnime]:
        """The viewer's AniList anime list (Watching/Planning cards plus every entry's status).

        Relation trees are not part of it: they come from the multi-day relation
        graph cache (full_relation_graph).
        """
        data = self._post(LIBRARY_QUERY, {"userId": self.viewer_id()})
        collection = data.get("MediaListCollection") or {}
        groups = collection.get("lists") or []
        list_entries: dict[int, dict[str, Any]] = {}
        # An entry whose media came back null (hidden, merged, a partial answer):
        # the list cannot prove that a missing title was removed.
        self.last_list_incomplete = False
        for group in groups:
            group_status = str(group.get("status") or "")
            for entry in group.get("entries") or []:
                media = (entry or {}).get("media") or {}
                if not media.get("id"):
                    self.last_list_incomplete = True
                    continue
                list_entries[int(media["id"])] = {
                    "status": str(entry.get("status") or group_status or "").upper(),
                    "progress": int(entry.get("progress") or 0),
                    "episodes": int(media["episodes"]) if media.get("episodes") else None,
                }
        # Every list entry (Completed, Repeating, Paused ... too), for callers
        # that keep local episodes of titles outside Watching/Planning.
        self.last_list_entries = list_entries

        result: list[LibraryAnime] = []
        # The metadata of every entry (Completed ... too): a local row made before its
        # title was known (a placeholder) is filled from the same answer.
        list_media: dict[int, LibraryAnime] = {}
        for group in groups:
            group_status = str(group.get("status") or "")
            for entry in group.get("entries") or []:
                media = (entry or {}).get("media") or {}
                if not media.get("id"):
                    continue
                titles = _as_title_list(media)
                cover = media.get("coverImage") or {}
                airing = media.get("nextAiringEpisode") or {}
                entry_status = str(entry.get("status") or group.get("status") or "")
                result.append(
                    LibraryAnime(
                        media_id=int(media["id"]),
                        title=(titles[0] if titles else str(media["id"])),
                        titles=titles,
                        synonyms=[str(x) for x in (media.get("synonyms") or []) if x],
                        cover_url=str(
                            cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""
                        ),
                        site_url=str(media.get("siteUrl") or f"https://anilist.co/anime/{media['id']}"),
                        status=entry_status,
                        progress=int(entry.get("progress") or 0),
                        episodes=int(media["episodes"]) if media.get("episodes") else None,
                        format=str(media.get("format")) if media.get("format") else None,
                        season_year=int(media["seasonYear"]) if media.get("seasonYear") else None,
                        start_date=_as_date_string(media.get("startDate")),
                        studio=_primary_studio(media),
                        media_status=str(media.get("status")) if media.get("status") else None,
                        end_date=_as_date_string(media.get("endDate")),
                        mean_score=int(media["meanScore"]) if media.get("meanScore") is not None else None,
                        user_score=float(entry["score"]) if entry.get("score") is not None else None,
                        duration=int(media["duration"]) if media.get("duration") is not None else None,
                        next_airing_episode=(int(airing["episode"]) if airing.get("episode") else None),
                        next_airing_at=int(airing["airingAt"]) if airing.get("airingAt") else None,
                        list_added_at=_list_added_at(entry),
                    )
                )
                list_media[int(media["id"])] = result.pop()
                if group_status in {"CURRENT", "PLANNING"}:
                    result.append(list_media[int(media["id"])])
        self.last_list_media = list_media
        result.sort(key=lambda item: (item.status != "CURRENT", item.title.casefold()))
        return result

    def library_compact(self) -> list[LibraryAnime]:
        """The list without relation trees (the only kind of list fetch)."""
        return self.library()

    def search(self, identity: VideoIdentity) -> list[AniListAnime]:
        data = self._post(QUERY, {"search": identity.title})
        media_items = (data.get("Page") or {}).get("media") or []
        result: list[AniListAnime] = []
        for media in media_items:
            titles = _as_title_list(media)
            synonyms = [str(value) for value in (media.get("synonyms") or []) if value]
            all_names = [*titles, *synonyms]
            score = max((title_similarity(identity.title, name) for name in all_names), default=0.0)
            if identity.year and media.get("seasonYear"):
                score += 8 if identity.year == int(media["seasonYear"]) else -3
            episodes = media.get("episodes")
            if identity.episode and episodes:
                score += 4 if identity.episode <= int(episodes) else -20
            result.append(_as_anime(media, score=score))
        result.sort(key=lambda item: item.score, reverse=True)
        return result

    def get_anime(self, media_id: int) -> AniListAnime:
        media = self._post(LIST_ENTRY_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListNotFoundError(err("anilist.animeNotFound", mediaId=media_id))
        return _as_anime(media, score=1000.0)

    def library_anime(self, media_id: int) -> LibraryAnime:
        """Fetch one anime with enough list/airing metadata for download decisions."""
        media = self._post(LIST_ENTRY_QUERY, {"mediaId": int(media_id)}).get("Media")
        if not isinstance(media, dict):
            raise AniListNotFoundError(err("anilist.animeNotFound", mediaId=media_id))
        titles = _as_title_list(media)
        cover = media.get("coverImage") or {}
        entry = media.get("mediaListEntry") or {}
        airing = media.get("nextAiringEpisode") or {}
        return LibraryAnime(
            media_id=int(media["id"]),
            title=titles[0] if titles else str(media["id"]),
            titles=titles,
            synonyms=[str(value) for value in (media.get("synonyms") or []) if value],
            cover_url=str(cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""),
            site_url=str(media.get("siteUrl") or f"https://anilist.co/anime/{int(media['id'])}"),
            status=str(entry.get("status") or ""),
            progress=int(entry.get("progress") or 0),
            episodes=int(media["episodes"]) if media.get("episodes") else None,
            format=str(media.get("format") or "") or None,
            season_year=int(media["seasonYear"]) if media.get("seasonYear") else None,
            start_date=_as_date_string(media.get("startDate")),
            studio=_primary_studio(media),
            media_status=str(media.get("status") or "") or None,
            end_date=_as_date_string(media.get("endDate")),
            mean_score=int(media["meanScore"]) if media.get("meanScore") is not None else None,
            user_score=float(entry["score"]) if entry.get("score") is not None else None,
            duration=int(media["duration"]) if media.get("duration") is not None else None,
            next_airing_episode=int(airing["episode"]) if airing.get("episode") else None,
            next_airing_at=int(airing["airingAt"]) if airing.get("airingAt") else None,
            list_added_at=_list_added_at(entry),
        )

    def episode_airing_at(self, media_id: int, episode: int) -> int | None:
        if media_id < 1 or episode < 1:
            return None
        schedule = self._post(
            AIRING_SCHEDULE_QUERY,
            {"mediaId": int(media_id), "episode": int(episode)},
        ).get("AiringSchedule")
        if not isinstance(schedule, dict) or not schedule.get("airingAt"):
            return None
        return int(schedule["airingAt"])

    def full_relation_graph(
        self,
        media_id: int,
        *,
        max_nodes: int = 250,
    ) -> dict[str, Any]:
        """Fetch every anime entry reachable through AniList relations.

        AniList does not expose recursive relations in one fixed GraphQL
        selection.  We therefore traverse the connected component in batches of
        up to 50 media IDs; each request also returns the neighbours' own
        fields and edges, so it covers two levels and the neighbours need no
        request of their own.  The safety cap prevents pathological franchise
        graphs from blocking the UI forever; callers receive ``truncated=True``
        when it is reached.
        """
        root_id = int(media_id)
        max_nodes = max(1, int(max_nodes))
        pending: list[int] = [root_id]
        requested: set[int] = set()
        nodes: dict[int, dict[str, Any]] = {}
        edge_keys: set[tuple[int, int, str]] = set()
        edges: list[dict[str, Any]] = []
        truncated = False
        root_media: dict[str, Any] | None = None
        terminal_alternative_ids: set[int] = set()
        expandable_ids: set[int] = {root_id}
        # Neighbours returned nested in an earlier response: complete enough to
        # be processed like a fetched entry when the traversal reaches them.
        prefetched: dict[int, dict[str, Any]] = {}
        from_nested: set[int] = set()

        while pending:
            batch: list[int] = []
            while pending and len(batch) < 50:
                candidate = int(pending.pop(0))
                if candidate in requested:
                    continue
                requested.add(candidate)
                batch.append(candidate)
            if not batch:
                continue

            fetched: dict[int, dict[str, Any]] = {}
            missing = [candidate for candidate in batch if candidate not in prefetched]
            if missing:
                for media in self._relation_graph_page(missing):
                    fetched[int(media["id"])] = media
                    for relation in (media.get("relations") or {}).get("edges") or []:
                        nested = relation.get("node") if isinstance(relation, dict) else None
                        if (
                            isinstance(nested, dict)
                            and nested.get("id")
                            and "relations" in nested
                            and int(nested["id"]) not in requested
                        ):
                            prefetched.setdefault(int(nested["id"]), nested)
            media_items = []
            for candidate in batch:
                if candidate in fetched:
                    media_items.append(fetched[candidate])
                elif candidate in prefetched:
                    from_nested.add(candidate)
                    media_items.append(prefetched.pop(candidate))

            for media in media_items:
                if not isinstance(media, dict) or not media.get("id"):
                    continue
                current_format = str(media.get("format") or "").upper()
                # Music videos are not useful watch-order entries and can pull
                # large unrelated character/song components into a franchise.
                if current_format == "MUSIC":
                    continue
                current_id = int(media["id"])
                if current_id == root_id:
                    root_media = media
                payload = _relation_node_payload(media, "SELF")
                if payload is None:
                    continue
                payload.pop("relation_type", None)
                nodes[current_id] = payload

                relation_edges = (media.get("relations") or {}).get("edges") or []
                # Alternative versions are useful as leaf nodes, but following
                # their own prequel/sequel trees merges a second watch order
                # into the selected franchise.  Only the selected root may
                # expand when it itself is an alternative entry.
                if current_id in terminal_alternative_ids and current_id != root_id:
                    relation_edges = []
                for relation in relation_edges:
                    if not isinstance(relation, dict):
                        continue
                    relation_type = str(relation.get("relationType") or "").upper()
                    target = relation.get("node")
                    if (
                        not relation_type
                        or not isinstance(target, dict)
                        or str(target.get("type") or "").upper() != "ANIME"
                        or not target.get("id")
                    ):
                        continue
                    target_id = int(target["id"])
                    target_format = str(target.get("format") or "").upper()
                    if target_id == current_id or target_format == "MUSIC":
                        continue
                    if relation_type in {"CHARACTER", "SHARED_CHARACTERS", "RELATED", "OTHER"}:
                        continue
                    if relation_type == "ALTERNATIVE" and target_id != root_id:
                        if target_id not in expandable_ids:
                            terminal_alternative_ids.add(target_id)
                    else:
                        expandable_ids.add(target_id)
                        terminal_alternative_ids.discard(target_id)
                    if (
                        relation_type == "PARENT"
                        and current_id != root_id
                        and not _parent_matches_root_franchise(root_media, target)
                    ):
                        continue

                    # Normalize PREQUEL/SEQUEL into a single chronological
                    # direction so the UI can build one horizontal backbone.
                    if relation_type == "PREQUEL":
                        source_id, destination_id, normalized_type = (
                            target_id,
                            current_id,
                            "SEQUEL",
                        )
                    elif relation_type == "SEQUEL":
                        source_id, destination_id, normalized_type = (
                            current_id,
                            target_id,
                            "SEQUEL",
                        )
                    else:
                        source_id, destination_id = sorted((current_id, target_id))
                        normalized_type = relation_type

                    key = (source_id, destination_id, normalized_type)
                    if key not in edge_keys:
                        edge_keys.add(key)
                        edges.append(
                            {
                                "source": source_id,
                                "target": destination_id,
                                "relation_type": normalized_type,
                            }
                        )

                    if target_id not in requested and target_id not in pending:
                        if len(requested) + len(pending) >= max_nodes:
                            truncated = True
                        else:
                            pending.append(target_id)

            if len(requested) >= max_nodes and pending:
                truncated = True
                pending.clear()

        if root_id not in nodes:
            raise AniListError(err("anilist.graphMissingRoot", rootId=root_id))
        self._fill_nested_list_entries(nodes, from_nested)

        nodes_list = sorted(
            nodes.values(),
            key=lambda item: (
                item.get("start_date") or f"{int(item.get('season_year') or 9999):04d}",
                int(item.get("media_id") or 0),
            ),
        )
        edges.sort(
            key=lambda item: (
                item["relation_type"] != "SEQUEL",
                item["source"],
                item["target"],
                item["relation_type"],
            )
        )
        return {
            "root_id": root_id,
            "nodes": nodes_list,
            "edges": edges,
            "truncated": truncated,
            "partial": False,
        }

    # False once AniList refused the two-level selection (for example a query
    # complexity limit): the one-level query then serves every later graph.
    _nested_graph_query = True

    def _relation_graph_page(self, ids: list[int]) -> list[dict[str, Any]]:
        query = (
            FULL_RELATION_GRAPH_NESTED_QUERY
            if AniListClient._nested_graph_query
            else FULL_RELATION_GRAPH_QUERY
        )
        try:
            page = self._post(query, {"ids": ids}).get("Page")
        except AniListError as exc:
            rejected = (
                (isinstance(exc, AniListHTTPError) and (exc.status_code == 400 or exc.status_code >= 500))
                or (
                    isinstance(exc, AniListNetworkError) and isinstance(exc.__cause__, httpx.TimeoutException)
                )
                or type(exc) is AniListError
            )
            # Too heavy for AniList right now (complexity limit, a 5xx after the
            # retry, a read timeout): the one-level query serves from now on.
            if query is FULL_RELATION_GRAPH_QUERY or isinstance(exc, AniListAuthError) or not rejected:
                raise
            AniListClient._nested_graph_query = False
            _LOGGER.warning("FALLBACK step=anilist.relation_graph reason=nested_query_rejected error=%r", exc)
            page = self._post(FULL_RELATION_GRAPH_QUERY, {"ids": ids}).get("Page")
        media_items = (page or {}).get("media") if isinstance(page, dict) else None
        return [media for media in media_items or [] if isinstance(media, dict) and media.get("id")]

    def _fill_nested_list_entries(self, nodes: dict[int, dict[str, Any]], media_ids: set[int]) -> None:
        """Read the viewer's list status of entries seen only nested in another entry's response.

        AniList does not reliably fill ``mediaListEntry`` on nested relation
        nodes; one batched read per 50 entries keeps Watched/Planning marks right.
        """
        wanted = sorted(media_id for media_id in media_ids if media_id in nodes)
        if not wanted or not self._token_hash:
            return
        for offset in range(0, len(wanted), 50):
            chunk = wanted[offset : offset + 50]
            try:
                page = self._post(RELATION_LIST_ENTRIES_QUERY, {"ids": chunk}).get("Page")
            except AniListError as exc:
                _LOGGER.warning("FALLBACK step=anilist.relation_graph_list_entries error=%r", exc)
                return
            rows = page.get("media") if isinstance(page, dict) else None
            for media in rows if isinstance(rows, list) else []:
                if not isinstance(media, dict) or not media.get("id") or int(media["id"]) not in nodes:
                    continue
                node = nodes[int(media["id"])]
                list_status, progress, watched = _relation_watched(
                    {"mediaListEntry": media.get("mediaListEntry"), "episodes": node.get("episodes")}
                )
                node.update(list_status=list_status, progress=progress, watched=watched)

    def get_anime_with_relations(self, media_id: int) -> tuple[AniListAnime, list[tuple[str, AniListAnime]]]:
        media = self._post(RELATIONS_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListNotFoundError(err("anilist.animeNotFound", mediaId=media_id))

        relations: list[tuple[str, AniListAnime]] = []
        edges = (media.get("relations") or {}).get("edges") or []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            relation_type = str((edge or {}).get("relationType") or "")
            if not isinstance(node, dict) or not relation_type:
                continue
            try:
                relations.append((relation_type, _as_anime(node)))
            except (KeyError, TypeError, ValueError):
                continue
        return _as_anime(media, score=1000.0), relations

    def resolve_absolute_episode(
        self,
        start: AniListAnime,
        absolute_episode: int,
        *,
        max_hops: int = 12,
    ) -> tuple[AniListAnime, int, list[AniListAnime]] | None:
        """Map absolute numbering across TV/ONA sequel cours.

        ``start`` may be any cour from the chain (including a stale cached
        mapping). We first follow eligible PREQUEL edges to the beginning of
        the segmented arc, then subtract cour lengths while following SEQUEL.
        """
        if absolute_episode < 1:
            return None

        valid_formats = {"TV", "TV_SHORT", "ONA"}

        def relation_rank(source: AniListAnime, candidate: AniListAnime) -> tuple[float, float, int]:
            source_names = [*source.titles, *source.synonyms]
            candidate_names = [*candidate.titles, *candidate.synonyms]
            continuity = max(
                (title_similarity(left, right) for left in source_names for right in candidate_names),
                default=0.0,
            )
            year_gap = abs((candidate.season_year or 9999) - (source.season_year or 9999))
            return continuity, -float(year_gap), -candidate.id

        # Recover the beginning of a split-cour arc. A predecessor with at
        # least as many episodes as the absolute number is normally the old
        # parent series (for example original BLEACH with 366 episodes), not
        # another cour of the current arc.
        current = start
        backward = [current]
        visited = {current.id}
        for _ in range(max_hops):
            _, relations = self.get_anime_with_relations(current.id)
            prequels = [
                anime
                for relation_type, anime in relations
                if relation_type == "PREQUEL"
                and anime.id not in visited
                and (anime.format or "").upper() in valid_formats
                and anime.episodes is not None
                and 0 < anime.episodes < absolute_episode
            ]
            if not prequels:
                break
            current = max(prequels, key=lambda anime: relation_rank(backward[0], anime))
            visited.add(current.id)
            backward.insert(0, current)

        remaining = absolute_episode
        chain = [backward[0]]
        current = backward[0]
        visited = {current.id}

        for _ in range(max_hops + 1):
            if current.episodes is None:
                # AniList may not publish the final episode count for an
                # airing cour yet. The remaining absolute offset is still the
                # correct relative episode for this latest known sequel.
                return current, remaining, chain
            if current.episodes < 1:
                return None
            if remaining <= current.episodes:
                return current, remaining, chain

            remaining -= current.episodes
            _, relations = self.get_anime_with_relations(current.id)
            sequels = [
                anime
                for relation_type, anime in relations
                if relation_type == "SEQUEL"
                and anime.id not in visited
                and (anime.format or "").upper() in valid_formats
                and (anime.episodes is None or anime.episodes > 0)
            ]
            if not sequels:
                return None

            current = max(sequels, key=lambda anime: relation_rank(chain[-1], anime))
            visited.add(current.id)
            chain.append(current)

        return None

    def absolute_episode_number(
        self,
        start: AniListAnime,
        relative_episode: int,
        *,
        max_hops: int = 12,
    ) -> tuple[int, list[AniListAnime]]:
        """Convert a cour-relative episode to franchise/cour absolute numbering.

        Some Jimaku uploads use ``S02E17`` for season two episode five after a
        twelve-episode first season. The normal relative number remains the
        primary identity; this method only supplies an additional safe alias.
        """
        if relative_episode < 1:
            return relative_episode, [start]

        counted_formats = {"TV", "TV_SHORT", "ONA"}
        bridge_formats = {"OVA", "SPECIAL", "MOVIE"}

        def relation_rank(source: AniListAnime, candidate: AniListAnime) -> tuple[float, float, int]:
            source_names = [*source.titles, *source.synonyms]
            candidate_names = [*candidate.titles, *candidate.synonyms]
            continuity = max(
                (title_similarity(left, right) for left in source_names for right in candidate_names),
                default=0.0,
            )
            year_gap = abs((candidate.season_year or 9999) - (source.season_year or 9999))
            return continuity, -float(year_gap), -candidate.id

        total = int(relative_episode)
        current = start
        chain = [start]
        visited = {start.id}
        for _ in range(max_hops):
            _, relations = self.get_anime_with_relations(current.id)
            prequels = []
            for relation_type, anime in relations:
                if relation_type != "PREQUEL" or anime.id in visited:
                    continue
                candidate_format = (anime.format or "").upper()
                if candidate_format in counted_formats:
                    if anime.episodes is None or anime.episodes <= 0:
                        continue
                elif candidate_format not in bridge_formats:
                    continue
                prequels.append(anime)
            if not prequels:
                break
            candidate = max(prequels, key=lambda anime: relation_rank(current, anime))
            continuity = relation_rank(current, candidate)[0]
            # Do not cross into a loosely related parent show or a long-running
            # predecessor that uses its own numbering. Split-cour / seasonal
            # chains are typically bounded entries, while e.g. original BLEACH
            # (366 episodes) must not be added to Thousand-Year Blood War.
            episode_cap = max(100, int(start.episodes or 0) * 4)
            counted = (candidate.format or "").upper() in counted_formats
            if continuity < 35.0 or (counted and int(candidate.episodes or 0) > episode_cap):
                break
            if counted:
                total += int(candidate.episodes or 0)
            visited.add(candidate.id)
            chain.insert(0, candidate)
            current = candidate
        return total, chain

    def _minimal_list_entry(self, media_id: int) -> dict[str, Any] | None:
        media = self._post(
            MINIMAL_LIST_ENTRY_QUERY,
            {"mediaId": int(media_id)},
        ).get("Media")
        entry = media.get("mediaListEntry") if isinstance(media, dict) else None
        return entry if isinstance(entry, dict) else None

    def _save_progress(self, media_id: int, progress: int, status: str | None) -> dict[str, Any]:
        variables: dict[str, Any] = {"mediaId": media_id, "progress": progress}
        if status is not None:
            variables["status"] = status
        try:
            saved = self._post(SAVE_PROGRESS_MUTATION, variables).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            expected_status = status or str((entry or {}).get("status") or "")
            if (
                isinstance(entry, dict)
                and int(entry.get("progress") or 0) == int(progress)
                and (status is None or str(entry.get("status") or "") == expected_status)
            ):
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError(err("anilist.noUpdatedEntry"))
        return saved

    def set_progress(self, media_id: int, progress: int, status: str | None = None) -> dict[str, Any]:
        progress = max(0, int(progress))
        normalized_status = str(status).strip().upper() if status is not None else None
        return self._save_progress(int(media_id), progress, normalized_status)

    def set_list_status(self, media_id: int, status: str) -> dict[str, Any]:
        allowed = {"CURRENT", "PLANNING", "COMPLETED", "DROPPED", "PAUSED", "REPEATING"}
        status = str(status or "").strip().upper()
        if status not in allowed:
            raise AniListError(err("anilist.invalidStatus", status=status))
        try:
            saved = self._post(
                SAVE_STATUS_MUTATION,
                {"mediaId": int(media_id), "status": status},
            ).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            if isinstance(entry, dict) and str(entry.get("status") or "") == status:
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError(err("anilist.noUpdatedEntry"))
        return saved

    def set_score(self, media_id: int, score: float) -> dict[str, Any]:
        value = float(score)
        if value < 1 or value > 10:
            raise AniListError(err("anilist.scoreRange"))
        try:
            saved = self._post(
                SAVE_SCORE_MUTATION,
                {"mediaId": int(media_id), "scoreRaw": int(round(value * 10))},
            ).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            actual = float((entry or {}).get("score") or 0)
            if isinstance(entry, dict) and abs(actual - value) < 0.01:
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError(err("anilist.noSavedScore"))
        return saved

    def delete_list_entry(self, media_id: int) -> bool:
        entry = self._minimal_list_entry(media_id)
        if not isinstance(entry, dict) or not entry.get("id"):
            return False
        try:
            deleted = self._post(
                DELETE_LIST_ENTRY_MUTATION,
                {"id": int(entry["id"])},
            ).get("DeleteMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            return self._minimal_list_entry(media_id) is None
        return bool(isinstance(deleted, dict) and deleted.get("deleted"))

    def update_progress(
        self,
        media_id: int,
        progress: int,
        total_episodes: int | None = None,
        *,
        add_if_missing: bool = False,
        update_when_rewatching: bool = True,
        completed_to_rewatching_on_episode_one: bool = False,
        complete_current_final: bool = True,
        complete_rewatching_final: bool = True,
    ) -> dict[str, Any]:
        if progress < 1:
            raise AniListError(err("anilist.episodeNotPositive"))

        media = self._post(LIST_ENTRY_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListNotFoundError(err("anilist.animeNotFound", mediaId=media_id))

        entry = media.get("mediaListEntry")
        current_progress = int((entry or {}).get("progress") or 0)
        current_status = str((entry or {}).get("status") or "")
        api_total = media.get("episodes")
        # AniList is authoritative when it publishes a total. A stale local
        # hint must not make an airing entry look shorter than it really is.
        total = (int(api_total) if api_total else None) or total_episodes
        if total and progress > int(total):
            # Release filenames can use absolute numbering across split cours
            # (e.g. BLEACH 43 == cour-local 3). Never send that absolute number
            # as AniList progress: it can incorrectly complete the whole entry.
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "progress_exceeds_total",
                "requested_progress": int(progress),
                "total_episodes": int(total),
            }
        is_final = bool(total and progress == int(total))

        if not entry:
            if not add_if_missing:
                return {
                    "updated": False,
                    "progress": 0,
                    "status": None,
                    "reason": "not_on_list",
                }
            status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
            saved = self._save_progress(media_id, progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or progress),
                "status": saved.get("status") or status,
                "reason": "added",
            }

        if current_status == "COMPLETED":
            if progress == 1 and completed_to_rewatching_on_episode_one:
                self._save_progress(media_id, 0, "REPEATING")
                saved = self._save_progress(media_id, 1, None)
                return {
                    "updated": True,
                    "progress": int(saved.get("progress") or 1),
                    "status": saved.get("status") or "REPEATING",
                    "reason": "started_rewatching",
                }
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status,
                "reason": "status_not_modifiable",
            }

        if current_status == "REPEATING":
            if not update_when_rewatching:
                return {
                    "updated": False,
                    "progress": current_progress,
                    "status": current_status,
                    "reason": "rewatching_disabled",
                }
            if current_progress >= progress:
                return {
                    "updated": False,
                    "progress": current_progress,
                    "status": current_status,
                    "reason": "already_at_or_above",
                }
            status = "COMPLETED" if is_final and complete_rewatching_final else None
            saved = self._save_progress(media_id, progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or progress),
                "status": saved.get("status") or status or "REPEATING",
                "reason": "updated_rewatching",
            }

        # A confirmed watch must always move a PLANNING entry out of Planned.
        # Do this before the progress guard because AniList can contain an
        # inconsistent PLANNING entry with non-zero progress (for example after
        # an earlier client wrote progress without changing the list status).
        if current_status == "PLANNING":
            status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
            target_progress = max(current_progress, progress)
            saved = self._save_progress(media_id, target_progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or target_progress),
                "status": saved.get("status") or status,
                "reason": "completed_from_planning" if status == "COMPLETED" else "started_watching",
            }

        if current_status not in {"CURRENT", "PAUSED"}:
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "status_not_modifiable",
            }

        if current_progress >= progress:
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "already_at_or_above",
            }

        status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
        saved = self._save_progress(media_id, progress, status)
        return {
            "updated": True,
            "progress": int(saved.get("progress") or progress),
            "status": saved.get("status") or status,
            "reason": "updated",
        }
