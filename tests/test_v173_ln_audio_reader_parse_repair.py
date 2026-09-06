from __future__ import annotations

import threading
from pathlib import Path

from pudge.audiobooks import AudiobookService


def _service() -> AudiobookService:
    service = object.__new__(AudiobookService)
    service._lock = threading.Lock()
    service._reader_parse_alignment_refresh_seen = set()
    service._alignment_jobs = {}
    service._alignment_payload_cache = {}
    return service


def test_canonical_reader_parse_retries_degraded_zero_hint_alignment(monkeypatch) -> None:
    service = _service()
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "leading_prefix_debug": {
                    "degraded": True,
                    "reason": "no_prefix_seed",
                    "reading_hint_count": 0,
                },
            }
        ],
        "processing": {"input_fingerprint": "fingerprint-v13"},
    }
    monkeypatch.setattr(
        service,
        "link_for_light_novel",
        lambda *_args, **_kwargs: {"book": {"id": 283}},
    )
    monkeypatch.setattr(service, "_load_alignment", lambda *_args: alignment)
    monkeypatch.setattr(
        service,
        "_cached_chapter_start_reading_hints",
        lambda _book_id: {0: [{"offset_start": 0, "offset_end": 4, "reading": "こだかいおか"}]},
    )
    calls = []
    monkeypatch.setattr(
        service,
        "prepare_alignment",
        lambda book_id, **kwargs: calls.append((book_id, kwargs)) or {"status": "queued"},
    )

    assert service.maybe_refresh_alignment_after_reader_parse(180, 0) is True
    assert len(calls) == 1
    assert calls[0][0] == 180
    assert calls[0][1]["force"] is True
    assert int(calls[0][1]["priority"]) == 10
    # One canonical-cache-triggered rebuild per alignment fingerprint.
    assert service.maybe_refresh_alignment_after_reader_parse(180, 0) is False


def test_canonical_reader_parse_does_not_retry_alignment_that_already_had_readings(monkeypatch) -> None:
    service = _service()
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "leading_prefix_debug": {
                    "degraded": True,
                    "reason": "no_prefix_seed",
                    "reading_hint_count": 7,
                },
            }
        ],
        "processing": {"input_fingerprint": "fingerprint-v13"},
    }
    monkeypatch.setattr(service, "link_for_light_novel", lambda *_a, **_k: {"book": {"id": 283}})
    monkeypatch.setattr(service, "_load_alignment", lambda *_args: alignment)
    monkeypatch.setattr(
        service,
        "_cached_chapter_start_reading_hints",
        lambda _book_id: (_ for _ in ()).throw(AssertionError("should not read cache")),
    )

    assert service.maybe_refresh_alignment_after_reader_parse(180, 0) is False


def test_reader_api_hooks_trigger_alignment_repair_when_parse_becomes_ready() -> None:
    source = Path(__file__).parents[1].joinpath("pudge", "web_app.py").read_text(encoding="utf-8")
    chapter_block = source[source.index("    def light_novel_chapter("):source.index("    def light_novel_chapter_parse_status(")]
    status_block = source[source.index("    def light_novel_chapter_parse_status("):source.index("    def light_novel_stop_paired(")]
    assert "maybe_refresh_alignment_after_reader_parse" in chapter_block
    assert "maybe_refresh_alignment_after_reader_parse" in status_block
