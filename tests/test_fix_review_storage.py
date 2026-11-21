"""Storage, backup and update fixes from the code review.

1. A Home snapshot held the SQLite write lock until it finished. Inside
   connection_scope() no repository call committed until the scope ended, and
   personal_release_schedule() always ran an UPDATE, even when nothing was due.
   With an enabled personal schedule every get_state held the lock for the rest
   of the snapshot and mpv progress saves, subtitle workers and the agent waited
   (up to 30 s, then "database is locked"). Now each call in a shared scope
   commits on its own (a scope that begins its own transaction stays atomic),
   and the schedule only writes when an episode is due.
2. The snapshot integrity check built its SQLite URI without escaping: a folder
   named with "?" or "#" checked (and created) a different empty file, and
   "%41" made every check fail. The path is escaped now (Safe Mode too).
3. The full backup holds the iPhone identity (server secret, TLS private key)
   on purpose, so a restored Mac stays paired, but it was written 0644 and its
   manifest said secrets_included: false. It is written 0600 and says so now.
4. Secret files (config.toml and its temp file, the broken-config copy, the
   Jiten/JPDB plugin configs, restored iPhone keys) were briefly world-readable
   while written: created 0644, chmod-ed afterwards. They are created 0600.
5. A failed update step (dev-channel git merge, the rollback copy) ended the
   generated update script under set -e after the app had been told to quit,
   and the app never came back. An exit trap now logs the step and reopens it.
6. A failed update rolled back Pudge but not the third-party packages it had
   upgraded in the live venv; a config the brand rewrite cannot parse failed
   the update only after that upgrade; a kill (TERM, a closed Terminal) skipped
   the rollback entirely, and after a SIGKILL between removing the package and
   installing the new one the next run backed up the broken state. Now the
   config is checked before the venv is touched, the dependencies are restored
   from a name==version list, signals exit through the rollback, and a finished
   backup left by a killed update stays the rollback source.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pudge import backup, config, first_experience
from pudge.backup import create_backup, make_database_snapshot, restore_backup
from pudge.database import Database
from pudge.manager_models import LibraryAnime
from pudge.safe_mode import SafeModeController
from pudge.updater import AppUpdater
from t71g00_support import build_wheel, clone_install, mac_env, run_install, set_version


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# ------------------------------------------------ 1. connection_scope and the write lock


def test_a_call_inside_a_shared_scope_commits_at_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    other = sqlite3.connect(tmp_path / "library.sqlite3", timeout=0.2)
    try:
        with db.connection_scope():
            with db.connection_scope():  # get_state nests two scopes
                db.set_state("seen", "1")
                # Committed: another connection sees it and can write meanwhile.
                assert other.execute("SELECT value FROM state WHERE key='seen'").fetchone() == ("1",)
                other.execute("INSERT INTO state(key,value,updated_at) VALUES('other','x',0)")
                other.commit()
            # A failing call is rolled back on its own, like on a fresh connection.
            with pytest.raises(RuntimeError):
                with db.connect() as conn:
                    conn.execute("INSERT INTO state(key,value,updated_at) VALUES('failed','x',0)")
                    raise RuntimeError("boom")
            assert db.get_state("failed", "") == ""
            assert db.get_state("other", "") == "x"
    finally:
        other.close()


def test_a_scope_that_begins_its_own_transaction_stays_atomic(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    other = sqlite3.connect(tmp_path / "library.sqlite3", timeout=0.2)
    try:
        with pytest.raises(RuntimeError):
            with db.connection_scope() as conn:
                conn.execute("BEGIN IMMEDIATE")
                db.set_state("a", "1")
                with pytest.raises(sqlite3.OperationalError):
                    other.execute("INSERT INTO state(key,value,updated_at) VALUES('x','y',0)")
                other.rollback()
                db.set_state("b", "2")
                raise RuntimeError("boom")
        assert (db.get_state("a", ""), db.get_state("b", "")) == ("", "")
        with db.connection_scope() as conn:
            conn.execute("BEGIN IMMEDIATE")
            db.set_state("a", "1")
            assert other.execute("SELECT value FROM state WHERE key='a'").fetchone() is None
        assert other.execute("SELECT value FROM state WHERE key='a'").fetchone() == ("1",)
    finally:
        other.close()


def test_reading_a_personal_schedule_takes_no_write_lock_when_nothing_is_due(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    db.upsert_anime(LibraryAnime(media_id=42, title="Show", episodes=12))
    schedule = db.save_personal_release_schedule(
        42,
        episodes=[1, 2, 3],
        start_local_datetime="2026-09-14T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    first = schedule.items[0].unlock_at_utc
    other = sqlite3.connect(path, timeout=0.2)
    try:
        with db.connection_scope() as conn:
            conn.execute("BEGIN")  # deferred: only a write takes the lock
            before = db.personal_release_schedule(42, now=first - 60)
            assert before is not None and before.items[0].unlocked is False
            other.execute("INSERT INTO state(key,value,updated_at) VALUES('mpv','saved',0)")
            other.commit()
            conn.rollback()
    finally:
        other.close()
    due = db.personal_release_schedule(42, now=first)
    assert due is not None and due.items[0].unlocked is True


# ------------------------------------------------ 2. escaped SQLite URIs


@pytest.mark.parametrize("name", ["Lib #2", "Lib ?x", "Lib %41"])
def test_integrity_checks_read_the_file_in_any_folder_name(tmp_path: Path, name: str) -> None:
    folder = tmp_path / name
    folder.mkdir()
    database_path = folder / "library.sqlite3"
    Database(database_path).set_state("marker", "mine")
    snapshot = make_database_snapshot(database_path, "daily")
    assert snapshot.is_file()
    assert [path.name for path in tmp_path.iterdir()] == [name]  # no stray empty database
    # A damaged library in such a folder is reported, not some other empty file.
    damaged = folder / "damaged.sqlite3"
    damaged.write_bytes(b"not a database" * 512)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        backup._check_database_file(damaged)
    assert SafeModeController(tmp_path / "cache", database_path)._database_check() == "ok"
    assert SafeModeController(tmp_path / "cache", damaged)._database_check() != "ok"
    assert [path.name for path in tmp_path.iterdir()] == [name]


# ------------------------------------------------ 3. the full backup holds the iPhone identity


def _mac_with_identity(folder: Path) -> Path:
    folder.mkdir(parents=True)
    database_path = folder / "library.sqlite3"
    Database(database_path).set_state("marker", folder.name)
    (folder / "iphone").mkdir()
    (folder / "iphone" / "server-secret").write_bytes(os.urandom(32))
    (folder / "iphone" / "tls-key.pem").write_text("-----BEGIN PRIVATE KEY-----\nKEY\n")
    (folder / "iphone" / "tls-cert.pem").write_text("CERT")
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


def test_full_backup_is_private_and_says_it_carries_secrets(tmp_path: Path, umask_022) -> None:
    archive = _backup(_mac_with_identity(tmp_path / "mac"), tmp_path / "out" / "backup.zip")
    assert _mode(archive) == 0o600
    assert [path.name for path in archive.parent.iterdir()] == ["backup.zip"]  # no temp file left
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert "iphone/tls-key.pem" in bundle.namelist()
    assert manifest["secrets_included"] is True
    assert sorted(manifest["iphone_identity"]) == ["server-secret", "tls-cert.pem", "tls-key.pem"]
    # Without an iPhone identity there is no secret in it.
    plain = tmp_path / "plain"
    plain.mkdir()
    Database(plain / "library.sqlite3")
    with zipfile.ZipFile(_backup(plain / "library.sqlite3", tmp_path / "plain.zip")) as bundle:
        assert json.loads(bundle.read("manifest.json"))["secrets_included"] is False


# ------------------------------------------------ 4. secret files are 0600 from creation


def test_secret_files_are_private_from_creation(tmp_path: Path, monkeypatch, umask_022) -> None:
    # With chmod switched off, a file created 0644 and fixed afterwards stays 0644.
    monkeypatch.setattr(os, "chmod", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: None)
    destination = tmp_path / "pudge" / "config.toml"
    config._atomic_config_text_write(destination, '[jimaku]\napi_key = "secret"\n')
    assert _mode(destination) == 0o600
    assert destination.read_text(encoding="utf-8") == '[jimaku]\napi_key = "secret"\n'
    copy = config._keep_broken_config_copy(destination, b'[jimaku\napi_key = "secret"\n')
    assert copy is not None and _mode(copy) == 0o600
    monkeypatch.setenv("HOME", str(tmp_path))
    jiten = tmp_path / ".config" / "jiten-mpv" / "config.json"
    jiten.parent.mkdir(parents=True)
    jiten.write_text('{"base_url":"https://jiten.moe"}', encoding="utf-8")
    os.chmod(jiten, 0o644)
    first_experience._write_jiten_api_key("secret-key")
    assert _mode(jiten) == 0o600
    assert json.loads(jiten.read_text(encoding="utf-8"))["api_key"] == "secret-key"
    assert sorted(path.name for path in jiten.parent.iterdir()) == ["config.json"]


def test_restored_iphone_keys_are_never_world_readable(tmp_path: Path, monkeypatch, umask_022) -> None:
    archive = _backup(_mac_with_identity(tmp_path / "old mac"), tmp_path / "backup.zip")
    new_mac = tmp_path / "new mac"
    new_mac.mkdir()
    Database(new_mac / "library.sqlite3").set_state("marker", "new")
    renamed: dict[str, int] = {}
    real_replace = os.replace

    def recording_replace(source, target, *args, **kwargs):
        if Path(target).parent.name == "iphone":
            renamed[Path(target).name] = _mode(Path(source))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", recording_replace)
    restore_backup(
        archive_path=archive,
        config_path=new_mac / "config.toml",
        database_path=new_mac / "library.sqlite3",
        cache_dir=new_mac / "cache",
    )
    # Already 0600 when it appears under its real name.
    assert renamed == {"server-secret": 0o600, "tls-cert.pem": 0o600, "tls-key.pem": 0o600}
    assert _mode(new_mac / "iphone" / "tls-key.pem") == 0o600


# ------------------------------------------------ 5. a failed update step reopens the app


def _generated_update(tmp_path: Path, monkeypatch, prefix: list[str], source) -> Path:
    home = tmp_path / "home"
    (home / "Applications/pudge.app").mkdir(parents=True)
    project = tmp_path / "source"
    project.mkdir()
    (project / "install.sh").write_text(
        f"#!/bin/zsh\ntouch '{tmp_path / 'installed'}'\nexit ${{INSTALL_STATUS:-0}}\n"
    )
    updater = AppUpdater(before_install=lambda: None, request_quit=source.terminate)
    updater.update_root = tmp_path / "updates"
    updater.log_path = tmp_path / "update.log"
    with monkeypatch.context() as patch:
        patch.setattr("pudge.updater.Path.home", lambda: home)
        patch.setattr("pudge.updater.os.getpid", lambda: source.pid)
        patch.setattr("pudge.updater.subprocess.Popen", lambda *a, **k: None)
        updater._launch_script(project, prefix)
    return updater.update_root / "run-pudge-update.zsh"


def _run_update(tmp_path: Path, script: Path, *, ditto_status: int = 0, install_status: int = 0):
    opened = tmp_path / "opened"
    opener = tmp_path / "open"
    opener.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{opened}'\n")
    opener.chmod(0o700)
    copier = tmp_path / "ditto"
    copier.write_text(f'#!/bin/sh\n[ {ditto_status} = 0 ] || exit {ditto_status}\ncp -R "$1" "$2"\n')
    copier.chmod(0o700)
    standins = (
        f"/usr/bin/open() {{ '{opener}' \"$@\"; }}\n/usr/bin/ditto() {{ '{copier}' \"$@\"; }}\n"
        "/bin/sleep() { :; }\n"
    )
    env = mac_env(
        tmp_path / "zdot",
        standins,
        root=tmp_path / "root",
        home=tmp_path / "home",
        path="/usr/bin:/bin",
        INSTALL_STATUS=str(install_status),
    )
    result = subprocess.run(["/bin/zsh", str(script)], timeout=30, env=env)
    lines = opened.read_text().splitlines() if opened.exists() else []
    return result, lines, (tmp_path / "update.log").read_text(encoding="utf-8")


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the generated macOS updater runs under /bin/zsh")
@pytest.mark.parametrize("failing", ["merge", "rollback copy", "install.sh"])
def test_a_failed_update_step_logs_it_and_reopens_the_app(tmp_path, monkeypatch, failing) -> None:
    source = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        prefix = ["/bin/sh", "-c", "exit 7"] if failing == "merge" else []
        script = _generated_update(tmp_path, monkeypatch, prefix, source)
        source.wait(timeout=5)  # the app quits as asked
        text = script.read_text(encoding="utf-8")
        assert "trap finish_update EXIT" in text
        for shell in ("bash", "zsh"):
            if shutil.which(shell):
                assert subprocess.run([shell, "-n", str(script)]).returncode == 0, shell
        result, opened, log = _run_update(
            tmp_path,
            script,
            ditto_status=3 if failing == "rollback copy" else 0,
            install_status=1 if failing == "install.sh" else 0,
        )
    finally:
        if source.poll() is None:
            source.kill()
        source.wait(timeout=5)
    assert result.returncode != 0
    assert opened == [
        f"-n {tmp_path / 'home/Applications/pudge.app'} --args --pudge-after-update"
    ]  # reopened exactly once
    if failing == "install.sh":
        assert (tmp_path / "installed").exists()
        assert "Updater error: step" not in log  # its own rollback path already reopened the app
    else:
        assert not (tmp_path / "installed").exists()
        status = 7 if failing == "merge" else 3
        assert f"failed (exit {status}); reopening pudge." in log
        assert ("exit 7" if failing == "merge" else "copy of the current app for rollback") in log


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the generated macOS updater runs under /bin/zsh")
def test_a_successful_update_still_reopens_once(tmp_path, monkeypatch) -> None:
    source = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        script = _generated_update(tmp_path, monkeypatch, ["/bin/sh", "-c", "exit 0"], source)
        source.wait(timeout=5)
        result, opened, log = _run_update(tmp_path, script)
    finally:
        if source.poll() is None:
            source.kill()
        source.wait(timeout=5)
    assert result.returncode == 0 and (tmp_path / "installed").exists()
    assert len(opened) == 1 and "Updater error" not in log


# ------------------------------------------------ 6. install.sh rolls back dependencies too

# A pip for the update runs: the wheel's [sync] install upgrades demo_dep to 2.0, the rollback
# (-r requirements.txt) puts the listed version back; REVIEW_PIP_MODE=stale breaks the installed
# package (the byte check fails), =term sends SIGTERM to install.sh during the wheel install.
REVIEW_PIP = r"""
import os, shutil, signal, sys, sysconfig, zipfile
from pathlib import Path

