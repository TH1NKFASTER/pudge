"""The audiobook TTS server key lives in the Keychain and never in a backup (WEBSEC-6)."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from pudge import study_secrets
from pudge.backup import create_backup
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


class FakeKeychain:
    available = True

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def get(self, account: str) -> str:
        return self.items.get(account, "")

    def set(self, account: str, value: str) -> bool:
        self.items[account] = value
        return True

    def delete(self, account: str) -> None:
        self.items.pop(account, None)


def _ln(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def _stored(path: Path, key: str) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM ln_settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else ""


def test_the_tts_key_moves_to_the_keychain(tmp_path: Path, monkeypatch) -> None:
    keychain = FakeKeychain()
    monkeypatch.setattr(study_secrets, "_STORE", keychain)
    study_secrets.forget_cache()
    service = _ln(tmp_path)
    service.save_settings({"irodori_tts_api_key": "tts-secret-key"})
    assert keychain.items["ln-irodori-tts-api-key"] == "tts-secret-key"
    assert _stored(tmp_path / "library.sqlite3", "irodori_tts_api_key") == ""
    assert service.settings().irodori_tts_api_key == "tts-secret-key"
    # A key an older version left in SQLite moves on the next read.
    with sqlite3.connect(tmp_path / "library.sqlite3") as conn:
        conn.execute("UPDATE ln_settings SET value='legacy-tts' WHERE key='irodori_tts_api_key'")
    study_secrets.forget_cache()
    assert service.settings().irodori_tts_api_key == "legacy-tts"
    assert keychain.items["ln-irodori-tts-api-key"] == "legacy-tts"
    assert _stored(tmp_path / "library.sqlite3", "irodori_tts_api_key") == ""
    service.save_settings({"irodori_tts_api_key": ""})
    assert "ln-irodori-tts-api-key" not in keychain.items
    study_secrets.forget_cache()


def test_a_backup_holds_neither_the_tts_key_nor_a_password_in_its_address(tmp_path: Path) -> None:
    # Without a Keychain (the conftest default) the key stays in SQLite: the backup must blank it.
    service = _ln(tmp_path)
    service.save_settings(
        {
            "irodori_tts_api_key": "tts-secret-key-0123",
            "irodori_tts_url": "https://alice:tts-url-password@tts.example:8443",
        }
    )
    database = tmp_path / "library.sqlite3"
    assert _stored(database, "irodori_tts_api_key") == "tts-secret-key-0123"
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    output = tmp_path / "out" / "backup.zip"
    result = create_backup(
        config_path=config, database_path=database, cache_dir=tmp_path / "cache", output=output, version="t"
    )
    assert result is not None
    with zipfile.ZipFile(output) as bundle:
        names = [name for name in bundle.namelist() if name.endswith("library.sqlite3")]
        assert names
        raw = bundle.read(names[0])
    assert b"tts-secret-key-0123" not in raw and b"tts-url-password" not in raw
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(raw)
    assert _stored(restored, "irodori_tts_api_key") == ""
    assert _stored(restored, "irodori_tts_url") == "https://alice@tts.example:8443"
    # The live library is untouched.
    assert _stored(database, "irodori_tts_api_key") == "tts-secret-key-0123"


def _restore_on_this_mac(tmp_path: Path, database: Path, archive: Path) -> None:
    from pudge.backup import restore_backup

    restore_backup(
        archive_path=archive,
        config_path=tmp_path / "config.toml",
        database_path=database,
        cache_dir=tmp_path / "cache",
    )


def test_restoring_a_backup_keeps_this_macs_tts_url_password(tmp_path: Path) -> None:
    service = _ln(tmp_path)
    address = "https://alice:tts-url-password@tts.example:8443"
    service.save_settings({"irodori_tts_url": address, "irodori_tts_api_key": "tts-secret-key-0123"})
    database = tmp_path / "library.sqlite3"
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    archive = tmp_path / "out" / "backup.zip"
    create_backup(
        config_path=config, database_path=database, cache_dir=tmp_path / "cache", output=archive, version="t"
    )
    _restore_on_this_mac(tmp_path, database, archive)
    assert _stored(database, "irodori_tts_url") == address
    assert _stored(database, "irodori_tts_api_key") == "tts-secret-key-0123"


def test_a_restored_different_tts_address_does_not_get_this_macs_password(tmp_path: Path) -> None:
    service = _ln(tmp_path)
    service.save_settings({"irodori_tts_url": "https://alice@other.example:9000"})
    database = tmp_path / "library.sqlite3"
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    archive = tmp_path / "out" / "backup.zip"
    create_backup(
        config_path=config, database_path=database, cache_dir=tmp_path / "cache", output=archive, version="t"
    )
    # This Mac now uses another server with a password; the backup names the old one.
    service.save_settings({"irodori_tts_url": "https://alice:tts-url-password@tts.example:8443"})
    _restore_on_this_mac(tmp_path, database, archive)
    assert _stored(database, "irodori_tts_url") == "https://alice@other.example:9000"
