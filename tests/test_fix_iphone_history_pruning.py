"""IOS-9 and WEBSEC-9.

IOS-9: every phone checkpoint (one per 5 s of playback), every Mac position save and every
bookmark edit stayed in the library database forever (2 h of listening a day: ~370 MB a year),
and handoffs nobody answered were scanned again on every /commands poll. Now receipts older
than 30 days, finished handoffs and old listening marks are pruned (hourly), and handoffs left
open are closed. A phone that cannot send for a while merges its periodic checkpoints into one
("continuousSince"): the Mac still counts that listening.

WEBSEC-9: a lock was created for any transfer ID in an archive request before the ID was
checked, and none was ever removed.
"""

from __future__ import annotations

import uuid

import pytest

from pudge.iphone_service import continuous_since, now_ms
from test_iphone_audio_repository import archived, audio_manifest, checkpoint, paired, service  # noqa: F401


def _age(service, table: str, days: float) -> None:
    with service.db.connect() as conn:
        conn.execute(f"UPDATE {table} SET created=?", (now_ms() - int(days * 86_400_000),))


def test_old_receipts_marks_and_finished_handoffs_are_pruned(service):
    device, manifest = archived(service)
    old = checkpoint(manifest, device)
    service.record_checkpoint(device, old)
    _age(service, "iphone_checkpoints", 31)
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_listen_marks SET observed=?", (now_ms() - 31 * 86_400_000,))
        conn.execute(
            "INSERT INTO iphone_handoffs(id,body,result,created) VALUES(?,?,?,?)",
            (str(uuid.uuid4()), "{}", '{"status":"source_paused"}', now_ms() - 8 * 86_400_000),
        )
    recent = checkpoint(manifest, device, base=1, sequence=2, position=50000)
    service.record_checkpoint(device, recent)
    removed = service.prune_history(force=True)
    assert removed["checkpoints"] == 1 and removed["handoffs"] == 1
    with service.db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM iphone_checkpoints")] == [recent["checkpointId"]]
        assert conn.execute("SELECT COUNT(*) FROM iphone_handoffs").fetchone()[0] == 0
    # The place itself stays (it lives in the progress row, not in the receipts).
    assert service.progress(manifest["assetId"])["selected"]["checkpointId"] == recent["checkpointId"]
    # Pruning runs at most hourly unless forced.
    assert service.prune_history() == {}


def test_rows_from_before_the_column_get_a_time_and_go_30_days_later(service):
    device, manifest = archived(service)
    service.record_checkpoint(device, checkpoint(manifest, device))
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_checkpoints SET created=NULL")
    assert service.prune_history(force=True)["checkpoints"] == 0
    with service.db.connect() as conn:
        assert conn.execute("SELECT created FROM iphone_checkpoints").fetchone()[0] is not None


def test_a_handoff_nobody_answered_is_closed(service):
    operation = str(uuid.uuid4())
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_handoffs(id,body,created) VALUES(?,?,?)",
            (operation, '{"assetId":"x","targetDeviceId":"mac"}', now_ms() - 10 * 60_000),
        )
    assert service.prune_history(force=True)["handoffs_closed"] == 1
    with service.db.connect() as conn:
        result = conn.execute("SELECT result FROM iphone_handoffs WHERE id=?", (operation,)).fetchone()[0]
    assert '"source_unreachable"' in result
    assert service.commands("any-device") == {"items": []}


def test_merged_periodic_checkpoints_still_count_as_listening(service, monkeypatch):
    device, manifest = archived(service)
    counted: list[dict] = []
    monkeypatch.setattr(service, "_record_listening", lambda _device, item: counted.append(item))
    session = str(uuid.uuid4())
    start = 1_700_000_000_000

    def periodic(sequence, position, observed, **extra):
        return dict(
            checkpoint(manifest, device, position=position, sequence=sequence, reason="periodic"),
            playbackSessionId=session,
            observedAt=observed,
            **extra,
        )

    first = service.record_checkpoint(device, periodic(1, 10_000, start))
    # Offline: the phone merged the saves at 15 s ... 45 s into the one at 50 s.
    service.record_checkpoint(
        device,
        periodic(
            9,
            50_000,
            start + 40_000,
            baseProgressRevision=first["revision"],
            continuousSince={"observedAt": start + 5_000, "positionMs": 15_000},
        ),
    )
    intervals = [item["interval"] for item in counted]
    # The step into the run (10 s -> 15 s) and the run itself (15 s -> 50 s).
    assert intervals == [
        (start / 1000, (start + 5_000) / 1000, 10.0, 15.0),
        ((start + 5_000) / 1000, (start + 40_000) / 1000, 15.0, 50.0),
    ]
    assert len({item["checkpoint"] for item in counted}) == 2


def test_an_implausible_run_is_not_counted():
    base = {"reason": "periodic", "continuousSince": {"observedAt": 1000, "positionMs": 100}}
    assert continuous_since(base, 5000, 2000) == {"observedAt": 1000, "positionMs": 100}
    assert continuous_since({**base, "reason": "pause"}, 5000, 2000) is None
    assert continuous_since({**base, "continuousSince": {"observedAt": "x"}}, 5000, 2000) is None
    assert continuous_since(base, 50, 2000) is None  # moved backwards
    assert continuous_since(base, 5000, 500) is None  # ended before it began


def test_unknown_transfer_ids_never_get_a_lock(service):
    request, _ = paired(service)
    device = request["deviceId"]
    for _ in range(20):
        with pytest.raises(PermissionError):
            service.archive_chunk(device, str(uuid.uuid4()), str(uuid.uuid4()), 0, b"x")
        with pytest.raises(PermissionError):
            service.commit_archive(device, str(uuid.uuid4()))
    with pytest.raises(ValueError):
        service.archive_chunk(device, "../../x", str(uuid.uuid4()), 0, b"x")
    assert service._transfer_locks == {}


def test_a_finished_transfer_leaves_no_lock_behind(service):
    request, _ = paired(service)
    device = request["deviceId"]
    manifest = audio_manifest(b"audio")
    plan = service.begin_archive(device, manifest)
    service.archive_chunk(device, plan["transferId"], manifest["parts"][0]["resourceId"], 0, b"audio")
    assert service._transfer_locks == {}
    service.commit_archive(device, plan["transferId"])
    assert service._transfer_locks == {} and service._handoff_locks == {}