args = sys.argv[1:]
site = Path(sysconfig.get_paths()["purelib"])
mode = os.environ.get("REVIEW_PIP_MODE", "")


def set_dep(version):
    for old in site.glob("demo_dep-*.dist-info"):
        shutil.rmtree(old)
    info = site / f"demo_dep-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: demo_dep\nVersion: {version}\n")


if args and args[0] == "install":
    target = args[-1]
    if "-r" in args:
        for line in Path(args[args.index("-r") + 1]).read_text().splitlines():
            name, _, version = line.partition("==")
            if name == "demo_dep":
                set_dep(version)
    elif target.endswith(".whl") and "--no-deps" in args:
        if mode == "term":
            os.kill(os.getppid(), signal.SIGTERM)
        # pip --root ROOT (the installer's staging): the same paths under ROOT.
        into = Path(args[args.index("--root") + 1] + str(site)) if "--root" in args else site
        zipfile.ZipFile(target).extractall(into)
        if mode == "stale":
            (into / "pudge" / "__init__.py").write_text("broken = True\n")
    elif target.split("[", 1)[0].endswith(".whl"):
        set_dep("2.0")
"""


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """A first install of release 9.8.7 (run_install's stand-ins)."""
    work = tmp_path_factory.mktemp("installed")
    result = run_install(work)
    assert result.returncode == 0, result.output
    return result


def _update_of(installed, tmp_path: Path):
    work = clone_install(installed.home.parent, tmp_path / "install")
    set_version(work / "project", "9.8.8")
    build_wheel(work / "project", "9.8.8")
    (work / "stubs" / "fake_pip.py").write_text(REVIEW_PIP, encoding="utf-8")
    site = next((work / "home/.local/share/pudge/venv/lib").glob("python*/site-packages"))
    info = site / "demo_dep-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo_dep\nVersion: 1.0\n")
    return work, site


