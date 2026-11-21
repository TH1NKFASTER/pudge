from __future__ import annotations

import time
from typing import Any

from .download_intents import DownloadIntentStore


class QueuedReleaseStore:
    """Releases the user chose while Torrents were off, started once they are on.

    One JSON document in the shared ``state`` table, so the window and the
    background agent see the same queue and survive restarts. One entry per
    (title, episode | whole title): a newer choice replaces the older one.
    """

    KEY = "queued_releases"
    # A claim older than this belongs to a process that died while adding.
    CLAIM_STALE_SECONDS = 600.0
    # Failed starts are retried on later passes, then left for the user to cancel.
    MAX_ATTEMPTS = 5

    def __init__(self, db: Any) -> None:
        self._journal = DownloadIntentStore(db)

    @staticmethod
    def entry_id(media_id: int, episode: int | None, batch: bool) -> str:
        return f"{int(media_id)}:{'batch' if batch else f'episode:{int(episode or 0)}'}"

    def _entries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("items")
        return (
            [dict(row) for row in rows if isinstance(row, dict) and row.get("id")]
            if isinstance(rows, list)
            else []
        )

    def _update(self, mutate: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        def apply(payload: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal result
            items = self._entries(payload)
            changed = mutate(items)
            result = items
            return {"items": items} if changed else None

        self._journal._mutate(self.KEY, apply)
        return result

    def list(self) -> list[dict[str, Any]]:
        return self._entries(self._journal._decode(self._journal._get_state(self.KEY, "")))

    def add(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
        release: dict[str, Any],
        *,
        anime_title: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Queue ``release``; returns (entry, same_release_was_already_queued)."""
        key = self.entry_id(media_id, episode, batch)
        now = time.time()
        found: dict[str, Any] = {}
        duplicate = False

        def mutate(items: list[dict[str, Any]]) -> bool:
            nonlocal found, duplicate
            existing = next((row for row in items if row["id"] == key), None)
            old_hash = str(((existing or {}).get("release") or {}).get("info_hash") or "").casefold()
            new_hash = str(release.get("info_hash") or "").casefold()
            old_title = str(((existing or {}).get("release") or {}).get("title") or "")
            duplicate = existing is not None and (
                (bool(new_hash) and old_hash == new_hash)
                or (not new_hash and old_title == release.get("title"))
            )
            if duplicate:
                # The same choice again: nothing to write (a start under way keeps going).
                found = dict(existing)
                return False
            entry = {
                "id": key,
                "media_id": int(media_id),
                "episode": int(episode) if episode is not None and not batch else None,
                "batch": bool(batch),
                "release": dict(release),
                "anime_title": str(anime_title or ""),
                "queued_at": float((existing or {}).get("queued_at") or now),
                "state": "queued",
                "attempts": 0,
                "error": "",
            }
            if existing is not None and existing.get("state") == "starting":
                # Another pass is adding the old choice: its claim stays its own.
                entry["state"] = "starting"
                entry["claimed_at"] = existing.get("claimed_at")
            if existing is not None:
                items[items.index(existing)] = entry
            else:
                items.append(entry)
            found = entry
            return True

        self._update(mutate)
        return found, duplicate

    def remove(self, entry_id: str) -> bool:
        removed = False

        def mutate(items: list[dict[str, Any]]) -> bool:
            nonlocal removed
            before = len(items)
            items[:] = [row for row in items if row["id"] != str(entry_id)]
            removed = len(items) != before
            return removed

        self._update(mutate)
        return removed

    def claim(
        self,
        *,
        now: float | None = None,
        limit: int | None = None,
        skip: set[str] | frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Mark up to ``limit`` startable entries as started by this process and return them.

        Callers claim one at a time: an add can take a minute and a claim older than
        CLAIM_STALE_SECONDS is taken over by the other process.
        """
        current = time.time() if now is None else float(now)
        claimed: list[dict[str, Any]] = []

        def mutate(items: list[dict[str, Any]]) -> bool:
            for row in items:
                if limit is not None and len(claimed) >= limit:
                    break
                if row["id"] in skip or self.exhausted(row):
                    continue
                if row.get("state") == "starting" and current - float(row.get("claimed_at") or 0) < (
                    self.CLAIM_STALE_SECONDS
                ):
                    continue
                row["state"] = "starting"
                row["claimed_at"] = current
                claimed.append(dict(row))
            return bool(claimed)

        self._update(mutate)
        return claimed

    @classmethod
    def exhausted(cls, row: dict[str, Any]) -> bool:
        """Failed MAX_ATTEMPTS times: no more automatic tries until Torrents are turned on again."""
        try:
            return int(row.get("attempts") or 0) >= cls.MAX_ATTEMPTS
        except (TypeError, ValueError):
            return False

    def reset_attempts(self) -> None:
        """Torrents were turned on: every entry gets its tries back."""

        def mutate(items: list[dict[str, Any]]) -> bool:
            changed = False
            for row in items:
                if int(row.get("attempts") or 0):
                    row["attempts"] = 0
                    changed = True
            return changed

        self._update(mutate)

    def unclaim(self, entry_id: str, *, error: str = "", count_attempt: bool = True) -> None:
        def mutate(items: list[dict[str, Any]]) -> bool:
            for row in items:
                if row["id"] == str(entry_id):
                    row["state"] = "queued"
                    row.pop("claimed_at", None)
                    if count_attempt:
                        row["attempts"] = int(row.get("attempts") or 0) + 1
                    row["error"] = str(error or "")[:500]
                    return True
            return False

        self._update(mutate)

    def finish(self, entry: dict[str, Any]) -> None:
        """Forget a started entry, unless the user queued a new choice for it meanwhile."""
        claimed_at = entry.get("claimed_at")

        def mutate(items: list[dict[str, Any]]) -> bool:
            before = len(items)
            items[:] = [
                row
                for row in items
                if not (row["id"] == entry.get("id") and row.get("claimed_at") == claimed_at)
            ]
            return len(items) != before

        self._update(mutate)
