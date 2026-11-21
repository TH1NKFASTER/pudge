"""Review round 6, data/storage/config/infrastructure fixes."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from pudge import config as config_module
from pudge.database import Database
from pudge.logging_utils import configure_logging


# --- configure_logging: one handler for a log folder reached through a symlink -----------------


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_configure_logging_keeps_one_handler_for_a_symlinked_log_folder(tmp_path: Path) -> None:
    real = tmp_path / "real-logs"
    real.mkdir()
    link = tmp_path / "linked-logs"
    link.symlink_to(real, target_is_directory=True)
    log_path = link / "runtime.log"

    logger = configure_logging(log_path)
    for _ in range(4):  # modules use configure_logging() as a logger getter
        assert configure_logging(log_path) is logger
    ours = [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
        and os.path.realpath(handler.baseFilename) == str((real / "runtime.log").resolve())
    ]
    assert len(ours) == 1

    logger.info("EVENT test.rv6_single_line")
    for handler in ours:
        handler.flush()
    text = (real / "runtime.log").read_text(encoding="utf-8")
    assert text.count("EVENT test.rv6_single_line") == 1


def test_configure_logging_plain_path_still_reuses_its_handler(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "runtime.log"
    logger = configure_logging(log_path)
    configure_logging(log_path)
    ours = [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path)
    ]
    assert len(ours) == 1


# --- subtitle selection recovery by file name: "\" in the name ----------------------------------


def _video(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize("name", ["Show\\01.mkv", "Show\\_01.mkv", "Show 100%\\x_01.mkv"])
def test_moved_file_with_a_backslash_finds_its_selection_by_name(tmp_path: Path, name: str) -> None:
    db = Database(tmp_path / "library.sqlite3")
    payload = os.urandom(4096)
    old = _video(tmp_path / "old" / name, payload)
    db.record_subtitle_history(
        video_path=old,
        media_id=None,
        episode=None,
        source="jimaku",
        candidate_name="ep01.srt",
        candidate_path=tmp_path / "cache" / "ep01.srt",
        status="selected",
    )
    # The file moves (same bytes, same name, another folder).
    new = _video(tmp_path / "new" / name, payload)
    old.unlink()

    found = db.latest_selected_subtitle_for_media_or_filename(video_path=new, media_id=None, episode=None)

    assert found is not None
    assert found["video_path"] == str(old)
    assert found["candidate_name"] == "ep01.srt"


def test_backslash_name_does_not_treat_underscore_as_a_wildcard(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    payload = os.urandom(4096)
    # Another file whose name differs only where "\_" used to be a wildcard; same bytes,
    # so only the name decides.
    other = _video(tmp_path / "old" / "Show\\X01.mkv", payload)
    db.record_subtitle_history(
        video_path=other,
        media_id=None,
        episode=None,
        candidate_name="other.srt",
        status="selected",
    )
    new = _video(tmp_path / "new" / "Show\\_01.mkv", payload)

    assert (
        db.latest_selected_subtitle_for_media_or_filename(video_path=new, media_id=None, episode=None) is None
    )


# --- load_config: the Jimaku key is read once --------------------------------------------------


def test_load_config_reads_the_jimaku_key_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[jimaku]\napi_key = "jk-test-123"\n'
        f'[paths]\ncache_dir = "{tmp_path / "cache"}"\n'
        f'[library]\nroot_dir = "{tmp_path / "lib"}"\ndatabase_path = "{tmp_path / "db" / "library.sqlite3"}"\n'
        f'cover_cache_dir = "{tmp_path / "covers"}"\n',
        encoding="utf-8",
    )
    calls: list[str] = []

    class CountingStore:
        """The real store (slots: its methods cannot be patched), counting resolve()."""

        def __init__(self, store: object) -> None:
            self.store = store

        def resolve(self, account: str, *args: object, **kwargs: object) -> str:
            calls.append(account)
            return self.store.resolve(account, *args, **kwargs)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self.store, name)

    monkeypatch.setattr(config_module, "_SECRET_STORE", CountingStore(config_module._SECRET_STORE))
    monkeypatch.delenv("JIMAKU_API_KEY", raising=False)

    cfg = config_module.load_config(path, apply_language=False)

    assert calls.count("jimaku-api-key") == 1
    assert cfg.jimaku.personal_api_key == "jk-test-123"
    assert cfg.jimaku.api_key == "jk-test-123"
