from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .database import Database


ACTIVE_JOB_STATES = {"queued", "running", "cancel_requested"}
class JobCenter:
    """Small persistent journal shared by long-running user operations."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE app_jobs SET state='failed',error='Pudge closed before the job finished',"
                "message='Interrupted',finished_at=?,updated_at=? "
                "WHERE state IN ('queued','running','cancel_requested')",
                (now, now),
            )

    def start(
        self,
        kind: str,
        title: str,
        *,
        payload: dict[str, Any] | None = None,
        total: float = 0,
        attempt_of: str = "",
    ) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_jobs(
                    id,kind,title,state,current,total,message,error,payload_json,
                    result_json,attempt_of,created_at,updated_at,finished_at
                ) VALUES(?,?,?,'queued',0,?,'Queued','',?,'{}',?,?,?,0)
                """,
                (
                    job_id,
                    str(kind),
                    str(title),
                    max(0.0, float(total)),
                    json.dumps(payload or {}, ensure_ascii=False),
                    str(attempt_of or ""),
                    now,
                    now,
                ),
            )
            self._prune(conn)
        return job_id

    def update(
        self,
        job_id: str,
        *,
        state: str = "running",
        current: float | None = None,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        requested_state = str(state)
        values: list[Any] = [requested_state, requested_state, time.time()]
        sets = [
            "state=CASE WHEN state='cancel_requested' AND ? IN ('queued','running') "
            "THEN state ELSE ? END",
            "updated_at=?",
        ]
        if current is not None:
            sets.append("current=?")
            values.append(max(0.0, float(current)))
        if total is not None:
            sets.append("total=?")
            values.append(max(0.0, float(total)))
        if message is not None:
            sets.append("message=?")
            values.append(str(message)[:1000])
        values.append(str(job_id))
        with self.db.connect() as conn:
            conn.execute(f"UPDATE app_jobs SET {','.join(sets)} WHERE id=?", values)

    def finish(
        self,
        job_id: str,
        *,
        message: str = "Completed",
        result: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE app_jobs SET state='succeeded',current=CASE WHEN total>0 THEN total ELSE current END,"
                "message=?,error='',result_json=?,updated_at=?,finished_at=? WHERE id=?",
                (
                    str(message)[:1000],
                    json.dumps(result or {}, ensure_ascii=False),
                    now,
                    now,
                    str(job_id),
                ),
            )

    def fail(self, job_id: str, error: object, *, message: str = "Failed") -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE app_jobs SET state='failed',message=?,error=?,updated_at=?,finished_at=? WHERE id=?",
                (str(message)[:1000], str(error)[-2000:], now, now, str(job_id)),
            )

    def request_cancel(self, job_id: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute(
                "UPDATE app_jobs SET state='cancel_requested',message='Stopping…',updated_at=? "
                "WHERE id=? AND state IN ('queued','running')",
                (time.time(), str(job_id)),
            )
        return bool(cursor.rowcount)

    def cancelled(self, job_id: str, *, message: str = "Cancelled") -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE app_jobs SET state='cancelled',message=?,updated_at=?,finished_at=? WHERE id=?",
                (str(message)[:1000], now, now, str(job_id)),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM app_jobs WHERE id=?", (str(job_id),)).fetchone()
        return self._payload(row) if row is not None else None

    def jobs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM app_jobs ORDER BY "
                "CASE WHEN state IN ('queued','running','cancel_requested') THEN 0 ELSE 1 END,"
                "updated_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [self._payload(row) for row in rows]

    @staticmethod
    def _decode(raw: object) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _payload(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = self._decode(item.pop("payload_json", "{}"))
        item["result"] = self._decode(item.pop("result_json", "{}"))
        current = float(item.get("current") or 0.0)
        total = float(item.get("total") or 0.0)
        item["progress"] = max(0.0, min(1.0, current / total)) if total > 0 else 0.0
        item["can_cancel"] = str(item.get("state") or "") in ACTIVE_JOB_STATES
        item["can_retry"] = str(item.get("state") or "") in {"failed", "cancelled"}
        return item

    @staticmethod
    def _prune(conn: Any) -> None:
        conn.execute(
            "DELETE FROM app_jobs WHERE id IN (SELECT id FROM app_jobs "
            "WHERE state IN ('succeeded','failed','cancelled') "
            "ORDER BY updated_at DESC LIMIT -1 OFFSET 250)"
        )
