"""Listen together: an alignment built without the reader's readings is rebuilt once the
reader's parse of the chapter is ready (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from pudge import web_app
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


def test_parse_status_schedules_the_alignment_repair_once_the_parse_is_ready() -> None:
    # The reader's parse poll answers at once; a ready parse with words schedules the
    # alignment repair in the background (the chapter call does the same, see
    # test_v179_torrent_ln_runtime_hardening.py::
    # test_reader_parse_alignment_refresh_is_scheduled_after_response_not_run_inline).
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    answers = {
        1: {"ready": False},
        2: {"ready": True, "payload": {"tokens": []}},
        3: {"ready": True, "payload": {"tokens": [[{"surface": "狼"}]]}},
    }
    api.light_novels = SimpleNamespace(chapter_parse_status=lambda book, chapter: answers[chapter])
    repaired: list[tuple] = []
    api.audiobooks = SimpleNamespace(
        maybe_refresh_alignment_after_reader_parse=lambda *args: repaired.append(args)
    )
    queued: list[tuple] = []

    class Supervisor:
        def start(self, name, target, *, args=(), **kwargs):
            queued.append((target, tuple(args)))
            return SimpleNamespace()

    api.task_supervisor = Supervisor()
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    assert [api.light_novel_chapter_parse_status(180, chapter) for chapter in (1, 2, 3)] == [
        answers[1],
        answers[2],
        answers[3],
    ]
    assert repaired == [] and len(queued) == 1
    target, args = queued[0]
    target(*args)
    assert repaired == [(180, 3)]
