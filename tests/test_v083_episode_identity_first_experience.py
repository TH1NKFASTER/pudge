from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.first_experience import (
    _write_jiten_api_key,
    _write_jpdb_api_token,
    dependency_status,
    mpv_study_script_plan,
    mpv_study_status,
)
from pudge.manager_models import DownloadItem, LibraryEpisode


def test_schema_v5_backfills_separate_episode_identity(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    assert LATEST_SCHEMA_VERSION == 7
    with db.connect() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
        }
    assert {"episode", "media_episode", "release_episode"} <= columns


def test_v5_migration_repairs_absolute_episode_from_managed_download(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA user_version=4;
            CREATE TABLE anime(media_id INTEGER PRIMARY KEY);
            CREATE TABLE episodes(
                id INTEGER PRIMARY KEY, media_id INTEGER, episode INTEGER,
                torrent_hash TEXT, video_path TEXT
            );
            CREATE TABLE downloads(
                torrent_hash TEXT PRIMARY KEY, media_id INTEGER, episode INTEGER
            );
            CREATE TABLE release_history(
                info_hash TEXT PRIMARY KEY, media_id INTEGER, episode INTEGER
            );
            INSERT INTO episodes(media_id,episode,torrent_hash,video_path)
            VALUES(189046,78,'hash78','/tmp/ReZero-78.mkv');
            INSERT INTO downloads(torrent_hash,media_id,episode)
            VALUES('hash78',189046,12);
            """
        )
        # Exercise the migration directly: the full Database schema has many
        # unrelated legacy columns that are irrelevant to this repair.
        db = object.__new__(Database)
        db._migrate_v5(conn)
        row = conn.execute(
            "SELECT episode,media_episode,release_episode FROM episodes"
        ).fetchone()
    assert row == (12, 12, 78)


def test_episode_and_download_round_trip_both_numbers(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "ReZero - 78.mkv"
    db.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Re:Zero 4",
            episode=12,
            media_episode=12,
            release_episode=78,
            video_path=video,
        )
    )
    db.upsert_download(
        DownloadItem(
            torrent_hash="abc",
            name="ReZero - 78",
            state="complete",
            progress=1,
            save_path=str(tmp_path),
            content_path=str(video),
            episode=12,
            media_episode=12,
            release_episode=78,
        )
    )
    episode = db.episode_by_path(video)
    download = db.download_by_hash("abc")
    assert episode is not None and download is not None
    assert (episode.episode, episode.media_episode, episode.release_episode) == (12, 12, 78)
    assert (download.episode, download.media_episode, download.release_episode) == (12, 12, 78)
    assert db.ready_episode(0, 78) is None


def test_release_history_keeps_media_and_release_numbers(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.record_release(
        "hash78", 189046, 12, "ReZero - 78", 100.0, release_episode=78
    )
    assert db.release_metadata_by_hash("HASH78") == (189046, 12, 78, 100.0)


def test_jiten_key_writer_preserves_config_and_is_private(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config" / "jiten-mpv" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"base_url":"https://jiten.moe"}', encoding="utf-8")
    _write_jiten_api_key("secret-key")
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload == {"base_url": "https://jiten.moe", "api_key": "secret-key"}
    assert config.stat().st_mode & 0o777 == 0o600


def test_jpdb_token_writer_preserves_config_and_is_private(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin = tmp_path / ".config" / "mpv" / "scripts" / "jpdb-mpv-plugin"
    plugin.mkdir(parents=True)
    config = plugin / "config.json"
    config.write_text('{"serverPort":9730}', encoding="utf-8")
    _write_jpdb_api_token("jpdb-secret")
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["apiToken"] == "jpdb-secret"
    assert payload["serverPort"] == 9730
    assert config.stat().st_mode & 0o777 == 0o600


def test_mpv_plugins_use_jiten_key_and_jpdb_managed_auth(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    scripts = tmp_path / ".config" / "mpv" / "scripts"
    (tmp_path / ".local" / "share" / "jiten-mpv").mkdir(parents=True)
    scripts.mkdir(parents=True)
    (scripts / "jiten-mpv.lua").write_text("-- jiten", encoding="utf-8")
    jpdb = scripts / "jpdb-mpv-plugin"
    jpdb.mkdir()
    (jpdb / "main.lua").write_text("-- jpdb", encoding="utf-8")
    server = jpdb / "jpdb-server"
    server.write_text("binary", encoding="utf-8")
    server.chmod(0o755)

    status = mpv_study_status(jiten_api_key="jiten-key", selected_plugin="jiten")
    assert status["jiten_mpv"]["available"] is True
    assert status["jpdb_mpv"]["installed"] is True
    assert status["jpdb_mpv"]["available"] is True
    assert status["jpdb_mpv"]["auth_managed_by_plugin"] is True
    assert status["mpv_study"]["effective"] == "jiten"


def test_mpv_script_plan_keeps_other_scripts_and_loads_only_selected_plugin(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    scripts = tmp_path / ".config" / "mpv" / "scripts"
    (tmp_path / ".local" / "share" / "jiten-mpv").mkdir(parents=True)
    scripts.mkdir(parents=True)
    jiten = scripts / "jiten-mpv.lua"
    jiten.write_text("-- jiten", encoding="utf-8")
    ordinary = scripts / "ordinary.lua"
    ordinary.write_text("-- ordinary", encoding="utf-8")
    jpdb = scripts / "jpdb-mpv-plugin"
    jpdb.mkdir()
    jpdb_main = jpdb / "main.lua"
    jpdb_main.write_text("-- jpdb", encoding="utf-8")
    server = jpdb / "jpdb-server"
    server.write_text("binary", encoding="utf-8")
    server.chmod(0o755)

    plan = mpv_study_script_plan(
        "jpdb", jiten_api_key="jiten-key", jpdb_api_token="jpdb-key"
    )
    assert plan["exclusive"] is True
    assert plan["selected"] == "jpdb"
    assert str(ordinary) in plan["scripts"]
    assert str(jpdb_main) in plan["scripts"]
    assert str(jiten) not in plan["scripts"]


def test_dependency_status_accepts_configured_absolute_tools(tmp_path: Path) -> None:
    mpv = tmp_path / "mpv"
    ffmpeg = tmp_path / "ffmpeg"
    mpv.write_text("#!/bin/sh\necho 'mpv test'\n", encoding="utf-8")
    ffmpeg.write_text("#!/bin/sh\necho 'ffmpeg test'\n", encoding="utf-8")
    mpv.chmod(0o755)
    ffmpeg.chmod(0o755)
    status = dependency_status(mpv=str(mpv), ffmpeg=str(ffmpeg))
    assert status["mpv"]["installed"] is True
    assert status["ffmpeg"]["installed"] is True


def test_jpdb_detection_accepts_macos_and_versioned_installer_layout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin = (
        tmp_path / "Library" / "Application Support" / "mpv" / "scripts"
        / "jpdb-player"
    )
    plugin.mkdir(parents=True)
    script = plugin / "jpdb.lua"
    script.write_text("-- jpdb plugin", encoding="utf-8")
    server = plugin / "jpdb-mpv-plugin-0.11.0"
    server.write_text("binary", encoding="utf-8")
    server.chmod(0o755)

    status = mpv_study_status(selected_plugin="jpdb")

    assert status["jpdb_mpv"]["installed"] is True
    assert status["jpdb_mpv"]["available"] is True
    assert status["jpdb_mpv"]["key_configured"] is False
    assert status["jpdb_mpv"]["auth_managed_by_plugin"] is True
    assert status["jpdb_mpv"]["script_path"] == str(script)
    assert str(script) in status["jpdb_mpv"]["detected_variants"]
    assert status["mpv_study"]["effective"] == "jpdb"


def test_jpdb_detection_reads_generic_installer_generated_main_lua(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    scripts = tmp_path / ".config" / "mpv" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "main.lua"
    script.write_text("-- official player integration\nlocal endpoint = 'https://jpdb.io'", encoding="utf-8")

    status = mpv_study_status(selected_plugin="jpdb")

    assert status["jpdb_mpv"]["installed"] is True
    assert status["jpdb_mpv"]["available"] is True
    assert status["jpdb_mpv"]["script_path"] == str(script)
    assert status["mpv_study"]["effective"] == "jpdb"


def test_onboarding_exposes_dependency_and_jiten_mpv_flow() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "first_experience_dependencies" in html
    assert "install_first_experience_dependencies" in html
    assert "install_jiten_mpv" in html
    assert "JitenMPV" in html
