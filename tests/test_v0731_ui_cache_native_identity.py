from __future__ import annotations

import os
from pathlib import Path

from pudge.cache_management import cleanup_segment_audio_cache, mark_segment_audio_active

ROOT = Path(__file__).resolve().parents[1]


def test_segment_audio_cleanup_removes_stale_and_caps_old_files(tmp_path: Path) -> None:
    root = tmp_path / "segment-audio"
    root.mkdir()
    now = 2_000_000.0
    stale = root / "stale.wav"
    stale.write_bytes(b"x" * 80)
    os.utime(stale, (now - 2 * 86400, now - 2 * 86400))
    old_a = root / "old-a.flac"
    old_b = root / "old-b.flac"
    old_a.write_bytes(b"a" * 70)
    old_b.write_bytes(b"b" * 70)
    os.utime(old_a, (now - 7200, now - 7200))
    os.utime(old_b, (now - 7100, now - 7100))

    result = cleanup_segment_audio_cache(
        tmp_path,
        now=now,
        force=True,
        max_age_seconds=86400,
        max_bytes=80,
        cap_min_age_seconds=3600,
    )

    assert not stale.exists()
    assert result["remaining_bytes"] <= 80
    assert result["removed_files"] >= 2


def test_segment_audio_cleanup_never_removes_active_file(tmp_path: Path) -> None:
    root = tmp_path / "segment-audio"
    root.mkdir()
    now = 3_000_000.0
    active = root / "active.flac"
    active.write_bytes(b"a" * 100)
    os.utime(active, (now - 2 * 86400, now - 2 * 86400))
    mark_segment_audio_active(active, True)
    try:
        cleanup_segment_audio_cache(
            tmp_path,
            now=now,
            force=True,
            max_age_seconds=1,
            max_bytes=1,
            cap_min_age_seconds=0,
        )
        assert active.exists()
    finally:
        mark_segment_audio_active(active, False)


def test_segment_audio_is_lossless_compressed_and_force_reuses_cache() -> None:
    source = (ROOT / "pudge" / "syncing.py").read_text(encoding="utf-8")
    assert 'output = output_dir / f"{digest}.flac"' in source
    assert '"-c:a", "flac", "-compression_level", "0"' in source
    block = source[source.index("def _extract_audio_segment"):source.index("def _clip_cues(")]
    assert "if force:\n        output.unlink" not in block
    assert "touch_segment_audio(output)" in block


def test_multivolume_cards_are_bounded_and_focus_unfinished_volume() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "grid-template-columns:repeat(auto-fill,minmax(min(280px,100%),1fr))" in html
    assert "grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))" in html
    assert ".ln-grid>*{min-width:0}" in html
    assert ".ln-series-group{display:grid;gap:9px;min-width:0;width:100%;max-width:100%;box-sizing:border-box;overflow:hidden" in html
    assert ".ln-series-books.series-scroll{height:195px;overflow-y:auto;overflow-x:hidden;grid-auto-rows:max-content;align-content:start" in html
    assert "const unfinished=book=>!Boolean(book?.finished);" in html
    assert "return group.find(unfinished)||group[group.length-1];" in html


def test_manga_multivolume_has_first_paint_height_cap() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert ".ln-series-books.series-scroll{height:195px" in html
    assert "window.PudgeSeriesScroll?.focus?.(root);" in manga
    assert "if (scrollHost && previousScrollTop != null) scrollHost.scrollTop = previousScrollTop;" in manga


def test_native_launcher_embeds_python_in_pudge_process() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "#include <Python.h>" in installer
    assert "Py_InitializeFromConfig(&config)" in installer
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in installer
    assert "return Py_RunMain();" in installer
    assert "$PYTHON_CONFIG --embed --cflags" in installer
    assert "$PYTHON_CONFIG --embed --ldflags" in installer
    assert "execv(python" not in installer
