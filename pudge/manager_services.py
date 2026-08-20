from __future__ import annotations

import json
import math
import sqlite3
import time
from statistics import median
from typing import Any


class ReleaseTelemetryService:
    """Persist release outcomes and derive conservative race timeouts."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def record(
        self,
        release: Any,
        *,
        media_id: int | None,
        media_episode: int | None,
        provider: str,
        outcome: str,
        snapshot: dict[str, Any] | None = None,
        elapsed_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connector = getattr(self.database, "connect", None)
        if not callable(connector):
            return
        snapshot = dict(snapshot or {})
        info_hash = str(getattr(release, "info_hash", "") or "").casefold()
        if not info_hash:
            return
        try:
            with connector() as conn:
                ensure_parent = getattr(self.database, "_ensure_anime_parent", None)
                if callable(ensure_parent):
                    ensure_parent(conn, media_id, str(getattr(release, "title", "") or ""))
                conn.execute(
                    """
                    INSERT INTO torrent_observations(
                        info_hash,media_id,media_episode,provider,title,score,
                        listed_seeders,listed_leechers,live_seeders,live_leechers,
                        metadata_seconds,download_speed_bps,outcome,details_json,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        info_hash,
                        media_id,
                        media_episode,
                        str(provider),
                        str(getattr(release, "title", "") or ""),
                        float(getattr(release, "score", 0.0) or 0.0),
                        int(getattr(release, "seeders", 0) or 0),
                        int(getattr(release, "leechers", 0) or 0),
                        self._optional_int(snapshot, "num_seeds", "seeders"),
                        self._optional_int(snapshot, "num_leechs", "leechers", "connections"),
                        float(elapsed_seconds) if elapsed_seconds is not None else None,
                        self._optional_float(snapshot, "dlspeed", "download_speed"),
                        str(outcome),
                        json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str),
                        time.time(),
                    ),
                )
        except (AttributeError, sqlite3.Error, TypeError, ValueError):
            # Telemetry must never change download correctness.  Older/fake DB
            # adapters used by integrations may not have the v7 table yet.
            return

    @staticmethod
    def _optional_int(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            if payload.get(key) is not None:
                try:
                    return int(payload[key])
                except (TypeError, ValueError):
                    continue
        return None

    @classmethod
    def _optional_float(cls, payload: dict[str, Any], *keys: str) -> float | None:
        value = cls._optional_int(payload, *keys)
        return float(value) if value is not None else None

    def deadlines(self, *, provider: str, default_fast: float, default_total: float) -> tuple[float, float]:
        connector = getattr(self.database, "connect", None)
        if not callable(connector):
            return default_fast, default_total
        try:
            with connector() as conn:
                rows = conn.execute(
                    "SELECT metadata_seconds FROM torrent_observations "
                    "WHERE provider=? AND outcome='winner' AND metadata_seconds>0 "
                    "ORDER BY observed_at DESC LIMIT 100",
                    (str(provider),),
                ).fetchall()
        except (AttributeError, sqlite3.Error, TypeError, ValueError):
            return default_fast, default_total
        samples = sorted(float(row[0]) for row in rows if row[0] is not None)
        if len(samples) < 5:
            return default_fast, default_total
        p90 = samples[max(0, math.ceil(len(samples) * 0.9) - 1)]
        fast = max(5.0, min(25.0, median(samples) * 1.5))
        total = max(45.0, min(180.0, p90 * 2.5))
        return fast, max(fast, total)
