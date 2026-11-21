"""Show in Finder reveals a folder audiobook, never `open`s it (an .app folder would run) — WEBSEC-4."""

from __future__ import annotations

import sys
from pathlib import Path

from desktop_isolation import patch_webapp_popen
from test_web_app import make_api


def test_show_in_finder_reveals_a_folder_book_even_when_it_is_an_app_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    bundle = tmp_path / "Downloads" / "Foo.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    first, second = bundle / "01.mp3", bundle / "02.mp3"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")
    book = api.audiobooks._upsert(
        path=bundle,
        title="Foo",
        duration=1200.0,
        files=[
            {"index": 0, "path": str(first), "title": "1", "duration": 600.0, "start": 0.0, "end": 600.0},
            {"index": 1, "path": str(second), "title": "2", "duration": 600.0, "start": 600.0, "end": 1200.0},
        ],
        chapters=[],
    )
    opened: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    # Only web_app's Popen (the api's audiobook cover thread runs ffmpeg meanwhile).
    patch_webapp_popen(monkeypatch, lambda command, *a, **k: opened.append(command))
    assert api.audiobook_reveal_source(int(book["id"]))["ok"] is True
    assert opened == [["open", "-R", str(bundle.resolve())]]
