"""SCEN-12: a full backup carries the iPhone identity (server secret, TLS key). Restored onto a
second Mac while the first one stays in use, both Macs had the same server ID: a phone could not
tell them apart, and since it names episodes by the Mac's row ID, a watched mark for episode N of
one Mac counted episode N of the other (AniList, cleanup). The restore now asks whether this Mac
replaces the backed-up one (identity and pairings come back, as before) or is a new Mac (it keeps
its own identity; the backup's iPhone data stays behind and phones pair with it anew)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from pudge.backup import create_backup, restore_backup
from pudge.database import Database
from pudge.iphone_service import IPhoneService


def _mac(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "library.sqlite3"
    Database(path).set_state("marker", name)
    return path


def _pair(service) -> tuple[str, str]:
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Phone",
    }
    service.pair(request)
    service.approve_pairing(request["deviceId"])
    return request["deviceId"], service.pair(request)["token"]


def _restore(archive: Path, database: Path, **kwargs) -> dict:
    return restore_backup(
        archive_path=archive,
        config_path=database.parent / "config.toml",
        database_path=database,
        cache_dir=database.parent / "cache",
        **kwargs,
    )


def _backed_up_mac(tmp_path: Path):
    first = _mac(tmp_path, "first")
    service = IPhoneService(Database(first), first.parent / "iphone")
    (first.parent / "iphone" / "tls-cert.pem").write_text("CERT")
    (first.parent / "iphone" / "tls-key.pem").write_text("KEY")
    device, token = _pair(service)
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_assets(id,book_id,manifest) VALUES(?,NULL,'{}')", (str(uuid.uuid4()),)
        )
    archive = tmp_path / "backup.zip"
    create_backup(
        config_path=first.parent / "config.toml",
        database_path=first,
        cache_dir=first.parent / "cache",
        output=archive,
        version="test",
    )
    return service, device, token, archive


def test_a_new_mac_keeps_its_own_identity_and_leaves_the_backups_iphone_data(tmp_path):
    first, _device, token, archive = _backed_up_mac(tmp_path)
    second = _mac(tmp_path, "second")
    own = IPhoneService(Database(second), second.parent / "iphone")
    own_device, own_token = _pair(own)

    result = _restore(archive, second, iphone_identity="new")

    assert result["restored_iphone_identity"] == 0 and result["iphone_identity"] == "new"
    restarted = IPhoneService(Database(second), second.parent / "iphone")
    assert restarted.server_id == own.server_id != first.server_id
    assert (
        not (second.parent / "iphone" / "tls-cert.pem").exists()
        or (second.parent / "iphone" / "tls-cert.pem").read_text() != "CERT"
    )
    # The other Mac's phone is not paired here; this Mac's own phone still is.
    with pytest.raises(PermissionError):
        restarted.authenticate(token)
    assert restarted.authenticate(own_token) == own_device
    with restarted.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_assets").fetchone()[0] == 0
    # The library itself came back.
    assert Database(second).get_state("marker", "") == "first"


def test_replacing_the_mac_brings_the_identity_and_pairings_back_as_before(tmp_path):
    first, device, token, archive = _backed_up_mac(tmp_path)
    second = _mac(tmp_path, "second")
    result = _restore(archive, second)  # the default: this Mac replaces the backed-up one
    assert result["iphone_identity"] == "replace" and result["restored_iphone_identity"] == 3
    moved = IPhoneService(Database(second), second.parent / "iphone")
    assert moved.server_id == first.server_id
    assert moved.authenticate(token) == device


def test_an_unknown_choice_is_refused_before_anything_changes(tmp_path):
    _first, _device, _token, archive = _backed_up_mac(tmp_path)
    second = _mac(tmp_path, "second")
    with pytest.raises(ValueError):
        _restore(archive, second, iphone_identity="both")
    assert Database(second).get_state("marker", "") == "second"


def test_the_window_asks_which_mac_the_backup_is_from(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('settings', true); await __tick(20);
            await __click('#restoreBackup'); await __tick(20);
            const options = [...document.querySelectorAll('#pudgeConfirmChoice option')].map(o => [o.value, o.textContent]);
            const label = __text('#pudgeConfirmChoiceLabel');
            document.getElementById('pudgeConfirmChoice').value = 'new';
            await __click('[data-pudge-confirm="ok"]'); await __tick(50);
            return {options, label, texts: [t('label.restoreIphoneIdentity'), t('option.restoreReplaceMac'), t('option.restoreNewMac')]};
            """,
            lang=lang,
            before="""
            __api.restore_full_backup = async () => ({ok: true, state: await __api.get_state()});
            """,
        )
        out = result.value
        assert out["label"] == out["texts"][0]
        assert out["options"] == [["replace", out["texts"][1]], ["new", out["texts"][2]]]
        assert result.calls_of("restore_full_backup") == [["new"]]
