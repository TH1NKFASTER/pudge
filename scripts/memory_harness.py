"""Idle memory harness: start the app's real services on an invented library and report memory.

Run from a checkout (no network, no window, nothing outside a temporary folder is touched):

    PYTHONPATH=$PWD python scripts/memory_harness.py [--anime 150] [--episodes 4] [--books 20]
        [--words 10000] [--polls 120] [--top 25] [--no-trace] [--keep]

It seeds a library (anime + episode files, light novels, cached Jiten words, cover images),
constructs ``WebAppApi`` like the window does, then replays the idle traffic of an open window
(state polls, Home/Library/LN/manga/statistics loads) and prints, before and after the idle phase:
RSS, the tracemalloc top allocators, heavy modules in ``sys.modules`` and the sizes of the caches
``pudge.memory_diagnostics`` knows about (the same report the hourly ``EVENT memory`` line logs).
"""

from __future__ import annotations

import argparse
import gc
import os
import random
import socket
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HEAVY_MODULES = (
    "numpy",
    "scipy",
    "PIL.Image",
    "mlx",
    "whisper",
    "torch",
    "anitopy",
    "rapidfuzz",
    "ffsubsync",
    "pysubs2",
    "webrtcvad",
    "auditok",
    "pudge.whisper_mlx",
    "pudge.manga_align",
    "pudge.ocr",
    "pudge.visual_novels",
)


def _isolate() -> Path:
    root = Path(tempfile.mkdtemp(prefix="pudge-memory-harness."))
    for name in ("home", "pudge-home", "tmp"):
        (root / name).mkdir()
    os.environ.update(
        HOME=str(root / "home"),
        PUDGE_HOME=str(root / "pudge-home"),
        TMPDIR=str(root / "tmp"),
        PUDGE_RUNTIME_LOG_PATH=str(root / "runtime.log"),
        PUDGE_ANIME_MAPPINGS="0",
        PUDGE_SEADEX="0",
        PUDGE_INTRO_DETECTION="0",
    )
    tempfile.tempdir = None

    def refuse(*_args, **_kwargs):
        raise OSError("memory harness: network disabled")

    socket.create_connection = refuse  # type: ignore[assignment]
    socket.socket.connect = refuse  # type: ignore[assignment]
    return root


def _seed(root: Path, *, anime: int, episodes: int, books: int, words: int) -> Path:
    from pudge.config import AppConfig, write_config
    from pudge.database import Database
    from pudge.manager_models import LibraryAnime, LibraryEpisode

    rng = random.Random(7)
    cfg = AppConfig()
    cfg.config_path = root / "config.toml"
    cfg.library.database_path = root / "library.sqlite3"
    cfg.library.cover_cache_dir = root / "cache" / "covers"
    cfg.paths.cache_dir = root / "cache"
    cfg.library.root_dir = root / "library"
    cfg.ui.review_gate_enabled = False
    write_config(cfg, cfg.config_path)
    cfg.library.cover_cache_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.library.database_path)
    statuses = ("CURRENT", "PLANNING", "COMPLETED", "PAUSED", "DROPPED")
    for media_id in range(1000, 1000 + anime):
        title = f"Invented Show {media_id} " + " ".join(rng.choice("kmnstr") * 3 for _ in range(3))
        db.upsert_anime(
            LibraryAnime(
                media_id,
                title,
                titles=[title, f"Show {media_id}"],
                synonyms=[f"S{media_id}"],
                cover_url=f"https://example.invalid/cover/{media_id}.jpg",
                status=statuses[media_id % len(statuses)],
                progress=media_id % 6,
                episodes=12,
                format="TV",
                season_year=2010 + media_id % 15,
                media_status="FINISHED",
                duration=24,
            )
        )
        folder = cfg.library.root_dir / f"Invented Show {media_id}"
        folder.mkdir(parents=True, exist_ok=True)
        for number in range(1, episodes + 1):
            video = folder / f"Invented Show {media_id} - {number:02d} [1080p].mkv"
            video.write_bytes(b"x")
            db.upsert_episode(LibraryEpisode(media_id, title, number, video, state="local"))
    try:
        from PIL import Image

        for media_id in range(1000, 1000 + anime):
            Image.new("RGB", (460, 650), (media_id % 255, 80, 120)).save(
                cfg.library.cover_cache_dir / f"{media_id}.jpg", quality=80
            )
    except Exception:  # noqa: BLE001 - covers are optional for the measurement
        pass
    db.close() if hasattr(db, "close") else None
    from pudge.light_novels import LightNovelService

    service = LightNovelService(cfg, logger=None)
    book_dir = root / "books"
    book_dir.mkdir()
    sentence = "これは発明された文章です。物語は静かに進み、主人公は駅へ歩いた。"
    for index in range(books):
        source = book_dir / f"Invented Novel {index} v{index % 5 + 1}.txt"
        source.write_text("\n\n".join(sentence * 40 for _ in range(60)), encoding="utf-8")
        try:
            service.import_file(source) if hasattr(service, "import_file") else None
        except Exception:  # noqa: BLE001 - a book that cannot import still leaves the others
            pass
    from pudge.jiten_words import JitenWordRepository

    repo = JitenWordRepository(cfg.library.database_path)
    batch = []
    for index in range(words):
        batch.append(
            {
                "wordId": 100000 + index,
                "readingIndex": 0,
                "spelling": f"語{index}",
                "reading": f"ご{index}",
                "meanings": [f"invented meaning {index}", "second gloss"],
                "partsOfSpeech": ["noun"],
            }
        )
        if len(batch) == 500:
            repo.put_many("harness", batch)
            batch = []
    if batch:
        repo.put_many("harness", batch)
    repo.close()
    service.close() if hasattr(service, "close") else None
    return cfg.config_path


