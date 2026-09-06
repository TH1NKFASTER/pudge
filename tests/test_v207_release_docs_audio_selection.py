from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.backup import create_backup, restore_backup
from pudge.database import Database
from pudge.manager_models import LibraryEpisode


ROOT = Path(__file__).resolve().parents[1]


def test_audiobook_volume_card_selects_on_plain_background_click() -> None:
    js = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert "if(target.closest?.('.audiobook-cover'))return false" in js
    assert "[data-media-action],.audiobook-bookmarks,.audiobook-scrubber-shell" in js
    assert ".audiobook-controls,.audiobook-bookmarks" not in js
    assert "if(!modified&&!audioSelection.size)return" not in js
    assert "if(audioSelection.has(id))audioSelection.delete(id);else audioSelection.add(id)" in js


def test_full_backup_keeps_durable_prepared_subtitle(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    database_path = tmp_path / "library.sqlite3"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    config_path.write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")

    durable_dir = database_path.parent / "prepared-subtitles"
    durable_dir.mkdir()
    prepared = durable_dir / "ready.srt"
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")

    db = Database(database_path)
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Anime",
            episode=1,
            video_path=video,
            subtitle_path=prepared,
            subtitle_origin="jimaku",
            state="ready",
        )
    )

    archive_path = tmp_path / "backup.zip"
    result = create_backup(
        config_path=config_path,
        database_path=database_path,
        cache_dir=cache_dir,
        output=archive_path,
        version="0.7.26",
    )
    assert result["cached_files"] == 1
    with zipfile.ZipFile(archive_path) as archive:
        assert any(name.startswith("prepared-subtitles/") for name in archive.namelist())

    database_path.unlink()
    prepared.unlink()
    restore_backup(
        archive_path=archive_path,
        config_path=config_path,
        database_path=database_path,
        cache_dir=cache_dir,
    )

    restored = Database(database_path).episodes(1)[0]
    assert restored.subtitle_path is not None
    assert restored.subtitle_path.parent == durable_dir
    assert restored.subtitle_path.read_text(encoding="utf-8").endswith("日本語\n")


def test_release_docs_are_0726_and_product_level() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    algorithms = (ROOT / "docs/ALGORITHMS.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/USER_GUIDE.md").read_text(encoding="utf-8")
    vn = (ROOT / "docs/VN_READER_DESIGN.md").read_text(encoding="utf-8")
    development = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    config_example = (ROOT / "config.example.toml").read_text(encoding="utf-8")

    assert "Current version: **0.7.26**." in readme
    assert "pudge-macos-v0.7.26.zip" in readme
    assert "## v0.7.26" in changelog
    assert "Ready means usable now" in algorithms
    assert "next unwatched episode" in guide
    assert "Click the background of a volume card" in guide
    assert "## Current behavior" in vn
    assert "ScreenCaptureKit" not in vn and "**0.8.0:**" not in vn
    assert "make build-release" in development
    assert 'reasoning_effort = "low"' in config_example
    assert config_example == (ROOT / "pudge/config.example.toml").read_text(encoding="utf-8")

    # Product docs should explain decisions without freezing implementation detail.
    for technical_detail in (
        "transition_episode_state",
        "RapidFuzz",
        "FFT",
        "app_jobs",
        "0.65 seconds",
    ):
        assert technical_detail not in algorithms
