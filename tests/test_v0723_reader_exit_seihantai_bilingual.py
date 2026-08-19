from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.library import scan_library
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.pipeline_cache import _CACHE_SCHEMA
from pudge.subtitle_formats import clean_srt_for_playback

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_ln_reader_exit_stops_paired_audiobook_process() -> None:
    service = object.__new__(AudiobookService)
    calls: list[int] = []
    service.link_for_light_novel = lambda _ln, include_alignment=False: {"book": {"id": 75}}  # type: ignore[method-assign]
    service.stop = lambda book_id: calls.append(int(book_id)) or {"ok": True, "stopped": True}  # type: ignore[method-assign]

    result = service.stop_for_light_novel(6)

    assert result == {"ok": True, "stopped": True, "audiobook_id": 75}
    assert calls == [75]
    html = HTML.read_text(encoding="utf-8")
    close = html[html.index("if(target.id==='lnReaderClose')"):]
    close = close[: close.index("return;") + len("return;")]
    assert "light_novel_stop_paired(closingBookId)" in close
    assert "ui.lnPairedState=null" in close


def test_library_scan_repairs_detached_absolute_release_to_media_episode(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "library"
    folder = root / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text("210031", encoding="utf-8")
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episodes=13,
            status="CURRENT",
        )
    )
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_args, **_kwargs: ("none", None, None))

    rows = scan_library(
        root,
        db,
        media_episode_resolver=lambda anime, release: 7 if anime.media_id == 210031 and release == 19 else release,
    )

    assert len(rows) == 1
    assert rows[0].media_id == 210031
    assert rows[0].episode == 7
    assert rows[0].media_episode == 7
    assert rows[0].release_episode == 19



def test_manager_scan_maps_seihantai_release_19_to_episode_7_from_numbering_cache(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    folder = cfg.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text("210031", encoding="utf-8")
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episodes=13,
            status="CURRENT",
        )
    )
    cache = cfg.paths.cache_dir / "anilist-release-numbering" / "210031.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"offset":12,"resolver_version":2,"prequel_titles":[]}', encoding="utf-8")
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_args, **_kwargs: ("none", None, None))

    manager.scan_library()

    episode = manager.db.episode_by_path(video.resolve())
    assert episode is not None
    assert episode.media_id == 210031
    assert episode.episode == 7
    assert episode.media_episode == 7
    assert episode.release_episode == 19

def test_library_scan_uses_completed_download_metadata_before_backend_forgets_task(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "library"
    folder = root / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text("210031", encoding="utf-8")
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=210031, title="Seihantai na Kimi to Boku 2nd Season", episodes=13))
    db.upsert_download(
        DownloadItem(
            torrent_hash="f3a099e5c9a9519b1b406a23202f94e1e9d07157",
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video),
            media_id=210031,
            episode=7,
            media_episode=7,
            release_episode=19,
            raw={"backend": "aria2", "total_size": 5, "downloaded": 5},
        )
    )
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_args, **_kwargs: ("none", None, None))

    rows = scan_library(root, db, media_episode_resolver=lambda _anime, _release: None)

    assert rows[0].episode == 7
    assert rows[0].release_episode == 19
    assert rows[0].torrent_hash == "f3a099e5c9a9519b1b406a23202f94e1e9d07157"


def test_same_cue_japanese_chinese_second_line_is_removed_and_cache_is_new(tmp_path: Path) -> None:
    source = tmp_path / "bilingual.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nこれは日本語の台詞です\n这是中文翻译台词\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n次の日本語です\n我们继续说话\n",
        encoding="utf-8",
    )

    cleaned, result = clean_srt_for_playback(source, tmp_path / "cache")
    payload = cleaned.read_text(encoding="utf-8")

    assert cleaned.name.startswith("v14-")
    assert "这是中文翻译台词" not in payload
    assert "我们继续说话" not in payload
    assert "これは日本語の台詞です" in payload
    assert "次の日本語です" in payload
    assert result["bilingual_cjk"] is True
    assert result["bilingual_profile"]["removed_inline_chinese_lines"] == 2
    assert _CACHE_SCHEMA == "final-pipeline-v10"
