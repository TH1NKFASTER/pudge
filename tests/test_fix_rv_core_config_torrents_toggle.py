from __future__ import annotations

import tomllib
from pathlib import Path

from pudge.config import AppConfig, write_torrents_enabled


def _config(path: Path, enabled: bool) -> AppConfig:
    config = AppConfig()
    config.config_path = path
    config.nyaa.torrents_enabled = enabled
    return config


def test_header_with_comment_is_updated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[paths]\nlibrary = "/x"\n\n[nyaa]   # torrent search\ntorrents_enabled = true\n'
        'base_url = "https://example.invalid"\n\n[ aria2 ] # client\nport = 6800\n',
        encoding="utf-8",
    )

    write_torrents_enabled(_config(path, False), path)

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["nyaa"]["torrents_enabled"] is False
    assert data["nyaa"]["base_url"] == "https://example.invalid"
    assert data["aria2"]["port"] == 6800


def test_unsupported_layout_never_writes_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '["nyaa"]\ntorrents_enabled = true\n'
    path.write_text(original, encoding="utf-8")

    write_torrents_enabled(_config(path, False), path)

    # Appending a second [nyaa] table would make the whole file unparseable.
    assert path.read_text(encoding="utf-8") == original
    tomllib.loads(path.read_text(encoding="utf-8"))


def test_missing_section_is_appended(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nlibrary = "/x"', encoding="utf-8")

    write_torrents_enabled(_config(path, True), path)

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["nyaa"]["torrents_enabled"] is True
    assert data["paths"]["library"] == "/x"
