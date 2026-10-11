"""Backup / restore safety: undo copies, daily copies, mpv helpers, iPhone access, manga imports, broken config."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
import tomllib
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

import pudge.backup as backup_module
import pudge.config as config_module
from pudge.backup import create_backup, restore_backup
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.i18n import err
from pudge.safe_mode import SafeModeController


def list_database_snapshots(database_path):
    return backup_module.list_database_snapshots(database_path)


def make_daily_snapshot(database_path, **kwargs):
    return backup_module.make_daily_snapshot(database_path, **kwargs)


def make_database_snapshot(database_path, kind):
    return backup_module.make_database_snapshot(database_path, kind)


def restore_database_snapshot(**kwargs):
    return backup_module.restore_database_snapshot(**kwargs)


@pytest.fixture(autouse=True)
def _forget_config_errors():
    yield
    getattr(config_module, "CONFIG_LOAD_ERROR", {}).clear()


def _live(tmp_path: Path, marker: str = "mine", name: str = "live") -> Path:
    folder = tmp_path / name
    folder.mkdir()
    database_path = folder / "library.sqlite3"
    Database(database_path).set_state("marker", marker)
    return database_path


def _backup(database_path: Path, output: Path) -> Path:
    create_backup(
        config_path=database_path.parent / "config.toml",
        database_path=database_path,
        cache_dir=database_path.parent / "cache",
        output=output,
        version="test",
    )
    return output


def _restore(archive: Path, database_path: Path) -> dict:
    return restore_backup(
        archive_path=archive,
        config_path=database_path.parent / "config.toml",
        database_path=database_path,
        cache_dir=database_path.parent / "cache",
    )


def _marker(database_path: Path) -> str:
    return Database(database_path).get_state("marker", "")


# ------------------------------------------------------------ 1. undo copies, daily copies


def test_restore_keeps_the_previous_library_and_can_be_undone(tmp_path: Path) -> None:
    live = _live(tmp_path)
    archive = _backup(_live(tmp_path, "from backup", "source"), tmp_path / "backup.zip")

    result = _restore(archive, live)

    assert _marker(live) == "from backup"
    copies = list_database_snapshots(live)
    assert [copy["kind"] for copy in copies] == ["pre-restore"]
    assert copies[0]["path"] == result["undo_snapshot"]
    restore_database_snapshot(snapshot_path=Path(copies[0]["path"]), database_path=live)
    assert _marker(live) == "mine"
    # The undo kept the library it replaced as well, so it can be undone again.
    assert len(list_database_snapshots(live)) == 2
    for _ in range(4):
        _restore(archive, live)
    assert [copy["kind"] for copy in list_database_snapshots(live)] == ["pre-restore"] * 3
    # The newest copy is the library just before the last restore.
    with closing(sqlite3.connect(list_database_snapshots(live)[0]["path"])) as conn:
        assert conn.execute("SELECT value FROM state WHERE key='marker'").fetchone()[0] == "from backup"
    with pytest.raises(ValueError, match="no longer exists"):
        restore_database_snapshot(snapshot_path=tmp_path / "elsewhere.backup", database_path=live)


def test_daily_copy_once_a_day_seven_kept_each_a_complete_database(tmp_path: Path) -> None:
    database_path = _live(tmp_path, "today")
    start = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
    made = [make_daily_snapshot(database_path, now=start + day * 86400) for day in range(9)]
    assert all(made)
    assert make_daily_snapshot(database_path, now=start + 8 * 86400 + 3600) is None
    copies = list_database_snapshots(database_path)
    assert len(copies) == 7 and copies[0]["path"] == str(made[-1])
    assert {copy["kind"] for copy in copies} == {"daily"}
    newest = Path(copies[0]["path"])
    with closing(sqlite3.connect(newest)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT value FROM state WHERE key='marker'").fetchone()[0] == "today"
    assert stat.S_IMODE(newest.stat().st_mode) == 0o600


def test_safe_mode_offers_the_newest_local_copy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", tmp_path / "app-session.json")
    database_path = _live(tmp_path)
    migration = database_path.with_name(database_path.name + ".pre-v13.backup")
    migration.write_bytes(database_path.read_bytes())
    os.utime(migration, (time.time() - 30 * 86400,) * 2)
    daily = make_daily_snapshot(database_path)

    status = SafeModeController(tmp_path / "cache", database_path).status(run_checks=False)

    assert status["latest_snapshot"]["path"] == str(daily)
    assert [copy["kind"] for copy in status["snapshots"]] == ["daily", "migration"]
    assert status["migration_backup"] == str(daily)


def test_a_damaged_library_can_be_replaced_by_its_last_good_copy(tmp_path: Path) -> None:
    database_path = _live(tmp_path, "good")
    with Database(database_path).connect() as conn:
        conn.executemany(
            "INSERT INTO state(key,value,updated_at) VALUES(?,?,0)",
            [(f"k{i}", "x" * 500) for i in range(400)],
        )
    good = make_daily_snapshot(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    size = database_path.stat().st_size
    with database_path.open("r+b") as handle:  # damage pages in the middle of the file
        handle.seek(size // 2)
        handle.write(os.urandom(16384))
    with closing(sqlite3.connect(database_path)) as conn:
        try:
            damaged = conn.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        except sqlite3.DatabaseError:
            damaged = True
    assert damaged

    restore_database_snapshot(snapshot_path=good, database_path=database_path)

    assert _marker(database_path) == "good"
    # The damaged file is kept aside but never offered as a copy to restore.
    assert [copy["kind"] for copy in list_database_snapshots(database_path)] == ["daily"]
    assert len(list(database_path.parent.glob("library.sqlite3.damaged-*.backup"))) == 1


def test_window_restore_and_its_undo_through_the_app(tmp_path: Path, monkeypatch) -> None:
    from test_v0730_database_retire import _restore_api

    api = _restore_api(tmp_path, monkeypatch)
    try:
        first = api.restore_full_backup()
        assert first["ok"] and api.manager.db.get_state("marker") == "restored"
        undone = api.restore_local_snapshot(first["undo_snapshot"])
        assert undone["ok"] and api.manager.db.get_state("marker") == "original"
        refused = api.restore_local_snapshot(str(tmp_path / "elsewhere.backup"))
        assert refused["ok"] is False and refused["error"] == err("backup.unknownSnapshot")
    finally:
        api.close()


# ------------------------------------------------------------ 2. mpv helpers during a restore


def test_restore_is_refused_while_a_player_is_open(tmp_path: Path, monkeypatch) -> None:
    from test_v0730_database_retire import _restore_api

    api = _restore_api(tmp_path, monkeypatch)
    api._play_lock = threading.RLock()
    api._active_playbacks_payload = lambda: [
        {"video_path": str(tmp_path / "Show 01.mkv"), "status": "running"}
    ]
    try:
        result = api.restore_full_backup()
        assert result["ok"] is False and result["busy"] is True
        assert result["error"] == err("backup.playbackActive") and result["workers"] == ["Show 01.mkv"]
        assert api.manager.db.get_state("marker") == "original"
    finally:
        api.close()


def test_restore_waits_for_player_helpers_and_helpers_wait_for_restore(tmp_path: Path, monkeypatch) -> None:
    from pudge.manager_models import LibraryAnime, LibraryEpisode
    from pudge.playback_save import main

    from pudge.restore_guard import database_use, exclusive_database

    live = _live(tmp_path)
    archive = _backup(_live(tmp_path, "from backup", "source"), tmp_path / "backup.zip")
    monkeypatch.setattr(backup_module, "RESTORE_LOCK_TIMEOUT", 0.3, raising=False)
    with database_use(live):  # an mpv helper is writing
        with pytest.raises(RuntimeError) as refused:
            _restore(archive, live)
    assert str(refused.value) == err("backup.helpersBusy")
    assert _marker(live) == "mine" and list_database_snapshots(live) == []

    cfg = AppConfig()
    cfg.library.database_path = live
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    config_path = tmp_path / "config.toml"
    write_config(cfg, config_path)
    video = tmp_path / "Show 01.mkv"
    video.write_bytes(b"video")
    db = Database(live)
    db.upsert_anime(LibraryAnime(7, "Show"))
    db.upsert_episode(LibraryEpisode(7, "Show", 1, video))
    results: list[int] = []
    args = ["--config", str(config_path), "--playback-video", str(video), "--playback-position", "30"]
    args += ["--playback-duration", "300", "--playback-active-seconds", "30"]
    with exclusive_database(live):  # a restore is swapping the file
        helper = threading.Thread(target=lambda: results.append(main(args)))
        helper.start()
        helper.join(0.6)
        assert helper.is_alive(), "the playback-save fallback wrote during a restore"
    helper.join(10)
    assert results == [0]
    assert Database(live).playback_evidence(video)["position"] == 30.0


# ------------------------------------------------------------ 3./4. iPhone access and identity


def _pair(service) -> tuple[str, str]:
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Phone",
    }
    service.pair(request)  # the Mac shows Allow / Deny first
    service.approve_pairing(request["deviceId"])
    return request["deviceId"], service.pair(request)["token"]


def test_restore_keeps_iphone_revocations_and_later_pairings(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService

    live = _live(tmp_path)
    service = IPhoneService(Database(live), live.parent / "iphone")
    revoked, revoked_token = _pair(service)
    archive = _backup(live, tmp_path / "backup.zip")
    service.revoke(revoked)
    later, later_token = _pair(service)

    _restore(archive, live)

    restarted = IPhoneService(Database(live), live.parent / "iphone")
    with pytest.raises(PermissionError):
        restarted.authenticate(revoked_token)
    assert restarted.authenticate(later_token) == later


def test_full_backup_moves_the_iphone_identity_to_a_new_mac(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService

    old_mac = _live(tmp_path, "old mac", "old-mac")
    service = IPhoneService(Database(old_mac), old_mac.parent / "iphone")
    (old_mac.parent / "iphone" / "tls-cert.pem").write_text("CERT")
    (old_mac.parent / "iphone" / "tls-key.pem").write_text("KEY")
    device, token = _pair(service)
    archive = _backup(old_mac, tmp_path / "backup.zip")
    with zipfile.ZipFile(archive) as backup:
        assert {"iphone/server-secret", "iphone/tls-cert.pem", "iphone/tls-key.pem"} <= set(backup.namelist())

    new_mac = _live(tmp_path, "new mac", "new-mac")
    assert IPhoneService(Database(new_mac), new_mac.parent / "iphone").server_id != service.server_id
    result = _restore(archive, new_mac)

    moved = IPhoneService(Database(new_mac), new_mac.parent / "iphone")
    assert result["restored_iphone_identity"] == 3
    assert moved.server_id == service.server_id
    assert (new_mac.parent / "iphone" / "tls-cert.pem").read_text() == "CERT"
    assert (new_mac.parent / "iphone" / "tls-key.pem").read_text() == "KEY"
    assert stat.S_IMODE((new_mac.parent / "iphone" / "tls-key.pem").stat().st_mode) == 0o600
    assert moved.authenticate(token) == device


def test_phone_position_after_restoring_an_older_library(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService
    from test_iphone_audio_repository import archived, checkpoint

    database_path = tmp_path / "library.sqlite3"
    service = IPhoneService(Database(database_path), tmp_path / "iphone")
    device, manifest = archived(service)
    asset = manifest["assetId"]
    assert (
        service.record_checkpoint(device, checkpoint(manifest, device, base=0, position=10000))["revision"]
        == 1
    )
    older = make_database_snapshot(database_path, "daily")
    for base, position in ((1, 20000), (2, 30000)):
        assert (
            service.record_checkpoint(device, checkpoint(manifest, device, base=base, position=position))[
                "status"
            ]
            == "accepted"
        )

    restore_database_snapshot(snapshot_path=older, database_path=database_path)
    restored = IPhoneService(Database(database_path), tmp_path / "iphone")
    assert restored.progress(asset)["revision"] == 1
    # The phone (at revision 3) goes on listening: its newer place is taken, not rejected.
    receipt = restored.record_checkpoint(device, checkpoint(manifest, device, base=3, position=40000))
    assert receipt["status"] == "accepted" and receipt["revision"] == 4
    assert restored.progress(asset)["selected"]["positionMs"] == 40000

    # Restored again, and the Mac plays after the restore: both places are offered.
    restore_database_snapshot(snapshot_path=older, database_path=database_path)
    again = IPhoneService(Database(database_path), tmp_path / "iphone")
    time.sleep(0.01)
    assert (
        again.record_checkpoint("mac", checkpoint(manifest, "mac", base=1, position=5000))["status"]
        == "accepted"
    )
    receipt = again.record_checkpoint(device, checkpoint(manifest, device, base=4, position=45000))
    assert receipt["status"] == "conflict" and receipt["revision"] == 5
    progress = again.progress(asset)
    assert progress["selected"]["positionMs"] == 5000
    assert [item["positionMs"] for item in progress["conflicts"]] == [45000]


# ------------------------------------------------------------ 5. manga imports


def _png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(path)
    return path


def test_imported_manga_lives_beside_the_library_and_old_imports_move(tmp_path: Path) -> None:
    from pudge.manga import MangaService

    database_path = tmp_path / "data" / "library.sqlite3"
    database_path.parent.mkdir()
    cache = tmp_path / "Caches"
    service = MangaService(Database(database_path), cache_dir=cache)
    book = service.import_images([_png(tmp_path / "drop" / "001.png")], title="Dropped")
    stored = Path(book["path"])
    assert stored.parent == (database_path.parent / "manga-imports").resolve() and stored.is_file()
    assert not (cache / "manga-imports").exists()

    # A volume packed by an earlier version in Caches, with its OCR fingerprint.
    legacy = cache / "manga-imports" / "Old volume-0123456789abcdef0123.cbz"
    legacy.parent.mkdir(parents=True)
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.write(_png(tmp_path / "old" / "001.png"), "0001__001.png")
    old_book = service.import_file(legacy)
    with service.db.connect() as conn:
        fingerprint = conn.execute(
            "SELECT source_fingerprint FROM manga_books WHERE id=?", (old_book["id"],)
        ).fetchone()[0]

    MangaService(Database(database_path), cache_dir=cache)

    moved = database_path.parent / "manga-imports" / legacy.name
    assert moved.is_file() and not legacy.exists() and not legacy.parent.exists()
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT path,source_fingerprint FROM manga_books WHERE id=?", (old_book["id"],)
        ).fetchone()
    assert row[0] == str(moved.resolve()) and row[1] == fingerprint
    assert service.remove_books([int(old_book["id"])]) == 1 and not moved.exists()


# ------------------------------------------------------------ 6. broken config.toml

BROKEN = '[ui]\nlanguage = "ru"\n\n[library]\nroot_dir = \n'


def test_broken_config_starts_with_defaults_and_keeps_a_copy(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")

    cfg = load_config(config_path, apply_language=False)

    assert config_module.CONFIG_LOAD_ERROR["line"] == 5 and config_module.CONFIG_LOAD_ERROR["path"] == str(
        config_path
    )
    assert config_module.CONFIG_LOAD_ERROR["message"]
    copy = Path(config_module.CONFIG_LOAD_ERROR["copy"])
    assert copy.name.startswith("config.toml.bad-") and copy.read_text(encoding="utf-8") == BROKEN
    assert config_path.read_text(encoding="utf-8") == BROKEN
    assert cfg.library.root_dir == AppConfig().library.root_dir
    # The user's language, and no first-run onboarding over their settings.
    assert cfg.ui.language == "ru" and cfg.ui.onboarding_completed is True
    load_config(config_path, apply_language=False)
    assert sorted(tmp_path.glob("config.toml.bad-*")) == [copy]
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(config_path, apply_language=False, recover_invalid=False)
    config_path.write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")
    load_config(config_path, apply_language=False)
    assert config_module.CONFIG_LOAD_ERROR == {}


def test_broken_config_puts_the_app_in_safe_mode_with_the_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", tmp_path / "app-session.json")
    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")
    load_config(config_path, apply_language=False)
    database_path = _live(tmp_path)
    controller = SafeModeController(
        tmp_path / "cache", database_path, config_error=dict(config_module.CONFIG_LOAD_ERROR)
    )

    assert controller.begin() is True
    status = controller.status(run_checks=False)
    assert status["reason"] == "config_invalid" and status["background_paused"] is True
    assert status["config_error"]["line"] == 5


def test_playback_helper_saves_with_default_settings_when_config_is_broken(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge.manager_models import LibraryAnime, LibraryEpisode
    from pudge.playback_save import main

    database_path = tmp_path / "data" / "library.sqlite3"
    monkeypatch.setattr("pudge.config.DEFAULT_DATABASE_PATH", database_path)
    monkeypatch.setattr("pudge.config.DEFAULT_LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr("pudge.config.DEFAULT_CACHE_DIR", tmp_path / "cache")
    database_path.parent.mkdir()
    db = Database(database_path)
    video = tmp_path / "Show 01.mkv"
    video.write_bytes(b"video")
    db.upsert_anime(LibraryAnime(7, "Show"))
    db.upsert_episode(LibraryEpisode(7, "Show", 1, video))
    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")

    code = main(["--config", str(config_path), "--playback-video", str(video), "--playback-position", "42"])

    assert code == 0 and db.playback_evidence(video)["position"] == 42.0


def test_background_agent_skips_instead_of_crashing_on_a_broken_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from pudge import agent

    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")
    monkeypatch.setattr(agent, "AnimeManager", lambda *a, **k: pytest.fail("no work on default settings"))

    assert agent.main(["--config", str(config_path)]) == 0
    assert agent.main(["--config", str(config_path), "--scheduled"]) == 0
    assert "line 5" in capsys.readouterr().err


# ------------------------------------------------------------ window: Settings and Safe Mode


SNAPSHOTS = [
    {
        "path": "/data/library.sqlite3.pre-restore-20261010-120000.backup",
        "kind": "pre-restore",
        "created_at": 1791800000,
        "size": 9,
    },
    {
        "path": "/data/library.sqlite3.daily-20261009-120000.backup",
        "kind": "daily",
        "created_at": 1791700000,
        "size": 9,
    },
]


def test_settings_restore_a_local_copy_after_confirmation(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('settings', true); await __tick(20);
            const options = [...document.querySelectorAll('#localCopySelect option')].map(option => option.textContent);
            const inMaintenance = !!$('restoreLocalCopy').closest('#settingsMaintenance');
            $('localCopySelect').value = '/data/library.sqlite3.daily-20261009-120000.backup';
            await __click('#restoreLocalCopy'); await __tick(20);
            const question = __text('#pudgeConfirmMessage');
            await __click('[data-pudge-confirm="cancel"]'); await __tick(20);
            const afterCancel = __apiCalls.filter(call => call.name === 'restore_local_snapshot').length;
            await __click('#restoreLocalCopy'); await __tick(20);
            await __click('[data-pudge-confirm="ok"]'); await __tick(50);
            const date = localCopyDate(ui.state.safe_mode.snapshots[1]);
            return {options, inMaintenance, question, afterCancel, toast: __toasts().at(-1),
                    texts: {question: t('confirm.restoreLocalCopy', {date}), toast: t('toast.localCopyRestored', {date}),
                            kinds: [t('label.localCopyBeforeRestore'), t('label.localCopyDaily')]}};
            """,
            lang=lang,
            state={"safe_mode": {"active": False, "snapshots": SNAPSHOTS}},
            before="__api.restore_local_snapshot = async () => ({ok: true, state: await __api.get_state()});",
        )
        out, texts = result.value, result.value["texts"]
        assert out["inMaintenance"] is True
        assert [option.split(" · ")[0] for option in out["options"]] == texts["kinds"]
        assert out["question"] == texts["question"] and out["afterCancel"] == 0
        assert result.calls_of("restore_local_snapshot") == [[SNAPSHOTS[1]["path"]]]
        assert out["toast"] == texts["toast"]


