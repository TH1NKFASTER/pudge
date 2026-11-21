"""The end of an anime watched on a paired phone: the score wheel and the next entry.

The Mac asks for a score when its mpv closes after the final episode (index.html
``showScoreModal``: the AniList score from a wheel, then the direct sequel the Mac suggests,
``final_sequel_option``). The phone does the same after the final episode (a movie or a
one-episode title: its only one) through these routes, all reusing the Mac's own calls:

- ``POST media/anime/<id>/score`` ``{"score": 1-10, "episode": n}`` or ``{"skip": true,
  "episode": n}``: ``set_anilist_score`` (AniList away: queued there by
  ``anilist_tracking.queue_pending_score`` and sent with the next refresh) / ``skip_rating``.
- ``GET media/anime/<id>/next-entry``: the Mac's suggestion (``final_sequel_option``).
- ``POST media/anime/<id>/next-entry`` ``{"action": "planning"|"watching"|"download"}``.

The next entry is computed ahead (``warm``): when a phone prepares (downloads, streams) or
reports a place in the final episode, the Mac's ``warm_final_sequel`` fills its cache in the
background, so the sheet's GET answers at once (final_sequel_cache).

What a counted watch answers (``finale``) says whether to ask: ``kind`` "final" (rating episode
= the last one), ``prompted`` when it was answered already (on the Mac or a phone). Only the end
of the anime asks: being caught up with an airing title does not (as on the Mac).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

from .iphone_media import MediaError, MediaTemporaryError

_ACTIONS = ("planning", "watching", "download")
# One warm-up check per episode this often (a phone sends its place every 15 s).
WARM_CHECK_SECONDS = 600.0


def finale(db: Any, media_id: int, episode: int) -> dict[str, Any] | None:
    """Whether ``episode`` (its last number) of ``media_id`` ends what can be watched now, as
    the Mac's ``play_status`` decides (the final episode; a movie or a one-episode title). None
    otherwise: the latest aired episode of an airing title asks nothing."""
    if not media_id or episode is None:
        return None
    anime = db.get_anime(int(media_id))
    if anime is None:
        return None
    total = int(anime.episodes) if anime.episodes else None
    single = total == 1 or str(anime.format or "").strip().upper() == "MOVIE"
    number = int(episode or 0)
    kind = ""
    rating_episode = None
    if total is not None and number >= total:
        kind, rating_episode = "final", total
    elif single and number <= 1:
        kind, rating_episode = "final", 1
    if not kind or rating_episode is None:
        return None
    return {
        "mediaId": int(anime.media_id),
        "title": str(anime.title or f"AniList #{anime.media_id}"),
        "kind": kind,
        "episode": int(rating_episode),
        "userScore": float(anime.user_score) if anime.user_score else None,
        "prompted": bool(db.rating_prompted(int(anime.media_id), int(rating_episode))),
    }


class IPhoneFinale:
    def __init__(self, media: Any) -> None:
        self.media = media
        self._warm_checked: dict[int, float] = {}

    @property
    def api(self) -> Any:
        return self.media.api

    def handle(self, method: str, media_id: int, part: str, body: Callable[[], dict[str, Any]]) -> Any:
        if media_id <= 0:
            raise MediaError("This anime is not on the Mac")
        if part == "score" and method == "POST":
            return self.score(media_id, body())
        if part == "next-entry" and method == "GET":
            return self.next_entry(media_id)
        if part == "next-entry" and method == "POST":
            return self.next_action(media_id, body())
        raise KeyError("Unknown media operation")

    def warm_due(self, episode_id: int) -> bool:
        """Whether ``episode_id`` was not checked for a warm-up lately (then marked checked)."""
        now = time.monotonic()
        last = self._warm_checked.get(int(episode_id))
        if last is not None and now - last < WARM_CHECK_SECONDS:
            return False
        if len(self._warm_checked) > 500:
            self._warm_checked.clear()
        self._warm_checked[int(episode_id)] = now
        return True

    def warm(self, row: Any) -> bool:
        """A phone prepares or plays episode ``row``: its title's next entry is computed in the
        background when it is the final episode (``warm_final_sequel`` of the Mac)."""
        try:
            warm = getattr(self.api, "warm_final_sequel", None)
            media_id = int(row.get("media_id") or 0)
            if warm is None or media_id <= 0:
                return False
            number = int(row.get("media_episode_end") or 0) or int(
                row.get("media_episode") or row.get("episode") or 0
            )
            queued = bool(warm(media_id, number))
        except Exception as exc:  # noqa: BLE001 - only a warm-up
            self.media._log("FAIL step=iphone.finale_warm error=%r", str(exc)[:200])
            return False
        if queued:
            self.media._log("EVENT iphone.finale_warm media_id=%s episode=%s", media_id, number)
        return queued

    def score(self, media_id: int, value: dict[str, Any]) -> dict[str, Any]:
        try:
            episode = int(value.get("episode")) if value.get("episode") is not None else None
        except (TypeError, ValueError):
            episode = None
        if value.get("skip"):
            if episode is not None:
                self.api.skip_rating(media_id, episode)
            self.media._log("EVENT iphone.finale_skip media_id=%s episode=%s", media_id, episode)
            return {"ok": True, "skipped": True}
        try:
            score = float(value.get("score"))
        except (TypeError, ValueError):
            raise MediaError("The score must be a number from 1 to 10") from None
        if not 1 <= score <= 10:
            raise MediaError("The score must be a number from 1 to 10")
        try:
            result = self.api.set_anilist_score(media_id, score, episode)
        except MediaError:
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            # A locked database, a full disk, Pudge shutting down: the phone sends it again
            # (503) instead of dropping the score for good (422).
            raise MediaTemporaryError(str(exc) or "The Mac could not save the score now") from None
        except Exception as exc:  # noqa: BLE001 - AniList refused the value (queued otherwise)
            raise MediaError(str(exc) or "AniList did not take the score") from None
        self.media._log(
            "EVENT iphone.finale_score media_id=%s episode=%s score=%s pending=%s",
            media_id,
            episode,
            score,
            bool(result.get("anilist_pending")),
        )
        return {
            "ok": True,
            "score": float(result.get("score") or score),
            "anilistPending": bool(result.get("anilist_pending")),
        }

    def next_entry(self, media_id: int) -> dict[str, Any]:
        try:
            option = self.api.final_sequel_option(media_id)
        except Exception as exc:  # noqa: BLE001 - AniList away: the phone asks again later
            raise MediaTemporaryError("AniList is not answering; try again later") from exc
        sequel = option.get("sequel") if isinstance(option, dict) else None
        return {"sequel": _sequel_view(sequel) if isinstance(sequel, dict) else None}

    def next_action(self, media_id: int, value: dict[str, Any]) -> dict[str, Any]:
        action = str(value.get("action") or "")
        if action not in _ACTIONS:
            raise MediaError("Unknown action")
        current = self.next_entry(media_id).get("sequel")
        if not current:
            return {"ok": False, "reason": "no_sequel", "sequel": None}
        sequel_id = int(current["mediaId"])
        result: dict[str, Any] = {"ok": True, "reason": ""}
        if action == "planning":
            answer = self.api.add_to_planning(sequel_id)
        elif action == "watching":
            answer = self.api.set_anime_watching(sequel_id)
        else:
            answer = self.api.download_final_sequel(media_id)
            result.update(
                ok=bool(answer.get("ok")),
                reason=str(answer.get("reason") or ""),
                background=bool(answer.get("background")),
                release=str(answer.get("release") or ""),
            )
        result["anilistPending"] = bool((answer or {}).get("anilist_pending"))
        self.media._log(
            "EVENT iphone.finale_next media_id=%s sequel=%s action=%s ok=%s reason=%s",
            media_id,
            sequel_id,
            action,
            result["ok"],
            result["reason"],
        )
        try:
            result["sequel"] = self.next_entry(media_id).get("sequel")
        except MediaError:
            result["sequel"] = current
        return result


def _sequel_view(sequel: dict[str, Any]) -> dict[str, Any]:
    """The Mac's suggestion for the phone (camelCase; the cover is the AniList URL)."""
    episodes = sequel.get("episodes")
    return {
        "mediaId": int(sequel["media_id"]),
        "title": str(sequel.get("title") or ""),
        "cover": str(sequel.get("cover") or ""),
        "siteUrl": str(sequel.get("site_url") or ""),
        "startDate": str(sequel.get("start_date") or ""),
        "mediaStatus": str(sequel.get("media_status") or ""),
        "listStatus": str(sequel.get("list_status") or ""),
        "format": str(sequel.get("format") or ""),
        "episodes": int(episodes) if isinstance(episodes, (int, float)) and episodes else None,
        "released": bool(sequel.get("released")),
        "canPlanning": bool(sequel.get("can_planning")),
        "canWatching": bool(sequel.get("can_watching")),
        "canDownload": bool(sequel.get("can_download")),
    }
