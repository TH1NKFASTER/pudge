from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database


def test_multifile_resume_offset_applies_only_to_selected_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    folder = tmp_path / "audio"
    folder.mkdir()
    for name in ("01.mp3", "02.mp3", "03.mp3"):
        (folder / name).write_bytes(b"audio")

    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (100.0, []))
    monkeypatch.setattr(
        service,
        "prepare_transcription",
        lambda *_args, **_kwargs: {"status": "queued", "ready": False},
    )
    book = service.import_folder(folder)
    commands: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.Popen",
        lambda command, **_kwargs: commands.append(command) or FakeProcess(),
    )
    monkeypatch.setattr(
        "pudge.audiobooks.threading.Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None),
    )

    service.play(book["id"], start=125.0)

    command = commands[0]
    second = next(item for item in command if Path(item).name == "02.mp3")
    third = next(item for item in command if Path(item).name == "03.mp3")
    selected = command.index(second)

    assert "--playlist-start=1" in command
    assert "--start=25.000" not in command[: command.index("--{")]
    assert command[selected - 2 : selected + 2] == [
        "--{",
        "--start=25.000",
        second,
        "--}",
    ]
    assert command[command.index(third) - 1] == "--}"
    assert command.count("--start=25.000") == 1


def test_single_file_resume_still_uses_file_local_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (100.0, []))
    monkeypatch.setattr(
        service,
        "prepare_transcription",
        lambda *_args, **_kwargs: {"status": "queued", "ready": False},
    )
    book = service.import_file(source)
    commands: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.Popen",
        lambda command, **_kwargs: commands.append(command) or FakeProcess(),
    )
    monkeypatch.setattr(
        "pudge.audiobooks.threading.Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None),
    )

    service.play(book["id"], start=12.5)

    command = commands[0]
    source_arg = next(item for item in command if Path(item).name == "book.mp3")
    source_index = command.index(source_arg)
    assert command[source_index - 2 : source_index + 2] == [
        "--{",
        "--start=12.500",
        source_arg,
        "--}",
    ]
