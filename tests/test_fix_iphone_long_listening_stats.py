"""Listening done on the phone while it could not reach the Mac arrives as one merged run
(minutes or hours). The statistics ledger takes intervals of at most 30 s, so the whole run
was refused ("invalid device interval") and the statistics did not move."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from pudge.consumption import MAX_CHUNK_SECONDS
from pudge.iphone_service import _LEDGER_CHUNK_SECONDS, listening_chunks
from test_iphone_audio_repository import archived, audio_manifest, checkpoint, paired, service  # noqa: F401


def test_chunk_size_matches_the_ledger():
    assert _LEDGER_CHUNK_SECONDS == MAX_CHUNK_SECONDS


def test_long_interval_is_split_into_ledger_sized_pieces():
    pieces = listening_chunks(1000.0, 1000.0 + 3600.0, 0.0, 5400.0)
    assert len(pieces) == 120
    assert all(b - a <= MAX_CHUNK_SECONDS + 1e-6 for a, b, _f, _l in pieces)
    assert pieces[0][0] == 1000.0 and pieces[-1][1] == 4600.0
    assert pieces[0][2] == 0.0 and abs(pieces[-1][3] - 5400.0) < 1e-6
    for left, right in zip(pieces, pieces[1:]):
        assert left[1] == right[0] and abs(left[3] - right[2]) < 1e-6


def test_short_interval_stays_one_piece():
    assert listening_chunks(10.0, 15.0, 1.0, 6.0) == [(10.0, 15.0, 1.0, 6.0)]


class _Ledger:
    def __init__(self) -> None:
        self.events: dict[str, tuple[float, float]] = {}

    def audiobook_media(self, _book_id):
        return SimpleNamespace(media_uuid="m", source_revision="r")

    def audiobook_activity_group(self, _book_id):
        return "g"

    def record_device_interval(self, **kwargs):
        assert kwargs["end_utc"] - kwargs["start_utc"] <= MAX_CHUNK_SECONDS + 0.001
        self.events.setdefault(kwargs["event_id"], (kwargs["start_utc"], kwargs["end_utc"]))
        return "applied"


def test_offline_hour_is_counted_and_backfill_is_idempotent(service):
    device, manifest = archived(service)
    ledger = _Ledger()
    service.api = SimpleNamespace(consumption=ledger, logger=None)
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_assets SET book_id=7 WHERE id=?", (manifest["assetId"],))
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
    service.record_checkpoint(
        device,
        periodic(
            900,
            58_000,
            start + 48_000,
            baseProgressRevision=first["revision"],
            continuousSince={"observedAt": start + 5_000, "positionMs": 15_000},
        ),
    )
    counted = sum(b - a for a, b in ledger.events.values())
    assert abs(counted - 48.0) < 0.01  # 5 s step + 43 s run, nothing refused
    before = dict(ledger.events)
    ledger.events.clear()
    assert service.backfill_listening_runs() == 1
    run_keys = {key for key in before if ":run" in key}
    assert set(ledger.events) == run_keys
