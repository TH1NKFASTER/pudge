from __future__ import annotations

import json
import threading
import time
from typing import Any


class DownloadIntentStore:
    """Tiny persistent journal for one logical "get me this episode" request."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._memory: dict[str, str] = {}
        self._lock = threading.RLock()

    def _set_state(self, key: str, value: str) -> None:
        setter = getattr(self.db, "set_state", None)
        if callable(setter):
            setter(key, value)
        else:
            self._memory[key] = value

    def _get_state(self, key: str, default: str = "") -> str:
        getter = getattr(self.db, "get_state", None)
        if callable(getter):
            return str(getter(key, default) or default)
        return self._memory.get(key, default)

    def _delete_state(self, key: str) -> None:
        delete = getattr(self.db, "delete_state", None)
        if callable(delete):
            delete(key)
            return
        setter = getattr(self.db, "set_state", None)
        if callable(setter):
            setter(key, "")
            return
        self._memory.pop(key, None)

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _mutate(
        self,
        key: str,
        mutator: Any,
    ) -> dict[str, Any] | None:
        connector = getattr(self.db, "connect", None)
        if callable(connector):
            with self._lock, connector() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                raw = "" if row is None else str(row["value"] if hasattr(row, "keys") else row[0])
                current = self._decode(raw)
                updated = mutator(dict(current))
                if updated is None:
                    return None
                encoded = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value,updated_at=excluded.updated_at",
                    (key, encoded, time.time()),
                )
                return updated

        with self._lock:
            current = self._decode(self._get_state(key, ""))
            updated = mutator(dict(current))
            if updated is None:
                return None
            self._set_state(
                key,
                json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
            )
            return updated

    @staticmethod
    def key(media_id: int, episode: int | None, batch: bool) -> str:
        suffix = "batch" if batch else f"episode:{int(episode or 0)}"
        return f"download_intent:{int(media_id)}:{suffix}"

    @staticmethod
    def _candidate(item: Any) -> dict[str, Any]:
        return {
            "title": str(getattr(item, "title", "") or ""),
            "info_hash": str(getattr(item, "info_hash", "") or ""),
            "score": float(getattr(item, "score", 0.0) or 0.0),
            "seeders": int(getattr(item, "seeders", 0) or 0),
            "leechers": int(getattr(item, "leechers", 0) or 0),
        }

    def begin(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
        candidates: list[Any],
        *,
        backend: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        key = self.key(media_id, episode, batch)

        def replace(current: dict[str, Any]) -> dict[str, Any]:
            try:
                previous_revision = int(current.get("revision") or 0)
            except (TypeError, ValueError):
                previous_revision = 0
            return {
                "media_id": int(media_id),
                "episode": int(episode) if episode is not None else None,
                "batch": bool(batch),
                "revision": previous_revision + 1,
                "state": "selecting",
                "backend": str(backend or ""),
                "candidates": [self._candidate(item) for item in candidates[:5]],
                "selected_hash": "",
                "selected_title": "",
                "created_at": now,
                "updated_at": now,
            }

        payload = self._mutate(key, replace)
        assert payload is not None
        return payload

    def update(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
        *,
        state: str,
        selected: Any | None = None,
        backend: str | None = None,
        detail: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        key = self.key(media_id, episode, batch)

        def mutate(payload: dict[str, Any]) -> dict[str, Any] | None:
            if not payload:
                if expected_revision is not None:
                    return None
                payload = {
                    "media_id": int(media_id),
                    "episode": int(episode) if episode is not None else None,
                    "batch": bool(batch),
                    "revision": 1,
                    "created_at": time.time(),
                    "candidates": [],
                }
            try:
                revision = int(payload.get("revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            if expected_revision is not None and revision != int(expected_revision):
                return None
            if revision <= 0:
                payload["revision"] = 1
            payload["state"] = str(state)
            payload["updated_at"] = time.time()
            if backend is not None:
                payload["backend"] = str(backend)
            if detail:
                payload["detail"] = str(detail)[:500]
            if selected is not None:
                payload["selected_hash"] = str(getattr(selected, "info_hash", "") or "")
                payload["selected_title"] = str(getattr(selected, "title", "") or "")
                payload["selected_score"] = float(getattr(selected, "score", 0.0) or 0.0)
            return payload

        return self._mutate(key, mutate)

    def get(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
    ) -> dict[str, Any] | None:
        with self._lock:
            payload = self._decode(self._get_state(self.key(media_id, episode, batch), ""))
        return payload or None

    def clear(self, media_id: int, episode: int | None, batch: bool) -> None:
        with self._lock:
            self._delete_state(self.key(media_id, episode, batch))

    def complete_if_present(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
        *,
        detail: str = "Download completed",
        expected_revision: int | None = None,
        expected_hash: str = "",
    ) -> bool:
        key = self.key(media_id, episode, batch)
        completed = False
        normalized_expected_hash = str(expected_hash or "").casefold()

        def mutate(payload: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal completed
            if not payload:
                return None
            if str(payload.get("state") or "").casefold() == "complete":
                return None
            try:
                revision = int(payload.get("revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            if expected_revision is not None and revision != int(expected_revision):
                return None
            selected_hash = str(payload.get("selected_hash") or "").casefold()
            if normalized_expected_hash:
                if selected_hash and selected_hash != normalized_expected_hash:
                    return None
                # A new begin() intentionally clears selected_hash.  Never let a
                # completion from the previous generation close that fresh intent.
                if not selected_hash and revision > 1:
                    return None
            payload["state"] = "complete"
            payload["updated_at"] = time.time()
            payload["detail"] = str(detail)[:500]
            completed = True
            return payload

        self._mutate(key, mutate)
        return completed

    def waiting_count(self) -> int:
        def counts(payload: dict[str, Any]) -> bool:
            state = str(payload.get("state") or "").casefold()
            if state == "waiting":
                return True
            if state != "selecting":
                return False
            candidates = payload.get("candidates")
            return isinstance(candidates, list) and bool(candidates)

        connector = getattr(self.db, "connect", None)
        if not callable(connector):
            count = 0
            for value in self._memory.values():
                if not isinstance(value, str):
                    continue
                try:
                    payload = json.loads(value or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and counts(payload):
                    count += 1
            return count
        try:
            with connector() as conn:
                rows = conn.execute(
                    "SELECT value FROM state WHERE key LIKE 'download_intent:%'"
                ).fetchall()
        except Exception:
            return 0
        count = 0
        for row in rows:
            try:
                raw = row["value"] if hasattr(row, "keys") else row[0]
                payload = json.loads(str(raw or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and counts(payload):
                count += 1
        return count
