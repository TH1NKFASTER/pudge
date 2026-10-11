from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace


from pudge.web_app import WebAppApi


def _old(path: Path) -> None:
    os.utime(path, (1, 1))


def test_install_status_preserves_model_download_failure(tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        tools=SimpleNamespace(manga_mokuro_python=""), paths=SimpleNamespace(cache_dir=tmp_path)
    )
    api._manga_ocr_install_lock = threading.Lock()
    api._manga_ocr_install_state = {"state": "failed", "detail": "model warm-up failed"}
    api._manga_ocr_install_thread = None
    # Mokuro's environment and package are there; warming up its models failed.
    python = tmp_path / "mokuro-ocr" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (tmp_path / "mokuro-ocr" / "package-ready.json").write_text("{}", encoding="utf-8")
    api._manga_ocr_log_path = lambda: tmp_path / "install.log"
    status = api.manga_ocr_status()
    assert status["state"] == "failed"
    assert status["detail"] == "model warm-up failed"
    assert status["installed"] is True and status["model_ready"] is False


def test_completed_cache_does_not_mask_failed_or_partial_ocr_job() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_threads = {}
    api._manga_book_ocr_state = {1: {"state": "failed", "errors": ["worker error"]}}
    api.manga = SimpleNamespace(
        ocr_cache_status=lambda _: {
            "complete": True,
            "total_pages": 2,
            "completed_pages": 2,
            "failed_pages": 0,
            "cached_pages": 2,
        }
    )
    status = api.manga_ocr_book_status(1)
    assert status["state"] == "failed"
    assert status["errors"] == ["worker error"]
    api._manga_book_ocr_state[1] = {"state": "partial", "errors": ["Jiten parser error"]}
    assert api.manga_ocr_book_status(1)["state"] == "partial"


def test_partial_ocr_run_does_not_mark_job_succeeded(tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        tools=SimpleNamespace(manga_mokuro_python=""), paths=SimpleNamespace(cache_dir=tmp_path)
    )
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_state = {}
    api._manga_ocr_cancel_events = {}
    api._manga_ocr_job_ids = {1: "job"}
    api.logger = SimpleNamespace(info=lambda *args: None, exception=lambda *args: None)
    results: list[tuple[str, str]] = []
    api.job_center = SimpleNamespace(
        fail=lambda _id, exc, **kwargs: results.append(("failed", str(exc))),
        finish=lambda _id, **kwargs: results.append(("succeeded", "")),
    )
    api.manga = SimpleNamespace(
        ocr_book_mokuro=lambda *args, **kwargs: {
            "complete": False,
            "cached_pages": 1,
            "errors": ["worker error"],
        },
        cached_region_texts=lambda _book_id: [],
    )
    api._run_manga_book_ocr(1)
    assert results == [("failed", "worker error")]
    assert api._manga_book_ocr_state[1]["state"] == "partial"
