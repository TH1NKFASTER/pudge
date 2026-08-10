from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.audiobooks import AudiobookService
from pudge.backup import create_backup, restore_backup
from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.manga import MangaService
from pudge.subtitles.selection import upgrade_is_better
from pudge.subtitles.stt import prepare_japanese_stt_reference
from pudge.subtitles.video_segments import choose_edit_boundary


def test_quality_upgrade_never_uses_filename_score() -> None:
    previous = {"accepted": True, "confidence": "B", "score": 72.0}
    better = {"accepted": True, "confidence": "A", "score": 73.0, "filename_score": -1000}
    worse = {"accepted": True, "confidence": "C", "score": 99.0, "filename_score": 9999}
    assert upgrade_is_better(previous, better, minimum_gain=10)[0] is True
    assert upgrade_is_better(previous, worse, minimum_gain=10)[0] is False


def test_versioned_database_migration_and_job_state(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    with db.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subtitle_jobs)")}
    assert version == LATEST_SCHEMA_VERSION
    assert {"stage", "lease_until", "heartbeat_at", "progress_json", "action_code"} <= columns

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    db.queue_subtitle_job(video, None, 1)
    claimed = db.claim_due_subtitle_jobs(1)
    assert len(claimed) == 1
    db.update_subtitle_job_stage(video, "aligning", progress={"candidate": 2})
    row = db.subtitle_jobs()[0]
    assert row["state"] == "processing"
    assert row["stage"] == "aligning"
    assert json.loads(row["progress_json"]) == {"candidate": 2}
    db.mark_subtitle_job_needs_action(video, "key missing", "configure_jimaku")
    row = db.subtitle_jobs()[0]
    assert row["state"] == "needs_action"
    assert row["action_code"] == "configure_jimaku"


def test_backup_redacts_secrets_and_preserves_current_tokens_on_restore(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    database = tmp_path / "library.sqlite3"
    cache = tmp_path / "cache"
    cache.mkdir()
    config.write_text(
        '[jimaku]\napi_key = "jimaku-secret"\n[anilist]\naccess_token = "anilist-secret"\n'
        '[ui]\nlanguage = "ru"\n',
        encoding="utf-8",
    )
    db = Database(database)
    with db.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ln_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO ln_settings VALUES('jpdb_api_token','jpdb-secret')")
    archive = tmp_path / "backup.zip"
    create_backup(
        config_path=config,
        database_path=database,
        cache_dir=cache,
        output=archive,
        version="test",
    )
    with zipfile.ZipFile(archive) as bundle:
        joined = b"\n".join(bundle.read(name) for name in bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
    assert b"jimaku-secret" not in joined
    assert b"anilist-secret" not in joined
    assert b"jpdb-secret" not in joined
    assert manifest["secrets_included"] is False

    config.write_text(
        '[jimaku]\napi_key = "current-key"\n[anilist]\naccess_token = "current-token"\n[ui]\nlanguage = "en"\n',
        encoding="utf-8",
    )
    restore_backup(
        archive_path=archive,
        config_path=config,
        database_path=database,
        cache_dir=cache,
    )
    restored = config.read_text(encoding="utf-8")
    assert 'api_key = "current-key"' in restored
    assert 'access_token = "current-token"' in restored
    assert 'language = "ru"' in restored


def test_video_chapter_is_preferred_as_piecewise_boundary() -> None:
    boundary, evidence = choose_edit_boundary(
        90.0,
        220.0,
        midpoint=155.0,
        edit_points=[{"time": 118.0, "kind": "chapter_start", "title": "Opening"}],
        cue_gaps=[(120.0, 205.0, 85.0)],
    )
    assert boundary in {118.0, 208.0}
    assert evidence["kind"] == "chapter_start"


def test_stt_reference_is_generated_once_and_cached(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = [str(value) for value in command]
        calls.append(command)
        if command[-1] == "--check":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "stt_worker" in " ".join(command):
            output = Path(command[-2])
            output.write_text(
                json.dumps({"segments": [{"start": i, "end": i + 0.8, "text": f"台詞{i}"} for i in range(10)]}),
                encoding="utf-8",
            )
        else:
            Path(command[-1]).write_bytes(b"audio")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pudge.subtitles.stt.subprocess.run", fake_run)
    first, info = prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    second, cached = prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    assert first == second
    assert first is not None and first.is_file()
    assert info["cache"] == "miss"
    assert cached["cache"] == "hit"
    assert len(calls) == 3


def test_manga_cbz_import_natural_order_and_page_data(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    archive = tmp_path / "volume.cbz"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, color in (("10.png", "red"), ("2.png", "blue"), ("1.png", "green")):
            image_path = tmp_path / name
            Image.new("RGB", (4, 6), color).save(image_path)
            bundle.write(image_path, name)
    service = MangaService(db)
    book = service.import_file(archive)
    page = service.page(book["id"], 1)
    assert book["page_count"] == 3
    assert page["name"] == "2.png"
    assert page["data_uri"].startswith("data:image/png;base64,")


def test_audiobook_imports_duration_and_chapters(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"audio")
    payload = {
        "format": {"duration": "3600"},
        "chapters": [
            {"start_time": "0", "end_time": "120", "tags": {"title": "Intro"}},
            {"start_time": "120", "end_time": "900", "tags": {"title": "Chapter 1"}},
        ],
    }
    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    book = service.import_file(audio)
    assert book["duration"] == 3600
    assert [chapter["title"] for chapter in book["chapters"]] == ["Intro", "Chapter 1"]
