from pathlib import Path

from pudge import media


def test_embedded_bitmap_does_not_hide_text_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *_: {
            "streams": [
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "jpn"},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "jpn", "title": "Japanese full"},
                },
            ]
        },
    )

    candidates = media.find_embedded_japanese_subtitles(
        Path("movie.mkv"), "ffprobe", "ffmpeg"
    )

    assert len(candidates) == 2
    assert candidates[0].codec == "ass"
    assert candidates[1].codec == "hdmv_pgs_subtitle"


def test_text_only_ignores_japanese_pgs(monkeypatch) -> None:
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *_: {
            "streams": [
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "jpn"},
                }
            ]
        },
    )

    result = media.find_embedded_japanese_subtitle(
        Path("movie.mkv"), "ffprobe", "ffmpeg", text_only=True
    )

    assert result is None
