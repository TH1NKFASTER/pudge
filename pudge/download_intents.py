from __future__ import annotations

import json
import time
from typing import Any


class DownloadIntentStore:
    """Tiny persistent journal for one logical "get me this episode" request."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._memory: dict[str, str] = {}

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
        payload = {
            "media_id": int(media_id),
            "episode": int(episode) if episode is not None else None,
            "batch": bool(batch),
            "state": "selecting",
            "backend": str(backend or ""),
            "candidates": [self._candidate(item) for item in candidates[:5]],
            "selected_hash": "",
            "selected_title": "",
            "created_at": now,
            "updated_at": now,
        }
        self._set_state(
            self.key(media_id, episode, batch),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
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
    ) -> dict[str, Any]:
        payload = self.get(media_id, episode, batch) or {
            "media_id": int(media_id),
            "episode": int(episode) if episode is not None else None,
            "batch": bool(batch),
            "created_at": time.time(),
            "candidates": [],
        }
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
        self._set_state(
            self.key(media_id, episode, batch),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        return payload

    def get(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
    ) -> dict[str, Any] | None:
        try:
            raw = self._get_state(self.key(media_id, episode, batch), "")
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload else None

    def clear(self, media_id: int, episode: int | None, batch: bool) -> None:
        self._delete_state(self.key(media_id, episode, batch))
