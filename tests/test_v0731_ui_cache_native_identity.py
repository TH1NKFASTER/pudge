"""Segment-audio cache, multi-volume shelves and the native launcher's embedded Python
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from css_support import css_rules
from t71g01_support import release_history

from pudge import syncing
from pudge.cache_management import cleanup_segment_audio_cache, mark_segment_audio_active


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


def test_segment_audio_is_lossless_flac_and_a_forced_resync_reuses_the_cache(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append([str(part) for part in command])
        Path(command[-1]).write_bytes(b"fLaC audio")
        return type("Completed", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(syncing.subprocess, "run", run)
    cache = tmp_path / "cache"
    output, error = syncing._extract_audio_segment(
        video, 12.5, 30.0, cache, ffmpeg_path=str(ffmpeg), force=False
    )
    assert error is None and output is not None
    assert output.parent == cache / "segment-audio" and output.suffix == ".flac"
    assert output.read_bytes() == b"fLaC audio"
    [command] = commands
    # Mono 16 kHz, FLAC at the fastest level: lossless for ffsubsync, small for mostly silent windows.
    assert command[command.index("-ac") + 1 : command.index("-ac") + 4] == ["1", "-ar", "16000"]
    assert command[-7:] == ["-c:a", "flac", "-compression_level", "0", "-f", "flac", f"{output}.tmp"]
    # A forced resync of the same window does not decode the episode again; it marks the file as used.
    old = time.time() - 3600
    os.utime(output, (old, old))
    again, error = syncing._extract_audio_segment(
        video, 12.5, 30.0, cache, ffmpeg_path=str(ffmpeg), force=True
    )
    assert (again, error) == (output, None) and len(commands) == 1
    assert output.stat().st_mtime > old + 3000


# Layout the window does not have here: series cards 90px high, 7px apart, moving with the
# shelf's scroll position.
LAYOUT = r"""
new MutationObserver(() => {
  for (const scroller of document.querySelectorAll('[data-series-scroll]')) {
    if (scroller.__rect) continue;
    scroller.__rect = {x: 0, y: 100, width: 300, height: 195};
    [...scroller.querySelectorAll('.ln-entry')].forEach((card, i) => Object.defineProperty(card, '__rect', {
      get: () => ({x: 0, y: 100 + i * 97 - scroller.scrollTop, width: 300, height: 90})}));
  }
}).observe(document.body, {childList: true, subtree: true});
"""


def _ln_book(book_id: int, volume: int, finished: bool, series: str, media: int) -> dict:
    return {
        "id": book_id,
        "title": f"{series} {volume}",
        "series_title": series,
        "volume": volume,
        "finished": int(finished),
        "anilist_id": media,
        "file_type": "epub",
    }


def test_multivolume_cards_are_bounded_and_focus_the_first_unfinished_volume(page) -> None:
    books = [
        _ln_book(1, 1, True, "Alpha", 100),
        _ln_book(2, 2, False, "Alpha", 100),
        _ln_book(3, 3, False, "Alpha", 100),
        # Every volume finished: the last one.
        _ln_book(4, 1, True, "Beta", 200),
        _ln_book(5, 2, True, "Beta", 200),
        _ln_book(6, 3, True, "Beta", 200),
    ]
    result = page.run(
        LAYOUT
        + r"""
        setPage('lightnovels'); await __tick(300);
        return [...document.querySelectorAll('.ln-series-group [data-series-scroll]')].map(scroller => ({
          current: Number(scroller.dataset.seriesCurrentBook), height: scroller.style.height, top: scroller.scrollTop}));
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": books}},
    )
    # The shelf shows the current card and the next one (the last volume alone), scrolled to it.
    assert result.value == [
        {"current": 2, "height": "187px", "top": 97},
        {"current": 6, "height": "90px", "top": 194},
    ]
    rules = css_rules("index.html")
    # Columns never get wider than the window; a long series scrolls inside its card.
    assert rules.get(".ln-grid", "grid-template-columns") == "repeat(auto-fill,minmax(min(300px,100%),1fr))"
    assert rules.get(".ln-grid>*", "min-width") == "0"
    group = rules.declarations(".ln-series-group")
    assert (group["min-width"], group["max-width"], group["overflow"], group["box-sizing"]) == (
        "0",
        "100%",
        "hidden",
        "border-box",
    )
    books_scroll = rules.declarations(".ln-series-books.series-scroll")
    assert (books_scroll["height"], books_scroll["overflow-y"], books_scroll["overflow-x"]) == (
        "195px",
        "auto",
        "hidden",
    )


def test_manga_multivolume_series_is_capped_and_focused_on_the_current_volume(page) -> None:
    books = [
        {
            "id": 11,
            "title": "M v1",
            "series_title": "Manga",
            "page_count": 10,
            "read_pages": 10,
            "anilist_id": 77,
        },
        {
            "id": 12,
            "title": "M v2",
            "series_title": "Manga",
            "page_count": 10,
            "read_pages": 3,
            "anilist_id": 77,
        },
        {
            "id": 13,
            "title": "M v3",
            "series_title": "Manga",
            "page_count": 10,
            "read_pages": 0,
            "anilist_id": 77,
        },
    ]
    result = page.run(
        LAYOUT
        + r"""
        setPage('manga'); await __tick(300);
        const scroller = document.querySelector('#mangaContent [data-series-scroll]');
        // A re-render keeps the reader's place: the card that was at the top stays there.
        const root = {querySelectorAll: () => [before], querySelector: () => after};
        const before = {dataset: {mangaBook: '2'}, getBoundingClientRect: () => ({top: 30, bottom: 130})};
        const after = {dataset: {mangaBook: '2'}, getBoundingClientRect: () => ({top: 50, bottom: 150})};
        const host = {scrollTop: 100, getBoundingClientRect: () => ({top: 0, bottom: 500})};
        const anchor = PudgeLibraryShelves.captureScrollAnchor(root, host);
        PudgeLibraryShelves.restoreScrollAnchor(root, host, anchor);
        return {current: Number(scroller.dataset.seriesCurrentBook), height: scroller.style.height, top: scroller.scrollTop,
                anchor: anchor.key, hostTop: host.scrollTop};
        """,
        api={"manga_state": {"books": books, "ocr_available": False}},
    )
    assert result.value == {
        "current": 12,
        "height": "187px",
        "top": 97,
        "anchor": "manga-book:2",
        "hostTop": 120,
    }
    # Before the first measurement the shelf is already capped (no tall first paint).
    assert css_rules("index.html").get(".ln-series-books.series-scroll", "height") == "195px"


def test_native_launcher_embeds_python_in_the_pudge_process() -> None:
    # Force Quit showed Homebrew's Python (rocket icon) while the launcher exec()ed Python;
    # the generated launcher runs Python inside its own process, linked with --embed flags.
    history = release_history()
    sim, install = history.sim, history.steps["install"].result
    assert install.calls("python-config") == [["--embed", "--cflags"], ["--embed", "--ldflags"]]
    assert "-lpython3.12" in install.calls("clang")[0]
    launcher = sim.launcher()
    assert "#include <Python.h>" in launcher
    assert launcher.index("Py_InitializeFromConfig(&config)") < launcher.index("return Py_RunMain();")
    assert "execv(" not in launcher