def _pudge_version(site: Path) -> str:
    return (site / "pudge" / "__init__.py").read_text(encoding="utf-8").split("\n", 1)[0]


def _deps(site: Path) -> list[str]:
    return sorted(path.name for path in site.glob("demo_dep-*.dist-info"))


def test_a_failed_update_restores_the_previous_dependencies(installed, tmp_path: Path) -> None:
    work, site = _update_of(installed, tmp_path)
    result = run_install(work, update=True, env={"REVIEW_PIP_MODE": "stale"})
    assert result.returncode != 0
    assert "Update failed; restoring the previous Pudge package..." in result.output
    assert "Update failed; restoring the previous Python dependencies..." in result.output
    assert _pudge_version(site) == "__version__ = '9.8.7'"
    assert _deps(site) == ["demo_dep-1.0.dist-info"]
    assert not (work / "home/.local/share/pudge/update-package-backup").exists()


def test_a_config_the_rewrite_cannot_parse_stops_the_update_before_the_venv(installed, tmp_path) -> None:
    work, site = _update_of(installed, tmp_path)
    (work / "home/.config/pudge/config.toml").write_text("[jimaku\n", encoding="utf-8")
    result = run_install(work, update=True)
    assert result.returncode != 0
    assert not [call for call in result.calls if call[:3] == ["venv-python", "-m", "pip"]]
    assert _pudge_version(site) == "__version__ = '9.8.7'"
    assert _deps(site) == ["demo_dep-1.0.dist-info"]