def _rss_mb() -> float:
    from pudge.memory_diagnostics import rss_mb

    return rss_mb()


def _heavy() -> list[str]:
    return [name for name in HEAVY_MODULES if name in sys.modules]


def _report(label: str, api, top: int, snapshot_before=None):
    from pudge.memory_diagnostics import cache_sizes

    gc.collect()
    snap = None
    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot().filter_traces((tracemalloc.Filter(False, tracemalloc.__file__),))
        current, peak = tracemalloc.get_traced_memory()
        traced = (
            f" traced_mb={current / 2**20:.1f} peak_mb={peak / 2**20:.1f} (rss includes tracemalloc's own)"
        )
    else:
        traced = ""
    print(f"\n=== {label}: rss_mb={_rss_mb():.1f}{traced}")
    print(f"heavy modules loaded: {', '.join(_heavy()) or '-'}  modules={len(sys.modules)}")
    if snap is not None:
        stats = snap.compare_to(snapshot_before, "lineno") if snapshot_before else snap.statistics("lineno")
        print(f"top allocators ({'growth since start' if snapshot_before else 'total'}):")
        for stat in stats[:top]:
            frame = stat.traceback[0]
            size = getattr(stat, "size_diff", stat.size) if snapshot_before else stat.size
            print(f"  {size / 2**20:8.2f} MB  {frame.filename.split('/pudge/')[-1]}:{frame.lineno}")
    sizes = cache_sizes(api, deep=True)
    print("caches (entries / approx MB):")
    for name, (count, size_mb) in sorted(sizes.items(), key=lambda item: -item[1][1])[:top]:
        print(f"  {size_mb:8.2f} MB  {count:7d}  {name}")
    return snap


def _idle(api, polls: int) -> None:
    calls = (
        ("get_state_fast", ()),
        ("ui_state_versions", ()),
        ("poll_downloads_and_subtitles", ("", False, True)),
        ("torrent_traffic_status", ()),
        ("sidebar_audio_state", ()),
        ("power_status", ()),
        ("light_novel_state", ()),
        ("manga_state", ()),
        ("audiobook_state", ()),
        ("statistics_words_state", ()),
    )
    for index in range(polls):
        for name, args in calls:
            method = getattr(api, name, None)
            if method is None:
                continue
            try:
                method(*args)
            except Exception as exc:  # noqa: BLE001 - keep replaying the rest
                if index == 0:
                    print(f"  ({name} failed: {exc!r})")
        if index % 20 == 0:
            try:
                api._bump_ui_state_version()
                api.get_state()
            except Exception as exc:  # noqa: BLE001
                if index == 0:
                    print(f"  (get_state failed: {exc!r})")
            try:
                api.consumption_statistics_query({})
            except Exception:  # noqa: BLE001
                pass


def _maintenance(api) -> None:
    """The launch pass the page starts (AniList is offline here: it fails fast)."""
    try:
        api.startup_maintenance()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and api.startup_maintenance_status("x").get("running"):
            time.sleep(0.5)
    except Exception as exc:  # noqa: BLE001
        print(f"  (startup_maintenance failed: {exc!r})")


def _reading_session(api, *, chapters: int = 30, words_per_chapter: int = 8000) -> None:
    """What reading light novels leaves behind (simulated, no Jiten): word states of the
    words met and the word positions Statistics reads for the chapters read."""
    now = time.monotonic()
    cache = api.light_novels._jiten_live_state_cache
    for index in range(20_000):
        cache[("harness", 100_000 + index, 0)] = (
            now - 3600 + index * 0.01,
            {"wordId": 100_000 + index, "readingIndex": 0, "knowledgeState": "young", "deckIds": [1, 2]},
        )
    positions = {
        f"chapter-{n}": [(offset * 3, 100_000 + offset, 0) for offset in range(words_per_chapter)]
        for n in range(chapters)
    }
    stats = api.consumption_statistics
    real, stats.ln_words = stats.ln_words, positions.get
    for chapter in positions:
        stats._chapter_words(chapter)
    stats.ln_words = real


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--anime", type=int, default=150)
    parser.add_argument("--episodes", type=int, default=4)
    parser.add_argument("--books", type=int, default=20)
    parser.add_argument("--words", type=int, default=10000)
    parser.add_argument("--polls", type=int, default=120)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--keep", action="store_true", help="keep the invented library folder")
    parser.add_argument("--no-trace", action="store_true", help="RSS only (tracemalloc adds ~2x)")
    args = parser.parse_args(argv)
    root = _isolate()
    print(f"harness folder: {root}")
    start = _rss_mb()
    config_path = _seed(root, anime=args.anime, episodes=args.episodes, books=args.books, words=args.words)
    from pudge.memory_diagnostics import release_idle_memory

    if not args.no_trace:
        tracemalloc.start(1)
    import pudge.web_app as web_app

    print(f"rss before import={start:.1f} after import+seed={_rss_mb():.1f}")
    api = web_app.WebAppApi(config_path)
    base = _report("after WebAppApi()", api, args.top)
    t0 = time.monotonic()
    _maintenance(api)
    _reading_session(api)
    _idle(api, args.polls)
    print(
        f"\nmaintenance + reading session + idle replay: {args.polls} polls in {time.monotonic() - t0:.1f}s"
    )
    _report("after idle replay", api, args.top, base)
    released = release_idle_memory(api, force=True)
    print(f"\nrelease_idle_memory: {released}")
    _report("after release_idle_memory", api, args.top, base)
    from pudge.memory_diagnostics import MemoryReporter

    print(f"\nEVENT memory line: {MemoryReporter(api, None).log_once('harness')}")
    try:
        api.close()
    except Exception:  # noqa: BLE001
        pass
    if not args.keep:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