def test_safe_mode_banner_names_the_broken_line_and_restores_the_newest_copy(page) -> None:
    error = {"path": "/c/config.toml", "line": 5, "message": "Invalid value", "copy": "/c/config.toml.bad-1"}
    result = page.run(
        r"""
        await __tick(20);
        const banner = __text('#safeModeBanner');
        await __click('[data-action="safe-mode-restore-copy"]'); await __tick(20);
        await __click('[data-pudge-confirm="ok"]'); await __tick(50);
        return {banner, broken: t('ui.safeModeConfigBroken', {line: 5, message: 'Invalid value', copy: '/c/config.toml.bad-1'}),
                latest: t('ui.safeModeLatestCopy', {copy: localCopyLabel(ui.state.safe_mode.latest_snapshot)})};
        """,
        lang="ru",
        state={
            "safe_mode": {
                "active": True,
                "config_error": error,
                "latest_snapshot": SNAPSHOTS[0],
                "snapshots": SNAPSHOTS,
            }
        },
        before="__api.restore_local_snapshot = async () => ({ok: true, state: await __api.get_state()});",
    )
    assert result.value["broken"] in result.value["banner"]
    assert result.value["latest"] in result.value["banner"]
    assert result.calls_of("restore_local_snapshot") == [[SNAPSHOTS[0]["path"]]]


def test_copies_made_in_one_second_stay_newest_first_after_pruning(tmp_path: Path) -> None:
    # Restores in quick succession: pruning frees a same-second name, which must not be reused
    # (the newest copy would then sort as the oldest and survive pruning as a 4th copy).
    from pudge.backup import list_database_snapshots, make_database_snapshot

    live = _live(tmp_path)
    made = [make_database_snapshot(live, "pre-restore", now=1_800_000_000.0) for _ in range(6)]
    copies = [Path(copy["path"]) for copy in list_database_snapshots(live)]
    assert copies == made[::-1][:3]
