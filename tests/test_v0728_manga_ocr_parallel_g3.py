from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from pudge import manga_ocr_parallel as parallel


def test_adaptive_workers_are_bounded_and_fail_closed() -> None:
    gib = 1024**3
    assert parallel.worker_count(40, 0) == 1
    assert parallel.worker_count(40, 18 * gib) == 1
    assert parallel.worker_count(40, 28 * gib) == 2
    assert parallel.worker_count(40, 38 * gib) == 2
    assert parallel.worker_count(40, 1024 * gib) == 2
    assert parallel.worker_count(5, 1024 * gib) == 1
    assert parallel.worker_count(6, 28 * gib) == 2


def fake_worker(tmp_path: Path) -> dict[str, str]:
    script = tmp_path / "mock_ocr_batch.py"
    script.write_text(
        """import json, os, sys, time
from pathlib import Path
assert sys.argv[1] == '--batch'
manifest, output, progress, stop = map(Path, sys.argv[2:])
pages = json.loads(manifest.read_text())['pages']
with output.open('w') as writer:
    for done, page in enumerate(pages, 1):
        if stop.exists(): sys.exit(75)
        time.sleep(float(os.environ.get('MOCK_OCR_DELAY', '0.02')))
        writer.write(json.dumps({'page_index': page['page_index'], 'regions': [{'text': str(os.getpid())}]}) + '\\n')
        writer.flush()
        progress.write_text(json.dumps({'done': done, 'page_index': page['page_index']}))
""",
        encoding="utf-8",
    )
    return {**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")}


def make_manifest(tmp_path: Path, page_count: int = 12) -> tuple[Path, Path, Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"archive": "unused", "pages": [{"page_index": i, "name": str(i), "regions": []} for i in range(page_count)]}))
    return manifest, tmp_path / "output.jsonl", tmp_path / "progress.json", tmp_path / "stop"


def test_parallel_ocr_merges_all_pages_without_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = fake_worker(tmp_path)
    monkeypatch.setattr(parallel.subprocess, "Popen", _real_popen_with_env(env))
    paths = make_manifest(tmp_path)
    assert parallel.run(*paths, worker_module="mock_ocr_batch", free_bytes=38 * 1024**3) == 0
    rows = [json.loads(line) for line in paths[1].read_text().splitlines()]
    assert len(rows) == 12
    assert sorted(row["page_index"] for row in rows) == list(range(12))
    assert len({row["regions"][0]["text"] for row in rows}) == 2
    assert json.loads(paths[2].read_text())["done"] == 12
    assert not list(tmp_path.glob("ocr-shards-*"))


def _real_popen_with_env(env: dict[str, str]):
    original = subprocess.Popen
    def run(*args, **kwargs):
        kwargs["env"] = env
        return original(*args, **kwargs)
    return run


def test_parallel_ocr_stop_retains_completed_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = fake_worker(tmp_path)
    env["MOCK_OCR_DELAY"] = "0.06"
    monkeypatch.setattr(parallel.subprocess, "Popen", _real_popen_with_env(env))
    manifest, output, progress, stop = make_manifest(tmp_path, 30)
    result: list[int] = []
    def task() -> None:
        result.append(parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch", free_bytes=38 * 1024**3))
    thread = threading.Thread(target=task)
    thread.start()
    time.sleep(0.20)
    stop.write_text("stop")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == [75]
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len({row["page_index"] for row in rows}) == len(rows)
    assert len(rows) < 30
    assert not list(tmp_path.glob("ocr-shards-*"))


def test_single_worker_when_memory_probe_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: None)
    assert parallel.worker_count(100) == 1


def test_book_ocr_uses_parallel_coordinator_and_retries_only_missing_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zipfile
    from PIL import Image
    from pudge.database import Database
    from pudge.manga import MangaService
    from pudge import manga as manga_module

    archive = tmp_path / "book.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (24, 24), "white").save(image)
    with zipfile.ZipFile(archive, "w") as bundle:
        for index in range(6):
            bundle.write(image, arcname=f"{index:02d}.png")
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(archive), "Fixture", 6, 0, "rtl", time.time(), time.time()),
        )
        book_id = int(cursor.lastrowid)
    service = MangaService(db, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "ocr_available", lambda **_kwargs: True)
    monkeypatch.setattr(service, "_vision_text_regions", lambda _image: [])
    calls: list[tuple[str, list[int]]] = []

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: object) -> None:
            manifest = json.loads(Path(command[-4]).read_text())
            pages = [int(row["page_index"]) for row in manifest["pages"]]
            calls.append((command[2], pages))
            # First run has one missing row; second run must retry only it.
            saved = pages[:-1] if len(calls) == 1 else pages
            Path(command[-3]).write_text(
                "".join(json.dumps({"page_index": page, "regions": [{"text": "text"}]}) + "\n" for page in saved)
            )

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(manga_module.subprocess, "Popen", FakePopen)
    first = service.ocr_book(book_id)
    assert calls[0] == ("pudge.manga_ocr_parallel", list(range(6)))
    assert first["ok"] is False and first["cached_pages"] == 5
    second = service.ocr_book(book_id)
    assert calls[1] == ("pudge.manga_ocr_worker", [5])
    assert second["ok"] is True and second["cached_pages"] == 6


def test_memory_drop_defers_unstarted_shards_without_losing_completed_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fake_worker(tmp_path)
    monkeypatch.setattr(parallel.subprocess, "Popen", _real_popen_with_env(env))
    gib = 1024**3
    readings = iter([38 * gib, 10 * gib, 10 * gib])
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: next(readings))
    manifest, output, progress, stop = make_manifest(tmp_path)
    assert parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch") == 1
    saved = [json.loads(line)["page_index"] for line in output.read_text().splitlines()]
    assert sorted(saved) == [0, 2, 4, 6, 8, 10]
    # No page is published as a successful empty OCR result merely because
    # another model could not start. A subsequent run can retry exactly these.
    missing = [index for index in range(12) if index not in saved]
    manifest.write_text(json.dumps({"archive": "unused", "pages": [{"page_index": i} for i in missing]}))
    assert parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch", free_bytes=38 * gib) == 0
    recovered = [json.loads(line)["page_index"] for line in output.read_text().splitlines()]
    assert sorted(recovered) == missing