def test_a_killed_update_still_rolls_back(installed, tmp_path: Path) -> None:
    work, site = _update_of(installed, tmp_path)
    result = run_install(work, update=True, env={"REVIEW_PIP_MODE": "term"})
    assert result.returncode == 143
    assert "Update failed; restoring the previous Pudge package..." in result.output
    assert _pudge_version(site) == "__version__ = '9.8.7'"
    assert _deps(site) == ["demo_dep-1.0.dist-info"]


def test_the_backup_of_an_update_killed_mid_install_stays_the_rollback_source(installed, tmp_path) -> None:
    work, site = _update_of(installed, tmp_path)
    # A SIGKILL right after the old package was removed: no EXIT trap ran.
    leftover = work / "home/.local/share/pudge/update-package-backup"
    leftover.mkdir()
    shutil.copytree(site / "pudge", leftover / "pudge")
    for info in site.glob("pudge-*.dist-info"):
        shutil.copytree(info, leftover / info.name)
        shutil.rmtree(info)
    shutil.rmtree(site / "pudge")
    (leftover / "requirements.txt").write_text("demo_dep==1.0\n", encoding="utf-8")
    (leftover / "complete").write_text("", encoding="utf-8")
    result = run_install(work, update=True, env={"REVIEW_PIP_MODE": "stale"})
    assert result.returncode != 0
    assert "Resuming after an interrupted update" in result.output
    assert _pudge_version(site) == "__version__ = '9.8.7'"
    assert sorted(path.name for path in site.glob("pudge-*.dist-info")) == ["pudge-9.8.7.dist-info"]
    assert _deps(site) == ["demo_dep-1.0.dist-info"]
    # The next update starts from a clean backup again.
    assert not leftover.exists()
    result = run_install(work, update=True)
    assert result.returncode == 0, result.output
    assert _pudge_version(site) == "__version__ = '9.8.8'"
