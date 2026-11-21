"""Installation and operations review (rv5).

1. Debug bundles, Copy diagnostics and episode debug exports scrub credentials with
   DebugBundleBuilder._sanitize_text. It knew only the bare words (password=, api_key=,
   *token=): the environment names Pudge reads (JIMAKU_API_KEY=, PUDGE_QBITTORRENT_PASSWORD=,
   PUDGE_LLM_API_KEY=), config-style names (jimaku_api_key =, client_secret:) and tracker
   passkeys / ?key= / &auth= URL parameters went out as they were.
2. With the library, database or caches on an external disk that is unplugged, every config
   load ran mkdir under /Volumes: a PermissionError stopped the app, CLI and agent from
   starting, or an empty folder took the disk's name on the system disk.
3. `pudge --doctor` opened the library with Database(): it created an empty library where
   none was (on the system disk with the library disk unplugged) and could migrate the
   schema under a running Pudge. It now checks a present file read-only.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from pudge import cli
from pudge import config as config_module
from pudge.config import AppConfig, load_config
from pudge.diagnostics import DebugBundleBuilder

sanitize = DebugBundleBuilder._sanitize_text

SECRET = "Invented-Secret-4711"


@pytest.mark.parametrize(
    "line",
    [
        f"env JIMAKU_API_KEY={SECRET} pudge",
        f"PUDGE_QBITTORRENT_PASSWORD={SECRET}",
        f"PUDGE_LLM_API_KEY='{SECRET}' worker",
        f'jimaku_api_key = "{SECRET}"',
        f"llm_api_key: {SECRET}",
        f'{{"client_secret": "{SECRET}"}}',
        f"{{'X-Api-Key': '{SECRET}'}}",
        f"passwd={SECRET}",
        f"OPENAI_API_KEY={SECRET}",
        f"announce http://tracker.invalid/announce?passkey={SECRET}",
        f"GET https://api.invalid/v1/search?q=a&key={SECRET} 200",
        f"rss https://feed.invalid/?page=rss&auth={SECRET}",
    ],
)
def test_credentials_with_prefixed_names_are_scrubbed(line: str) -> None:
    cleaned = sanitize(line)
    assert SECRET not in cleaned
    assert "[REDACTED]" in cleaned


@pytest.mark.parametrize(
    "line",
    [
        "Jimaku API key: set",
        '"secrets_included": true',
        "EVENT torrent.startup_off previous_enabled=True aria2_stopped=False",
        "DONE step=backup.create path='/Users/me/Downloads/pudge-backup.zip' cached_files=3",
        "https://anilist.invalid/anime/12345?page=2",
    ],
)
def test_ordinary_lines_stay_readable(line: str) -> None:
    assert sanitize(line) == line


def test_the_debug_bundle_log_copy_uses_the_new_rules(tmp_path: Path) -> None:
    log = tmp_path / "pudge-agent-error.log"
    log.write_text(f"Traceback: env JIMAKU_API_KEY={SECRET}\nplain line\n", encoding="utf-8")
    copied = DebugBundleBuilder._sanitized_log(log)
    assert SECRET not in copied and "plain line" in copied


# ------------------------------------------------------------------ unplugged library disk


def _absent_volume() -> Path:
    volume = Path("/Volumes") / f"PudgeTestDisk-{uuid.uuid4().hex[:10]}"
    assert not volume.exists()
    return volume


def test_a_library_on_an_unplugged_disk_creates_nothing_and_loads(tmp_path: Path, monkeypatch) -> None:
    volumes_existed = Path("/Volumes").exists()
    volume = _absent_volume()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[paths]\n"
        f'cache_dir = "{tmp_path / "cache"}"\n'
        "[library]\n"
        f'root_dir = "{volume / "Anime"}"\n'
        f'database_path = "{volume / "pudge" / "library.sqlite3"}"\n'
        f'cover_cache_dir = "{volume / "covers"}"\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path, resolve_secrets=False, apply_language=False)
    assert loaded.library.root_dir == volume / "Anime"
    assert not volume.exists()
    assert Path("/Volumes").exists() == volumes_existed
    assert (tmp_path / "cache").is_dir()  # folders on present disks are still made


def test_a_folder_the_system_refuses_does_not_stop_the_start(tmp_path: Path, monkeypatch) -> None:
    refused = tmp_path / "refused" / "Anime"
    original = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self == refused:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\ncache_dir = "{tmp_path / "cache"}"\n[library]\nroot_dir = "{refused}"\n'
        f'database_path = "{tmp_path / "data" / "library.sqlite3"}"\n'
        f'cover_cache_dir = "{tmp_path / "covers"}"\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path, resolve_secrets=False, apply_language=False)
    assert loaded.library.root_dir == refused and not refused.exists()
    assert (tmp_path / "data").is_dir()


def test_only_volumes_paths_count_as_an_absent_disk(tmp_path: Path) -> None:
    assert config_module._on_absent_volume(_absent_volume() / "Anime")
    assert not config_module._on_absent_volume(tmp_path / "Volumes" / "Disk" / "Anime")
    assert not config_module._on_absent_volume(Path("/Volumes"))


# ------------------------------------------------------------------ --doctor


def _doctor_config(tmp_path: Path, database: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = database
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.qbittorrent.enabled = config.aria2.enabled = config.llm.enabled = False
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    config.tools.ffmpeg = config.tools.ffprobe = config.tools.mpv = str(tool)
    return config


def test_doctor_never_creates_a_library(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "ffsubsync", object())
    database = tmp_path / "away" / "library.sqlite3"
    status = cli.doctor(_doctor_config(tmp_path, database))
    out = capsys.readouterr().out
    assert f"Database: {database} (missing)" in out.splitlines()
    assert not database.exists() and not database.parent.exists()
    assert status == 0


def test_doctor_checks_a_present_library_read_only(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "ffsubsync", object())
    database = tmp_path / "library.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample(x)")
        conn.execute("PRAGMA user_version=3")
    before = database.read_bytes()
    assert cli.doctor(_doctor_config(tmp_path, database)) == 0
    assert f"Database: {database} (ok)" in capsys.readouterr().out.splitlines()
    assert database.read_bytes() == before  # no schema, no migration
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    database.write_bytes(b"not a database at all" * 100)
    assert cli.doctor(_doctor_config(tmp_path, database)) == 1
    assert f"Database: {database} (broken:" in capsys.readouterr().out
