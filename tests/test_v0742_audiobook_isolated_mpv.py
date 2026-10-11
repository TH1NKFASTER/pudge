"""Audiobook playback runs an isolated mpv: no user config, no user scripts, no study
plugins — also when a light novel is read together with it (behaviour; audit 71Б rewrite:
the launched command is captured instead of parsing the source)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService


class _Process:
    def poll(self):
        return None


@pytest.fixture
def launched(monkeypatch) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.Popen",
        lambda command, **_kwargs: commands.append(list(command)) or _Process(),
    )
    # No monitor or cover threads: the test only looks at the command.
    monkeypatch.setattr(
        "pudge.audiobooks.threading.Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None, is_alive=lambda: False),
    )
    return commands


def _service(tmp_path: Path) -> tuple[AudiobookService, int, AppConfig]:
    config = AppConfig()
    config.library.database_path = tmp_path / "db.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    service = AudiobookService(
        Database(config.library.database_path), ffprobe="ffprobe", mpv="mpv", cache_dir=config.paths.cache_dir
    )
    path = tmp_path / "book.m4b"
    path.write_bytes(b"audio")
    book = service._upsert(
        path=path,
        title="Book",
        duration=120.0,
        files=[
            {"index": 0, "path": str(path), "title": "Book", "duration": 120.0, "start": 0.0, "end": 120.0}
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 120.0}],
    )
    return service, int(book["id"]), config


def players(commands: list[list[str]]) -> list[list[str]]:
    """The playback processes among everything launched (covers are read with ffmpeg)."""
    return [command for command in commands if Path(command[0]).name == "mpv"]


def _assert_isolated(command: list[str], tmp_path: Path) -> None:
    options = command[1 : command.index("--no-video")]
    # User config and auto-loaded scripts are off before anything else is decided.
    assert "--no-config" in options and "--load-scripts=no" in options
    # Nothing loads a study plugin (JitenMPV / jpdb) explicitly either.
    assert not [arg for arg in command if arg.startswith(("--script", "--scripts"))]
    # (The test's own folder may contain such a word: the temp folder is the user's choice.)
    args = [arg.replace(str(tmp_path), "") for arg in command]
    assert not [arg for arg in args if "jiten" in arg.casefold() or "jpdb" in arg.casefold()]


def test_audiobook_mpv_ignores_all_user_config_and_scripts(tmp_path: Path, launched) -> None:
    service, book_id, _config = _service(tmp_path)
    service.play(book_id, start=12.5)
    [command] = players(launched)
    assert command[0] == "mpv"
    _assert_isolated(command, tmp_path)
    assert "--no-video" in command and command[-1] == "--}"


def test_paired_reading_uses_the_same_isolated_audiobook_player(tmp_path: Path, launched) -> None:
    service, book_id, config = _service(tmp_path)
    source = tmp_path / "novel.txt"
    source.write_text("第一章\n架空の物語です。", encoding="utf-8")
    ln_id = int(LightNovelService(config).import_file(source)["id"])
    service.link_light_novel(ln_id, book_id, prepare_alignment=False)

    service.play_paired(ln_id, 0, 0.5)

    [command] = players(launched)
    _assert_isolated(command, tmp_path)
    assert any(arg.startswith("--input-ipc-server=") for arg in command)
