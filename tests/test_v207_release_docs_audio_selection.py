"""Audiobook volume cards select on a plain click; a full backup keeps prepared subtitles
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from t71g01_support import AUDIOBOOKS, AUDIO_SELECTION

from pudge.backup import create_backup, restore_backup
from pudge.database import Database
from pudge.manager_models import LibraryEpisode


def test_audiobook_volume_card_selects_on_plain_background_click(page) -> None:
    result = page.run(
        AUDIO_SELECTION
        + r"""
        const out = {};
        const step = async (name, selector) => { await __click(selector); await __tick(10); out[name] = selected(); };
        await step('background', `${card(5)} .audiobook-main`);
        // The empty part of the controls row is card background too.
        await step('controlsRow', `${card(4)} .audiobook-controls`);
        await step('toggleOff', `${card(4)} .audiobook-controls`);
        // Real controls, the cover, bookmarks and the timeline keep their own actions.
        await step('play', `${card(4)} [data-media-action="play-audio"]`);
        await step('cover', `${card(3)} .audiobook-cover`);
        await step('plainCover', `${card(4)} .audiobook-cover`);   // cover art without a link
        await step('bookmarks', `${card(3)} .audiobook-bookmarks`);
        await step('bookmark', `${card(3)} .audiobook-bookmark-label`);
        await step('timeline', `${card(4)} .audiobook-scrubber-shell`);
        // A volume of a series card selects just that volume.
        await step('volume', `${card(3)} .audiobook-main`);
        return out;
        """,
        api={**AUDIOBOOKS, "audiobook_play": {"ok": True}, "audiobook_seek_to": {"ok": True}},
    )
    assert result.value == {
        "background": [5],
        "controlsRow": [4, 5],
        "toggleOff": [5],
        "play": [5],
        "cover": [5],
        "plainCover": [5],
        "bookmarks": [5],
        "bookmark": [5],
        "timeline": [5],
        "volume": [3, 5],
    }
    assert [call[0] for call in result.calls_of("audiobook_play")] == [4]
    assert result.calls_of("open_url") == [["https://anilist.co/manga/9"]]
    assert result.calls_of("audiobook_seek_to") == [[3, 60]]


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
