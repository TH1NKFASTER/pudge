"""Image-subtitle OCR at preparation time, the watch-order graph, page shortcuts, the foil
shine on covers and the installer's version check (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import pudge.cli as cli
from css_support import css_rules
from pudge import ocr
from pudge.config import AppConfig, load_config, write_config
from pudge.models import SubtitleCandidate
from pudge.relation_graphs import compact_relations_from_graph
from pudge.web_app import WebAppApi
from source_policy import source
from tests.i18n_support import en, ru


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

    monkeypatch.setattr(
        ocr, "_vision_recognize", lambda image: (_ for _ in ()).throw(AssertionError("cache missed"))
    )
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


GRAPH = {
    "nodes": [
        {"media_id": 1, "title": "Root"},
        {"media_id": 2, "title": "Sequel"},
        {"media_id": 3, "title": "Merely related"},
        {"media_id": 4, "title": "Shares a character"},
    ],
    "edges": [
        {"source": 1, "target": 2, "relation_type": "SEQUEL"},
        {"source": 1, "target": 3, "relation_type": "RELATED"},
        {"source": 2, "target": 4, "relation_type": "SHARED_CHARACTERS"},
    ],
}


def test_cached_watch_order_shows_the_story_graph_only_once_its_first_frame_is_ready(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        setPage('planned', true); await __tick(100);
        // What the window shows each time the modal's content changes.
        window.seen = [];
        new MutationObserver(() => window.seen.push(__visible('#modalBackdrop'))).observe(document.getElementById('modalBody'), {childList: true, subtree: true});
        await __click('#plannedContent [data-media-id="1"] .anime-title');
        await __flush();
        const whileRendering = [...window.seen];
        await __tick(1000);
        return {whileRendering, open: __visible('#modalBackdrop'), text: __text('#modalBody')};
        """,
        state={"planned": [{"media_id": 1, "title": "Root", "status": "PLANNING", "media_kind": "anime"}]},
        api={
            "get_relation_graph_cache": {
                "graphs": {"g1": {"graph": GRAPH, "refreshed_at": 1}},
                "media_to_graph": {"1": "g1", "2": "g1", "3": "g1", "4": "g1"},
            }
        },
    )
    out = result.value
    # The graph is drawn while the modal is still hidden (no half-built frame flashes) ...
    assert out["whileRendering"] and not any(out["whileRendering"])
    assert out["open"] is True
    # A hidden backdrop stays hidden whatever classes it has (WebKit lets `.open {display:flex}`
    # override the hidden attribute; the page harness does not model that cascade).
    assert css_rules("index.html").get(".modal-backdrop[hidden]", "display") == "none !important"
    # ... and only story relations are in it: merely related titles and shared characters are not.
    assert "Root" in out["text"] and "Sequel" in out["text"]
    assert "Merely related" not in out["text"] and "Shares a character" not in out["text"]
    events = [args[0] for args in result.calls_of("log_ui_event")]
    assert events.index("watch_order.cache_rendered") < events.index("watch_order.first_frame")


def test_cmd_digit_opens_the_pages_in_sidebar_order(page) -> None:
    result = page.run(
        r"""
        const pages = [];
        for (const digit of ['2', '3', '6', '1']) {
          await __key(digit, {metaKey: true, code: `Digit${digit}`}, 'body'); await __tick(50);
          pages.push(ui.page);
        }
        const order = [...document.querySelectorAll('.nav button[data-page]')].map(button => button.dataset.page);
        return {pages, order};
        """,
    )
    order = result.value["order"]
    assert (
        result.value["pages"]
        == [order[1], order[2], order[5], order[0]]
        == ["lightnovels", "manga", "planned", "current"]
    )


def test_hovering_a_shining_cover_does_not_restart_its_foil_shine(page) -> None:
    show = {
        "media_id": 7,
        "title": "Show",
        "episode": 3,
        "video_path": "/library/Show - 03.mkv",
        "cover": "https://img.example/7.jpg",
    }
    result = page.run(
        """
        const cover = () => document.querySelector('#currentContent .cover-shell.polychrome');
        const shines = () => Number(cover().dataset.polychromeToken || 0);
        await __tick(3000);
        await __click('.nav button[data-page="planned"]'); await __tick(100);
        await __click('.nav button[data-page="current"]'); await __tick(100);   // a return to Watching starts a shine
        const started = shines();
        await __fire('#currentContent .cover-shell.polychrome', 'pointerover', {}, 'PointerEvent'); await __tick(100);
        const hovered = shines();
        await __fire('#currentContent .cover-shell.polychrome', 'pointerout', {}, 'PointerEvent');
        await __tick(3000);
        await __fire('#currentContent .cover-shell.polychrome', 'pointerover', {}, 'PointerEvent'); await __tick(100);
        return {started, hovered, later: shines()};
        """,
        state={"home": {"continue_watching": [show]}},
    )
    out = result.value
    # A hover during the shine is not a second start (it used to begin again a few frames in).
    assert out["hovered"] == out["started"] and out["later"] == out["started"] + 1


