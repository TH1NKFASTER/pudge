from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge import foreground, safe_mode
from pudge.cli import _subtitle_content_fingerprint
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_ocr_artifact import read_artifact
from pudge.process_utils import pid_alive
from pudge.subtitles.discovery import _content_fingerprint, content_fingerprint
from pudge.web_app import WebAppApi


def _write_cbz(path: Path, count: int = 3) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(count):
            buffer = io.BytesIO()
            Image.new("RGB", (32 + index, 40 + index), "white").save(
                buffer, format="PNG"
            )
            archive.writestr(f"{index + 1:03d}.png", buffer.getvalue())


def test_single_page_ocr_update_reads_only_changed_archive_page(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "manga.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    archive_path = tmp_path / "book.cbz"
    _write_cbz(archive_path, 3)
    book_id = int(service.import_file(archive_path)["id"])
    fingerprint, generation, _revision = service._ocr_context(book_id)

    original_read = zipfile.ZipFile.read
    page_reads: list[str] = []

    def counted_read(self, name, *args, **kwargs):
        if str(name).lower().endswith(".png"):
            page_reads.append(str(name))
        return original_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", counted_read)

    for page_index in range(3):
        assert service._commit_ocr_page_updates(
            book_id,
            source_fingerprint=fingerprint,
            generation=generation,
            updates=[
                (
                    page_index,
                    [{"text": f"page-{page_index}", "detector": "vision", "recognizer": "mangaocr"}],
                    "ready",
                    "",
                    False,
                )
            ],
        )

    # Old post-page rebuild behavior read 1 + 2 + 3 archive images.  Once the
    # first projection exists, each later page update reads only that page.
    assert page_reads == ["001.png", "002.png", "003.png"]
    artifact = read_artifact(service._ocr_artifact_path(book_id))
    assert artifact is not None
    assert [page["page_index"] for page in artifact["pages"]] == [0, 1, 2]
    assert [page["text"] for page in artifact["pages"]] == ["page-0", "page-1", "page-2"]
    assert int(artifact["revision"]) == service._ocr_context(book_id)[2]


def test_incremental_page_refresh_preserves_other_artifact_pages(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "manga.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    archive_path = tmp_path / "book.cbz"
    _write_cbz(archive_path, 2)
    book_id = int(service.import_file(archive_path)["id"])
    fingerprint, generation, _revision = service._ocr_context(book_id)

    for page_index in range(2):
        assert service._commit_ocr_page_updates(
            book_id,
            source_fingerprint=fingerprint,
            generation=generation,
            updates=[(page_index, [{"text": f"old-{page_index}"}], "ready", "", False)],
        )

    original_read = zipfile.ZipFile.read
    page_reads: list[str] = []

    def counted_read(self, name, *args, **kwargs):
        if str(name).lower().endswith(".png"):
            page_reads.append(str(name))
        return original_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", counted_read)
    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(1, [{"text": "new-1"}], "ready", "", False)],
    )

    assert page_reads == ["002.png"]
    artifact = read_artifact(service._ocr_artifact_path(book_id))
    assert artifact is not None
    assert [page["text"] for page in artifact["pages"]] == ["old-0", "new-1"]


def test_subtitle_content_fingerprint_has_one_shared_implementation(tmp_path: Path) -> None:
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n<font>テスト</font>\n",
        encoding="utf-8",
    )
    candidate = SimpleNamespace(path=subtitle)
    cache_dir = tmp_path / "cache"

    shared = content_fingerprint(candidate, cache_dir, ffmpeg_path="ffmpeg")
    assert shared
    assert _content_fingerprint(candidate, cache_dir, ffmpeg_path="ffmpeg") == shared
    assert _subtitle_content_fingerprint(candidate, cache_dir, ffmpeg_path="ffmpeg") == shared


def test_pid_alive_private_names_are_thin_compatibility_aliases() -> None:
    assert safe_mode._pid_alive is pid_alive
    assert foreground._pid_alive is pid_alive


class _FakeLightNovels:
    def __init__(self) -> None:
        self.study_calls: list[tuple] = []
        self.translate_calls: list[tuple] = []

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_action(self, *args, **kwargs):
        self.study_calls.append((args, kwargs))
        return {"ok": True, "kind": "study"}

    def translate_selection(self, *args):
        self.translate_calls.append(args)
        return {"ok": True, "kind": "translate"}


def test_light_novel_web_names_delegate_to_canonical_wrappers() -> None:
    api = WebAppApi.__new__(WebAppApi)
    fake = _FakeLightNovels()
    api.light_novels = fake

    payload = {"word_id": 7, "reading_index": 2, "grade": "easy"}
    assert api.light_novel_study_action(payload) == {"ok": True, "kind": "study"}
    assert len(fake.study_calls) == 1

    assert api.light_novel_translate("猫", "文脈", "English", 42) == {
        "ok": True,
        "kind": "translate",
    }
    assert fake.translate_calls == [("猫", "文脈", "English", 42)]
