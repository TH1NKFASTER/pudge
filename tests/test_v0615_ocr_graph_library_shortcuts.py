from __future__ import annotations

from pathlib import Path

from pudge import ocr
from pudge.config import AppConfig, load_config, write_config
from pudge.relation_graphs import compact_relations_from_graph
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def _pgs_segment(pts: int, segment_type: int, payload: bytes) -> bytes:
    return (
        b"PG"
        + int(pts).to_bytes(4, "big")
        + int(pts).to_bytes(4, "big")
        + bytes([segment_type])
        + len(payload).to_bytes(2, "big")
        + payload
    )


def _minimal_pgs() -> bytes:
    visible_pcs = (
        (4).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + b"\x00"
        + (1).to_bytes(2, "big")
        + b"\x00\x00\x00"
        + b"\x01"
        + (1).to_bytes(2, "big")
        + b"\x00\x00"
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
    )
    palette = b"\x00\x00" + bytes([0, 16, 128, 128, 0, 1, 235, 128, 128, 255])
    rle = b"\x01\x01\x01\x01\x00\x00"
    object_payload = (
        (1).to_bytes(2, "big")
        + b"\x00\xc0"
        + (len(rle) + 4).to_bytes(3, "big")
        + (4).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + rle
    )
    clear_pcs = (
        (4).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + b"\x00"
        + (2).to_bytes(2, "big")
        + b"\x00\x00\x00\x00"
    )
    return b"".join(
        [
            _pgs_segment(90_000, 0x16, visible_pcs),
            _pgs_segment(90_000, 0x14, palette),
            _pgs_segment(90_000, 0x15, object_payload),
            _pgs_segment(90_000, 0x80, b""),
            _pgs_segment(180_000, 0x16, clear_pcs),
            _pgs_segment(180_000, 0x80, b""),
        ]
    )


def test_ocr_setting_round_trips_and_is_exposed_in_web_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.matching.ocr_image_subtitles = True
    write_config(config, path)

    assert load_config(path).matching.ocr_image_subtitles is True

    api = WebAppApi(path)
    assert api._settings_payload()["ocr_image_subtitles"] is True
    result = api.save_settings({"ocr_image_subtitles": False})
    assert result["settings"]["ocr_image_subtitles"] is False
    assert load_config(path).matching.ocr_image_subtitles is False


def test_pgs_is_ocrd_to_cached_srt_during_preparation(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "episode.sup"
    subtitle.write_bytes(_minimal_pgs())
    calls: list[tuple[int, int]] = []

    def recognize(image):
        calls.append(image.size)
        return "こんにちは"

    monkeypatch.setattr(ocr, "_vision_recognize", recognize)
    output, result = ocr.image_subtitle_to_srt(
        video,
        tmp_path / "cache",
        subtitle_path=subtitle,
    )

    assert output is not None
    assert result["reason"] == "ocr_ready"
    assert result["cue_count"] == 1
    assert calls == [(4, 1)]
    text = output.read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,000" in text
    assert "こんにちは" in text

    monkeypatch.setattr(ocr, "_vision_recognize", lambda image: (_ for _ in ()).throw(AssertionError("cache missed")))
    cached, cached_result = ocr.image_subtitle_to_srt(
        video,
        tmp_path / "cache",
        subtitle_path=subtitle,
    )
    assert cached == output
    assert cached_result["reason"] == "cached"


def test_related_edges_do_not_enter_compact_or_full_graphs() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {"media_id": 2, "title": "Sequel"},
            {"media_id": 3, "title": "Merely related"},
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 1, "target": 3, "relation_type": "RELATED"},
        ],
    }
    relations = compact_relations_from_graph(graph, 1)
    assert [item["media_id"] for item in relations] == [2]

    html = HTML.read_text(encoding="utf-8")
    assert "function graphWithoutRelated(graph,rootId=null)" in html
    assert "'SHARED_CHARACTERS','RELATED','OTHER'" in html
    assert "nodes:nodes.filter(item=>visible.has(Number(item.media_id)))" in html


def test_cached_watch_order_is_hidden_until_complete_first_frame_and_logs_timing() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html.split("async function prepareCachedWatchOrderFirstFrame", 1)[1].split(
        "function showWatchOrderModal", 1
    )[0]
    assert source.index("backdrop.hidden=true") < source.index("renderOpenWatchOrder();")
    assert source.index("renderOpenWatchOrder();") < source.index("backdrop.hidden=false")
    assert source.index("backdrop.classList.add('preparing','watch-order-open')") < source.index(
        "backdrop.classList.add('open')"
    )
    assert "watch_order.cache_rendered" in source
    assert "watch_order.first_frame" in source
    assert ".modal-backdrop[hidden] { display:none !important; }" in html


def test_library_episode_width_card_link_shortcuts_and_polychrome_restart() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "const episodeColumnCh=Math.max(5,...episodeLabels.map" in html
    assert "--episode-column-width:${episodeColumnCh}ch" in html
    assert ".library-episode > strong { white-space:nowrap; }" in html
    assert "a.site_url||`https://anilist.co/anime/${a.media_id}`" in html
    assert "if(libraryCard&&!e.target.closest('details,button,a,input,select'))" in html
    assert "document.querySelectorAll('.nav button[data-page]')" in html
    assert "shortcut_app_watching" not in html
    assert "if(cover.classList.contains('polychrome-wake')" in html
    assert "setTimeout(wakePolychromeAnimations,180)" not in html


def test_ocr_is_a_prepare_time_fallback_after_text_selection() -> None:
    source = (ROOT / "pudge" / "cli.py").read_text(encoding="utf-8")
    discovery = source.index("if subtitle is None and subtitle_id is None and not args.fast_play:")
    ocr_step = source.index("if config.matching.ocr_image_subtitles and not args.fast_play:")
    ready = source.index('print("PREPARE_STATUS=ready")')
    assert discovery < ocr_step < ready
    assert "OCR: преобразую графические японские субтитры в SRT заранее" in source
    assert "subtitle = ocr_subtitle" in source
    assert "embedded_bitmap_fallback = None" in source[ocr_step:ready]


def test_installer_verifies_and_records_current_version_dynamically() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'EXPECTED_VERSION="${WHEEL_NAME#pudge-}"' in installer
    assert 'assert installed == expected, (installed, expected)' in installer
    assert 'assert __version__ == expected, (__version__, expected)' in installer
    assert "printf '%s\\n' \"$EXPECTED_VERSION\" > \"$DATA_DIR/installed-version.txt\"" in installer
    assert 'assert __version__ == "0.6.' not in installer


def test_text_candidates_are_selected_before_bitmap_ocr_fallback() -> None:
    source = (ROOT / "pudge" / "cli.py").read_text(encoding="utf-8")
    split = source.index("text_candidates = [")
    optimization = source.index("best, optimized_path, result = optimize_candidates(")
    ocr_step = source.index("if config.matching.ocr_image_subtitles and not args.fast_play:")
    assert split < optimization < ocr_step
    assert "candidates = text_candidates" in source[split:optimization]
    assert "bitmap_candidate_fallback" in source[split:ocr_step]
