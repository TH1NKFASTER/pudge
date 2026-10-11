"""iPhone: illustrations from the Mac (with the Mac's blur), anime in the Mac Home order."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.iphone_media import MediaError
from source_policy import source


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _png(width: int, height: int) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (width, height), (20, 120, 200)).save(data, "PNG")
    return data.getvalue()


def test_illustrations_reach_the_iphone_with_the_mac_blur_setting(service, tmp_path):
    covers = tmp_path / "covers"
    covers.mkdir()
    name = "ln-" + "ab" * 12 + ".png"
    (covers / name).write_bytes(_png(800, 1200))
    big = "ln-" + "cd" * 12 + ".jpg"
    Image.new("RGB", (3000, 2000)).save(covers / big, "JPEG")
    payload = {
        "chapter_index": 1,
        "title": "One",
        "parsing": False,
        "paragraphs": [f"[[PUDGE_LN_IMAGE_URL:covers/{name}]]", "[[PUDGE_LN_IMAGE_URL:../etc/passwd]]"],
        "tokens": [[], []],
        "vocabulary": [],
    }
    settings = SimpleNamespace(study_backend="jiten", review_mode="binary", blur_images=True)
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(settings=lambda: settings, cover_cache_dir=covers),
        light_novel_chapter=lambda book, chapter: payload,
    )
    view = service.media.handle("GET", "/api/v2/media/ln/3/chapters/1", {}, dict)
    assert [row["imageId"] for row in view["paragraphs"]] == [name, None]
    data, kind = service.media.handle("GET", f"/api/v2/media/ln-image/{name}", {}, dict)
    assert kind == "image/png" and data == (tmp_path / "covers" / name).read_bytes()
    data, kind = service.media.handle("GET", f"/api/v2/media/ln-image/{big}", {}, dict)
    assert kind == "image/jpeg" and Image.open(io.BytesIO(data)).width == 1600
    for bad in ("passwd", "ln-../x.png", "ln-" + "ab" * 11 + ".png"):
        with pytest.raises(MediaError):
            service.media.ln_image(bad)
    assert service.media.reader_settings()["blurImages"] is True


def test_anime_follow_the_mac_home_sections(service, tmp_path):
    with service.db.connect() as conn:
        for media_id, title in ((1, "Alpha"), (2, "Beta"), (3, "Gamma"), (4, "Delta")):
            conn.execute(
                "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(?,?,'CURRENT',0,12,0)",
                (media_id, title),
            )
            video = tmp_path / f"{media_id}.mkv"
            video.write_bytes(b"x")
            subtitle = tmp_path / f"{media_id}.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
            conn.execute(
                "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(?,?,1,?,?,'ready',0)",
                (media_id, title, str(video), str(subtitle)),
            )
    home = {
        "new_ready": [{"media_id": 3}, {"media_id": 1}],
        "completed_ready": [{"kind": "watch_sequence", "items": [{"media_id": 4}]}],
    }
    service.api = SimpleNamespace(
        get_state_fast=lambda: {"home": home},
        manager=SimpleNamespace(db=SimpleNamespace(anime_list=lambda: [])),
    )
    # Ready new (Mac order), then ready old, then everything else alphabetically.
    assert [group["title"] for group in service.media.anime()["items"]] == ["Gamma", "Alpha", "Delta", "Beta"]
    service.api = SimpleNamespace(
        get_state_fast=lambda: 1 / 0, manager=SimpleNamespace(db=SimpleNamespace(anime_list=lambda: []))
    )
    assert [group["title"] for group in service.media.anime()["items"]] == ["Alpha", "Beta", "Delta", "Gamma"]


def test_iphone_reader_follows_the_mac_and_zooms_manga():
    # policy: the iPhone app (Swift) cannot run here and these are phone-only interactions
    # (shelf volume picked like the Mac, the audio bar's follow/return arrow, pinch zoom).
    # What the Mac serves for them is tested above: illustrations with the Mac's blur and
    # anime in the Mac Home order.
    read = source("ios/Pudge/UI/ReadScreen.swift")
    assert "func continueTarget" not in read and "func macCurrent<Item>" in read
    assert "ReaderAudioBar(player: player, following: follow, onFollow: returnToAudio)" in read
    assert "abs(offset - (dragStart ?? offset)) > 24 { follow = false }" in read
    assert "guard follow, !dragging, let value else { return }" in read
    assert "struct ZoomableContainer<Content: View>: UIViewRepresentable" in read
    assert "ZoomableContainer(active: active, onTap:" in read