def _prepare(tmp_path: Path, monkeypatch, *, text_accepted: bool):
    """Preparation of an episode with a local Japanese SRT (score 80) and a PGS (score 95)."""
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    srt, sup = tmp_path / "Show - 01.ja.srt", tmp_path / "Show - 01.sup"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは。\n", encoding="utf-8")
    sup.write_bytes(b"PGS")
    ocr_srt = tmp_path / "ocr.srt"
    ocr_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは。これは日本語です。\n", encoding="utf-8")
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.matching.ocr_image_subtitles = True
    monkeypatch.setattr(cli, "find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr(
        cli,
        "find_local_subtitles",
        lambda **_k: [
            SubtitleCandidate(path=sup, source="local", score=95.0, name=sup.name),
            SubtitleCandidate(path=srt, source="local", score=80.0, name=srt.name),
        ],
    )
    synced: list[str] = []
    monkeypatch.setattr(
        cli,
        "_sync_one",
        lambda _video, subtitle, *a, **k: (
            synced.append(subtitle.name) or (subtitle, {"sync_was_successful": True})
        ),
    )
    monkeypatch.setattr(
        cli,
        "subtitle_quality_accepted",
        lambda *a, **k: (True, "") if text_accepted else (False, "low_confidence"),
    )
    converted: list[Path] = []
    monkeypatch.setattr(
        cli,
        "image_subtitle_to_srt",
        lambda _video, _cache, **kw: (
            converted.append(kw["subtitle_path"])
            or (ocr_srt, {"reason": "ocr_ready", "cue_count": 1, "quality": {"accepted": True}})
        ),
    )
    args = cli.build_parser().parse_args(["--prepare-only", "--offline", "--no-anilist-progress", str(video)])
    return cli.process_video(video, args, cfg, None), synced, converted, sup


def test_a_text_subtitle_wins_over_a_higher_scored_image_subtitle_and_needs_no_ocr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status, synced, converted, _sup = _prepare(tmp_path, monkeypatch, text_accepted=True)
    out = capsys.readouterr().out
    assert status == 0 and synced == ["Show - 01.ja.srt"] and converted == []
    assert en("cli.ocrStarting") not in out and "PREPARE_STATUS=ready" in out


def test_ocr_of_the_image_subtitle_is_the_fallback_when_text_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status, synced, converted, sup = _prepare(tmp_path, monkeypatch, text_accepted=False)
    out = capsys.readouterr().out
    assert status == 0 and synced == ["Show - 01.ja.srt", "Show - 01.sup"] and converted == [sup]
    # OCR runs during preparation, before the episode is reported ready, and its SRT is used.
    assert out.index(en("cli.ocrStarting")) < out.index("PREPARE_STATUS=ready")
    assert '"generated_by_ocr": true' in out
    assert en("cli.ocrStarting") == "OCR: converting Japanese image subtitles to SRT in advance…"
    assert "OCR: преобразую графические японские субтитры в SRT заранее" in ru("cli.ocrStarting")


def _installer_version_check(
    tmp_path: Path, wheel: str, installed: str, module_version: str
) -> tuple[subprocess.CompletedProcess, Path]:
    shell = shutil.which("zsh")
    if not shell:
        pytest.skip("the macOS installer shell (zsh) is not available")
    # policy: install.sh is a macOS-only program (venv, app bundle, launchctl) that cannot be
    # sourced; its version check is cut out of it and run here against a fake installed package.
    installer = source("install.sh")
    start = installer.index('WHEEL_NAME="${WHEEL_PATH:t}"')
    end = installer.index("PYVERIFY\n", installer.index("<<'PYVERIFY'")) + len("PYVERIFY\n")
    record = next(line for line in installer.splitlines() if "installed-version.txt" in line)
    site = tmp_path / "site"
    (site / "pudge").mkdir(parents=True)
    (site / "pudge" / "__init__.py").write_text(f"__version__ = {module_version!r}\n")
    (site / "pudge" / "config.py").write_text("def load_config(path=None):\n    return None\n")
    (site / f"pudge-{installed}.dist-info").mkdir()
    (site / f"pudge-{installed}.dist-info" / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: pudge\nVersion: {installed}\n"
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(
        f"#!/bin/sh\nexec env -u PYTHONPATH {sys.executable} -c \"import sys; sys.path[:0] = ['{site}']; exec(sys.stdin.read())\"\n"
    )
    python.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    script = tmp_path / "verify.zsh"
    script.write_text(
        f"set -euo pipefail\nWHEEL_PATH=/dist/{wheel}\nVENV_DIR={venv}\nDATA_DIR={data}\n"
        + installer[start:end]
        + record
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [shell, str(script)], capture_output=True, text=True, cwd=tmp_path, timeout=60
    ), data / "installed-version.txt"


def test_installer_checks_the_installed_code_against_the_wheel_version(tmp_path: Path) -> None:
    done, recorded_output = _installer_version_check(
        tmp_path / "ok", "pudge-9.8.7-py3-none-any.whl", "9.8.7", "9.8.7"
    )
    assert done.returncode == 0, done.stderr
    assert recorded_output.read_text() == "9.8.7\n"  # derived from the wheel name, not a constant
    stale, recorded_output = _installer_version_check(
        tmp_path / "stale", "pudge-9.8.7-py3-none-any.whl", "9.8.6", "9.8.7"
    )
    assert stale.returncode != 0 and "9.8.6" in stale.stderr and not recorded_output.exists()
    mixed, _ = _installer_version_check(tmp_path / "mixed", "pudge-9.8.7-py3-none-any.whl", "9.8.7", "9.8.6")
    assert mixed.returncode != 0 and "9.8.6" in mixed.stderr
