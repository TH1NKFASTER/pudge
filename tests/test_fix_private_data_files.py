"""Pudge's own data is this macOS user's only (WEBSEC-3); the media library keeps normal modes.

The data, config, cache and log folders are 0700, the database (journals, copies), log
files and the page server's folder 0600/0700, with explicit modes. The process umask is
not changed: show folders, subtitles next to videos and downloads must stay readable for
a media server running as another account (Plex, Jellyfin, Kodi) or a NAS share.
mpv's socket is never in a shared /tmp.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import stat
from pathlib import Path

import pytest

from pudge import private_files
from pudge.database import Database
from pudge.extra_files import title_folder_name
from pudge.subtitle_formats import write_srt


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _current_umask() -> int:
    value = os.umask(0o022)
    os.umask(value)
    return value


def test_a_new_library_database_is_private_even_under_umask_022(tmp_path: Path, umask_022) -> None:
    data = tmp_path / "data"
    database = Database(data / "library.sqlite3")
    database.set_state("probe", "1")
    assert _mode(data / "library.sqlite3") == 0o600
    wal = data / "library.sqlite3-wal"
    if wal.exists():
        assert _mode(wal) == 0o600


def test_an_existing_world_readable_database_and_its_copies_become_private(tmp_path: Path, umask_022) -> None:
    path = tmp_path / "library.sqlite3"
    database = Database(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.chmod(0o644)
    daily = path.with_name(f"{path.name}.daily-20260101-000000.backup")
    daily.write_bytes(b"copy")
    daily.chmod(0o644)
    Database(path, initialize=False)
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"), daily):
        if candidate.exists():
            assert _mode(candidate) == 0o600, candidate.name
    # The copy made before a schema migration is private from the start.
    with database.connect() as conn:
        migration_copy = database._backup_before_migration(conn, 1)
    assert _mode(migration_copy) == 0o600


def test_startup_makes_pudges_own_folders_and_logs_private_but_keeps_the_umask(
    tmp_path: Path, monkeypatch, umask_022
) -> None:
    folders = {name: tmp_path / name for name in ("data", "config", "cache", "logs-pudge")}
    for folder in folders.values():
        folder.mkdir()
        folder.chmod(0o755)
    debug = folders["logs-pudge"] / "debug"
    debug.mkdir()
    debug.chmod(0o755)
    db = folders["data"] / "library.sqlite3"
    runtime_log = tmp_path / "pudge-runtime.log"
    energy_log = tmp_path / "pudge-energy.jsonl"
    for file in (db, runtime_log, energy_log):
        file.write_bytes(b"")
        file.chmod(0o644)
    monkeypatch.setattr(private_files, "DATA_DIR", folders["data"])
    monkeypatch.setattr(private_files, "CONFIG_DIR", folders["config"])
    monkeypatch.setattr(private_files, "CACHE_DIR", folders["cache"])
    monkeypatch.setattr(private_files, "DEBUG_LOG_DIR", debug)
    monkeypatch.setattr(private_files, "DEFAULT_DATABASE_PATH", db)
    monkeypatch.setattr(private_files, "DEFAULT_RUNTIME_LOG_PATH", runtime_log)
    monkeypatch.setattr(private_files, "DEFAULT_ENERGY_LOG_PATH", energy_log)
    private_files.harden_private_storage()
    assert {name: _mode(folder) for name, folder in folders.items()} == dict.fromkeys(folders, 0o700)
    assert _mode(debug) == 0o700
    assert (_mode(db), _mode(runtime_log), _mode(energy_log)) == (0o600, 0o600, 0o600)
    assert _current_umask() == 0o022


def test_a_symlinked_or_foreign_path_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert private_files.secure_directory(link) is False
    assert _mode(target) == 0o755


def _run_entry_point(entry: str, monkeypatch, calls: list[str]) -> None:
    if entry == "cli":
        from pudge import cli

        monkeypatch.setattr(cli, "build_parser", lambda: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            cli.main(["--help"])
    elif entry == "agent":
        from pudge import agent

        monkeypatch.setattr(agent, "_main", lambda argv: calls.append("main") or 0)
        assert agent.main([]) == 0
    else:
        from pudge import app_entry

        monkeypatch.setattr(
            app_entry, "maybe_handle_notification_helper", lambda args: calls.append("main") or 0
        )
        assert app_entry.main([]) == 0


@pytest.mark.parametrize("entry", ["cli", "agent", "app"])
def test_every_entry_point_hardens_storage_first(entry: str, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(private_files, "harden_private_storage", lambda: calls.append("harden"))
    _run_entry_point(entry, monkeypatch, calls)
    assert calls[0] == "harden"


@pytest.mark.parametrize("entry", ["cli", "agent", "app"])
def test_library_files_keep_the_users_normal_modes_after_startup(
    entry: str, tmp_path: Path, monkeypatch, umask_022
) -> None:
    for name in ("DATA_DIR", "CONFIG_DIR", "CACHE_DIR"):
        monkeypatch.setattr(private_files, name, tmp_path / "pudge" / name.lower())
    _run_entry_point(entry, monkeypatch, [])
    assert _current_umask() == 0o022
    # A new show folder in the library, the .anilist.id marker and a subtitle next to the video.
    show = tmp_path / "Library" / title_folder_name("Frieren: Beyond Journey's End")
    show.mkdir(parents=True)
    (show / ".anilist.id").write_text("154587", encoding="utf-8")
    video = show / "Frieren - 01.mkv"
    video.write_bytes(b"video")
    subtitle = write_srt([(1.0, 2.0, "日本語")], video.with_suffix(".ja.srt"))
    assert _mode(show) == 0o755
    assert (_mode(show / ".anilist.id"), _mode(subtitle)) == (0o644, 0o644)


def test_runtime_log_and_rotations_are_private(tmp_path: Path, umask_022) -> None:
    from pudge import logging_utils

    log_path = tmp_path / "logs" / "pudge-runtime.log"
    logging_utils.set_log_rotation_owner(True)
    try:
        logger = logging_utils.configure_logging(log_path)
    finally:
        logging_utils.set_log_rotation_owner(False)
    added = [handler for handler in logger.handlers if getattr(handler, "baseFilename", "") == str(log_path)]
    try:
        logger.info("EVENT test.private_log")
        assert _mode(log_path) == 0o600
        for handler in added:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler.doRollover()
        assert _mode(log_path) == 0o600 and _mode(log_path.with_name(log_path.name + ".1")) == 0o600
    finally:
        for handler in added:
            logger.removeHandler(handler)
            handler.close()


def test_energy_log_and_debug_folder_are_private(tmp_path: Path, monkeypatch, umask_022) -> None:
    from pudge import energy_diagnostics, logging_utils

    energy_log = tmp_path / "pudge-energy.jsonl"
    monkeypatch.setattr(energy_diagnostics, "ENERGY_LOG_PATH", energy_log)
    monitor = energy_diagnostics.EnergyDiagnosticsMonitor.__new__(energy_diagnostics.EnergyDiagnosticsMonitor)
    monitor.logger = None
    monitor._append({"sample": 1})
    assert _mode(energy_log) == 0o600
    debug = tmp_path / "Logs" / "pudge" / "debug"
    monkeypatch.setattr(logging_utils, "DEBUG_LOG_DIR", debug)
    assert logging_utils.debug_log_dir(cleanup=False) == debug
    assert _mode(debug) == 0o700 and _mode(debug.parent) == 0o700


def test_the_page_servers_folder_is_private(tmp_path: Path, umask_022) -> None:
    from pudge.web_app import _start_asset_server
    from test_web_app import make_api

    api = make_api(tmp_path)
    server, _base = _start_asset_server(api)
    try:
        assert _mode(api.config.paths.cache_dir / "web-ui") == 0o700
    finally:
        server.shutdown()
        server.server_close()
        api.close()


# ------------------------------------------------------------------ mpv IPC socket


def _shared_tmp(tmp_path: Path) -> Path:
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    shared.chmod(0o1777)
    return shared


def test_socket_folder_is_tmpdir_when_it_is_private(tmp_path: Path, monkeypatch) -> None:
    private = tmp_path / "T"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    monkeypatch.setattr(private_files.tempfile, "gettempdir", lambda: str(private))
    assert private_files.private_socket_dir() == private


def test_without_tmpdir_the_socket_goes_to_a_private_per_user_folder(tmp_path: Path, monkeypatch) -> None:
    shared = _shared_tmp(tmp_path)
    monkeypatch.setattr(private_files.tempfile, "gettempdir", lambda: str(shared))
    monkeypatch.setattr(private_files, "_SHARED_TMP", shared)
    folder = private_files.private_socket_dir()
    assert folder == shared / f"pudge-{os.getuid()}"
    assert folder.is_dir() and _mode(folder) == 0o700


def test_a_squatted_per_user_folder_is_not_used(tmp_path: Path, monkeypatch) -> None:
    shared = _shared_tmp(tmp_path)
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    (shared / f"pudge-{os.getuid()}").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(private_files.tempfile, "gettempdir", lambda: str(shared))
    monkeypatch.setattr(private_files, "_SHARED_TMP", shared)
    monkeypatch.setattr(private_files, "CACHE_DIR", tmp_path / "cache")
    folder = private_files.private_socket_dir()
    assert folder == tmp_path / "cache" / "ipc" and _mode(folder) == 0o700


def test_mpv_is_started_with_its_socket_in_the_private_folder(tmp_path: Path, monkeypatch) -> None:
    from test_skip_segments_playback import _launch

    shared = _shared_tmp(tmp_path)
    monkeypatch.setattr(private_files.tempfile, "gettempdir", lambda: str(shared))
    monkeypatch.setattr(private_files, "_SHARED_TMP", shared)
    command, _env, _existed = _launch(tmp_path / "play", monkeypatch, exclusive=False, segments=False)
    socket = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--input-ipc-server=")))
    assert socket.parent == shared / f"pudge-{os.getuid()}"
    assert _mode(socket.parent) == 0o700
