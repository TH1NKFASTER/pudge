from __future__ import annotations

import time
from pathlib import Path
import re

import pytest

from pudge.companion_streaming import CompanionStreamingService
from pudge.database import Database
from pudge.mobile_sync import MobileSyncService


def _anime_entity(tmp_path: Path) -> tuple[Database, dict, Path]:
    db = Database(tmp_path / "pudge.sqlite3")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"fake-video")
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,updated_at) VALUES(?,?,?)",
            (42, "Fixture Anime", now),
        )
        conn.execute(
            """
            INSERT INTO episodes(
                media_id,title,episode,media_episode,video_path,state,
                playback_position,playback_duration,playback_updated_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (42, "Episode 3", 3, 3, str(video), "ready", 15.5, 1440.0, now, now),
        )
    sync = MobileSyncService(db)
    entity = next(
        item for item in sync.library_snapshot()["entities"]
        if item["kind"] == "anime_episode" and item["metadata"]["episode"] == 3
    )
    return db, entity, video


def test_streaming_resolves_opaque_entity_without_exposing_source_path(tmp_path: Path) -> None:
    db, entity, video = _anime_entity(tmp_path)
    service = CompanionStreamingService(
        db,
        cache_dir=tmp_path / "cache",
        ffmpeg="/bin/false",
        ffprobe="/bin/false",
    )
    resolved = service._resolve_episode(entity["entity_id"])
    assert resolved["video_path"] == video.resolve()
    assert resolved["episode"] == 3
    assert resolved["position_ms"] == 15500
    assert resolved["duration_ms"] == 1440000

    cache_key, output_dir = service._cache_dir(video.resolve())
    output_dir.mkdir(parents=True)
    (output_dir / "index.m3u8").write_text("#EXTM3U\n#EXTINF:4,\nsegment-00000.ts\n#EXT-X-ENDLIST\n", encoding="utf-8")
    (output_dir / "segment-00000.ts").write_bytes(b"segment")
    ticket = service._issue_ticket(cache_key=cache_key, entity_id=entity["entity_id"], output_dir=output_dir)
    path, content_type = service.media_path(ticket.ticket, "segment-00000.ts")
    assert path.read_bytes() == b"segment"
    assert content_type == "video/mp2t"
    with pytest.raises(ValueError):
        service.media_path(ticket.ticket, "../episode.mkv")


def test_hls_command_is_iphone_compatible_and_hardware_first(tmp_path: Path) -> None:
    db, _entity, video = _anime_entity(tmp_path)
    service = CompanionStreamingService(
        db,
        cache_dir=tmp_path / "cache",
        ffmpeg="/bin/echo",
        ffprobe="/bin/echo",
    )
    output = tmp_path / "out"
    command = service._command(video, output, "h264_videotoolbox")
    joined = " ".join(command)
    assert "h264_videotoolbox" in command
    assert "aac" in command
    assert "yuv420p" in command
    assert "-hls_time 4" in joined
    assert "independent_segments+temp_file" in joined
    assert str(output / "index.m3u8") == command[-1]


def test_companion_anime_player_and_http_contracts() -> None:
    root = Path(__file__).parents[1]
    app = (root / "pudge" / "web" / "companion" / "app.js").read_text(encoding="utf-8")
    html = (root / "pudge" / "web" / "companion" / "index.html").read_text(encoding="utf-8")
    css = (root / "pudge" / "web" / "companion" / "styles.css").read_text(encoding="utf-8")
    http = (root / "pudge" / "mobile_sync_http.py").read_text(encoding="utf-8")
    web_app = (root / "pudge" / "web_app.py").read_text(encoding="utf-8")

    for contract in (
        "PUDGE_COMPANION_ANIME_STREAMING_V12",
        "openAnimePlayer",
        "prepareAnimeStream",
        "syncAnimeProgress",
        "parseWebVtt",
        "animeSiblings",
        "/stream",
    ):
        assert contract in app
    for element_id in (
        "animePlayerView",
        "animeVideo",
        "animeSubtitleOverlay",
        "animeSpeed",
        "animePrevEpisode",
        "animeNextEpisode",
    ):
        assert f'id="{element_id}"' in html
    js = re.search(r"app\.js\?v=(\d+)", html)
    css_version = re.search(r"styles\.css\?v=(\d+)", html)
    assert js is not None
    assert css_version is not None
    assert js.group(1) == css_version.group(1)
    assert int(js.group(1)) >= 12
    assert ".anime-video-stage" in css
    assert "streaming.prepare(entity_id)" in http
    assert "streaming.media_path" in http
    assert "CompanionStreamingService" in web_app
    assert "streaming=self.companion_streaming" in web_app
    assert "streaming=api.companion_streaming" in web_app
