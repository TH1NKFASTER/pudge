"""Audit decisions, patch D3: structural performance changes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.process_priority import BACKGROUND_PRIORITY_ENV

ROOT = Path(__file__).resolve().parents[1]

_FAKE_MLX = """
import os
from pathlib import Path

LOADS = []

def transcribe(audio, path_or_hf_repo, language, word_timestamps, verbose):
    LOADS.append(path_or_hf_repo)
    name = Path(audio).name
    if name == "crash.flac":
        os._exit(9)
    if name == "fail.flac":
        raise RuntimeError("bad audio")
    import tqdm
    bar = tqdm.tqdm(total=4)
    bar.update(4)
    print("noise on stdout must not break the protocol")
    return {"segments": [{"start": 0.0, "end": 1.0, "text": name, "calls": len(LOADS)}]}
"""

_FAKE_TQDM = """
class tqdm:
    def __init__(self, *args, total=0, **kwargs):
        self.total = total
        self.n = 0
    def update(self, n=1):
        self.n += n
"""


def _fake_environment(tmp_path: Path) -> dict[str, str]:
    fake = tmp_path / "fake-modules"
    fake.mkdir(exist_ok=True)
    (fake / "mlx_whisper.py").write_text(_FAKE_MLX, encoding="utf-8")
    (fake / "tqdm.py").write_text(_FAKE_TQDM, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(fake), str(ROOT), env.get("PYTHONPATH", "")])
    return env


def _wait_answer(worker, request_id, timeout=30.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = worker.answer(0.25)
        if answer is None or (isinstance(answer, dict) and answer.get("id") == request_id):
            return answer
    raise AssertionError("no answer from the STT worker")


def test_one_stt_worker_serves_every_chunk_and_reports_crashes(tmp_path):
    from pudge.audiobooks import _SttWorker

    command = [sys.executable, "-m", "pudge.subtitles.stt_worker", "--serve", "tiny-model"]
    worker = _SttWorker(command, env=_fake_environment(tmp_path), stderr_path=tmp_path / "worker.log")
    try:
        for number in (1, 2):
            request = {
                "id": number,
                "audio": str(tmp_path / f"chunk-{number}.flac"),
                "output": str(tmp_path / f"chunk-{number}.json"),
                "progress": str(tmp_path / f"chunk-{number}.progress.json"),
                "words": True,
            }
            assert worker.submit(request)
            answer = _wait_answer(worker, number)
            assert answer["ok"] is True
            result = json.loads((tmp_path / f"chunk-{number}.json").read_text(encoding="utf-8"))
            # The same process: the second chunk sees the first call (the model stays loaded).
            assert result["segments"][0]["calls"] == number
            assert json.loads((tmp_path / f"chunk-{number}.progress.json").read_text())["percent"] == 100
        assert worker.submit({"id": 3, "audio": "fail.flac", "output": str(tmp_path / "x.json")})
        failed = _wait_answer(worker, 3)
        assert failed["ok"] is False and "bad audio" in failed["error"]
        assert worker.submit({"id": 4, "audio": "crash.flac", "output": str(tmp_path / "y.json")})
        assert _wait_answer(worker, 4) is None  # the worker died: end of its answers
        assert "noise on stdout" in worker.stderr_tail()
    finally:
        worker.kill()
    clean = _SttWorker(command, env=_fake_environment(tmp_path), stderr_path=tmp_path / "worker2.log")
    clean.stop()
    assert clean.process.returncode == 0


def _service(tmp_path):
    from pudge.audiobooks import AudiobookService
    from pudge.database import Database

    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _long_book(service, tmp_path):
    source = tmp_path / "long.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="Long",
        duration=650.0,
        files=[
            {"index": 0, "path": str(source), "title": "Long", "duration": 650.0, "start": 0.0, "end": 650.0}
        ],
        chapters=[{"index": 0, "title": "Long", "start": 0.0, "end": 650.0}],
    )
    return int(book["id"])


class _Worker:
    def __init__(self, crash_on=()):
        self.process = SimpleNamespace(poll=lambda: None)
        self.answers = []
        self.crash_on = set(crash_on)
        self.killed = False

    def submit(self, request):
        if request["id"] in self.crash_on:
            self.answers.append(None)
            return True
        Path(request["output"]).write_text(
            json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "x"}]}), encoding="utf-8"
        )
        self.answers.append({"id": request["id"], "ok": True})
        return True

    def answer(self, timeout):
        return self.answers.pop(0) if self.answers else False

    def stderr_tail(self):
        return "MLX crashed"

    def stop(self, timeout=15.0):
        pass

    def kill(self):
        self.killed = True


def test_a_crashed_worker_is_restarted_once_and_finished_chunks_are_kept(tmp_path, monkeypatch):
    service = _service(tmp_path)
    book_id = _long_book(service, tmp_path)
    cuts = []

    def cut(_source, destination, *, start, duration, cancel_event):
        destination.write_bytes(b"chunk")
        cuts.append((threading.current_thread().name, start))

    monkeypatch.setattr(service, "_extract_stt_chunk", cut)
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")
    workers = [_Worker(crash_on={2}), _Worker()]
    environments = []
    monkeypatch.setattr(
        service,
        "_start_stt_worker",
        lambda environment, path: environments.append(environment) or workers.pop(0),
    )
    output = service._transcript_path(book_id)
    service._transcription_cancel_events[book_id] = threading.Event()
    done = threading.Event()
    service._transcribe_worker(book_id, output, done)
    assert done.is_set() and output.is_file() and workers == []
    # An hours-long job: every worker is asked to run at low CPU priority.
    assert [environment.get(BACKGROUND_PRIORITY_ENV) for environment in environments] == ["1", "1"]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [row["start"] for row in payload["segments"]] == [1.0, 301.0, 601.0]
    # Chunks 2 and 3 were cut ahead on the extraction thread while the previous one was recognized.
    assert [name.startswith("audiobook-stt-cut") for name, _start in cuts] == [False, True, True]

    # Two crashes on the same chunk: an error that keeps the finished checkpoints.
    output.unlink()
    checkpoint = service._transcription_checkpoint_dir(output)
    (checkpoint / "chunk-00003.json").unlink()
    workers[:] = [_Worker(crash_on={3}), _Worker(crash_on={3})]
    service._transcription_cancel_events[book_id] = threading.Event()
    done = threading.Event()
    service._transcribe_worker(book_id, output, done)
    status = service.transcription_status(book_id)
    assert status["status"] == "error" and "MLX crashed" in status["error"]
    assert (checkpoint / "chunk-00001.json").is_file() and (checkpoint / "chunk-00002.json").is_file()
    assert not list(checkpoint.glob("*.flac"))


def test_serving_worker_runs_at_low_priority_and_rejects_unknown_arguments(tmp_path):
    from pudge.process_priority import BACKGROUND_NICE, mark_background

    service = _service(tmp_path)
    service.python, service.stt_model = sys.executable, "tiny-model"
    worker = service._start_stt_worker(mark_background(_fake_environment(tmp_path)), tmp_path / "worker.log")
    try:
        request = {
            "id": 1,
            "audio": str(tmp_path / "chunk-1.flac"),
            "output": str(tmp_path / "chunk-1.json"),
            "progress": str(tmp_path / "chunk-1.progress.json"),
            "words": True,
        }
        assert worker.submit(request)
        assert _wait_answer(worker, 1)["ok"] is True  # a serving worker: requests by JSON lines
        assert os.getpriority(os.PRIO_PROCESS, worker.process.pid) >= BACKGROUND_NICE
    finally:
        worker.stop()
    result = subprocess.run(
        [sys.executable, "-m", "pudge.subtitles.stt_worker", "--bogus"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    assert result.returncode == 2


def _alignment_arguments():
    sentences = [f"今日は{index}番目の文章を静かに読みます。" for index in range(60)]
    return dict(
        chapters=[{"chapter_index": 0, "title": "第一章", "text": "\n".join(sentences), "text_hash": "h"}],
        segments=[
            {"start": index * 3.0, "end": index * 3.0 + 2.8, "text": text}
            for index, text in enumerate(sentences)
        ],
        duration=200.0,
        model="tiny",
        speech_regions=[],
        chapter_start_reading_hints={0: []},
    )


def test_ln_alignment_runs_in_a_child_process_and_reuses_the_anchor_chain(tmp_path, monkeypatch):
    from pudge import audiobooks

    service = _service(tmp_path)
    monkeypatch.setattr(
        audiobooks,
        "align_light_novel_to_transcript",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("aligned in the window process")),
    )
    anchors = tmp_path / "work" / ".x.g1.anchors.json"
    first = service._align_reading_audio(
        _alignment_arguments(), ln_book_id=1, cancel_event=threading.Event(), anchor_cache=anchors
    )
    assert first["anchor_count"] > 8 and first["alignment_method"] == "exact-ngram"
    rows = json.loads((tmp_path / "work" / ".x.g1.anchors.json").read_text(encoding="utf-8"))
    assert len(rows) == 1  # the refinement pass of the same job reads the chain from here
    second = service._align_reading_audio(
        {**_alignment_arguments(), "chapter_start_segments": {0: []}},
        ln_book_id=1,
        cancel_event=threading.Event(),
        anchor_cache=anchors,
    )
    assert second["anchor_count"] == first["anchor_count"]
    assert not [path for path in anchors.parent.iterdir() if path.name.startswith(".align-")]
    assert service._alignment_processes == {}

    bad = {**_alignment_arguments(), "segments": []}
    try:
        service._align_reading_audio(bad, ln_book_id=1, cancel_event=threading.Event(), anchor_cache=anchors)
    except RuntimeError as exc:
        assert "too little Japanese text" in str(exc)
    else:
        raise AssertionError("an alignment error must reach the job")


def test_cancelling_stops_the_alignment_process(tmp_path):
    import time

    service = _service(tmp_path)
    sleeper = tmp_path / "slow-python"
    sleeper.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    sleeper.chmod(0o755)
    service.python = str(sleeper)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    try:
        service._align_reading_audio(
            _alignment_arguments(),
            ln_book_id=2,
            cancel_event=cancel,
            anchor_cache=tmp_path / "work" / "a.json",
        )
    except InterruptedError:
        pass
    else:
        raise AssertionError("cancelled alignment must stop")
    assert time.monotonic() - started < 5
    assert service._alignment_processes == {}


def _history(db, *, days=40, sessions=140, seed=7):
    import random
    import time as _time

    from pudge.consumption import ConsumptionLedger

    ledger = ConsumptionLedger(db)
    rng = random.Random(seed)
    media = []
    for index in range(8):
        kind = ["light_novel", "audiobook", "anime", "manga"][index % 4]
        metadata = (
            {"duration_seconds": 3600.0}
            if kind == "audiobook"
            else ({"character_count": 90000} if kind == "light_novel" else {})
        )
        row = ledger.ensure_media(
            kind=kind,
            title=f"T{index}",
            aliases=[("stable", f"m{index}")],
            source_revision="rev1",
            metadata=metadata,
        )
        media.append((row.media_uuid, kind))
    now = _time.time()
    moment = now - days * 86400
    position: dict[str, float] = {}
    for _ in range(sessions):
        media_uuid, kind = rng.choice(media)
        moment += rng.uniform(600, days * 86400 / sessions * 1.6)
        if moment > now - 7200:
            break
        if rng.random() < 0.2:
            position[media_uuid] = max(0.0, position.get(media_uuid, 0.0) - rng.uniform(0, 400))  # re-reading
        session = ledger.start_session(kind=kind, media_uuid=media_uuid, started_at_utc=moment)
        for _chunk in range(rng.randint(3, 12)):
            seconds = rng.uniform(15, 30)
            here = position.get(media_uuid, 0.0)
            if kind == "light_novel":
                chapter = int(here // 4000)
                start, end = (
                    {"chapter_index": chapter, "character_offset": here % 4000},
                    {"chapter_index": chapter, "character_offset": min(3999.0, here % 4000 + seconds * 5)},
                )
                after, payload = here + seconds * 5, {}
            elif kind == "manga":
                start = end = {"page_index": int(here)}
                after, payload = here + 1, {"page_count": 180}
            else:
                start, end = {"position_seconds": here}, {"position_seconds": here + seconds}
                after, payload = here + seconds, ({"duration_seconds": 1440.0} if kind == "anime" else {})
            position[media_uuid] = after
            ledger.append_interval(
                session_id=session,
                kind=kind,
                interval_start_utc=moment,
                interval_end_utc=moment + seconds,
                elapsed_monotonic_ms=seconds * 1000,
                media=[
                    {
                        "media_uuid": media_uuid,
                        "source_revision": "rev1",
                        "role": "primary",
                        "locator_start": start,
                        "locator_end": end,
                    }
                ],
                payload=payload,
            )
            moment += seconds
    return ledger, media


def _same_volume(left, right):
    import pytest

    assert set(left["by_media"]) == set(right["by_media"])
    for media_uuid, row in left["by_media"].items():
        other = right["by_media"][media_uuid]
        for name, value in row.items():
            if isinstance(value, float):
                assert other[name] == pytest.approx(value, abs=1e-6), (media_uuid, name)
            else:
                assert other[name] == value, (media_uuid, name)


def test_statistics_from_stored_history_summaries_match_a_full_scan(tmp_path):
    from pudge.consumption_statistics import VOLUME_ROLLUP_PREFIX, ConsumptionStatistics
    from pudge.database import Database

    db = Database(tmp_path / "db.sqlite3")
    ledger, media = _history(db)
    fast = ConsumptionStatistics(db)
    full = ConsumptionStatistics(db)
    full._prior_volume = lambda *args, **kwargs: None

    def check():
        for period in ("7d", "30d", "7d", "year", "30d"):
            _same_volume(full.query({"period": period})["volume"], fast.query({"period": period})["volume"])

    check()
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM state WHERE substr(key,1,26)=?", (VOLUME_ROLLUP_PREFIX,)
        ).fetchone()[0]
    assert stored >= 2
    # A late event from an offline phone (long ago) and a correction both make summaries stale.
    import time as _time

    old = _time.time() - 35 * 86400
    media_uuid, kind = next(row for row in media if row[1] == "audiobook")
    session = ledger.start_session(kind=kind, media_uuid=media_uuid, started_at_utc=old)
    ledger.append_interval(
        session_id=session,
        kind=kind,
        interval_start_utc=old,
        interval_end_utc=old + 25,
        elapsed_monotonic_ms=25000,
        media=[
            {
                "media_uuid": media_uuid,
                "source_revision": "rev1",
                "role": "primary",
                "locator_start": {"position_seconds": 3000.0},
                "locator_end": {"position_seconds": 3025.0},
            }
        ],
    )
    check()
    journal = fast.query({"period": "all"})["journal"]
    target = next(row for row in journal if row.get("type") == "session")
    fast.correction(target_type="session", target_id=str(target["session_id"]), excluded=True, reason="test")
    check()
    ledger.delete_history()
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM state WHERE substr(key,1,26)=?", (VOLUME_ROLLUP_PREFIX,)
            ).fetchone()[0]
            == 0
        )


def test_candidates_are_checked_in_parallel_only_on_mains_power(monkeypatch, tmp_path):
    from pudge import power_policy, syncing

    monkeypatch.setattr(syncing.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(syncing.os, "cpu_count", lambda: 8)
    state = {"on_battery": False, "thermal_limited": False, "mode": "normal"}
    monkeypatch.setattr(
        power_policy.PowerPolicy, "snapshot", lambda self, refresh=False: SimpleNamespace(**state)
    )
    monkeypatch.delenv("PUDGE_CANDIDATE_PROCESSES", raising=False)
    assert syncing._candidate_evaluation_workers(4) == 1  # only the preparation process opts in
    monkeypatch.setenv("PUDGE_CANDIDATE_PROCESSES", "auto")
    assert syncing._candidate_evaluation_workers(4) == 3
    assert syncing._candidate_evaluation_workers(2) == 2
    assert syncing._candidate_evaluation_workers(1) == 1
    state["on_battery"] = True
    assert syncing._candidate_evaluation_workers(4) == 1
    state.update(on_battery=False, mode="energy_saving")
    assert syncing._candidate_evaluation_workers(4) == 1
    state.update(mode="normal", thermal_limited=True)
    assert syncing._candidate_evaluation_workers(4) == 1


def _srt_file(path, text):
    path.write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
    return path


def test_parallel_candidate_checks_give_the_serial_result(monkeypatch, tmp_path):
    from pudge import syncing
    from pudge.config import SyncConfig
    from pudge.models import SubtitleCandidate

    files = [
        _srt_file(tmp_path / f"{name}.srt", text) for name, text in (("a", "一"), ("b", "二"), ("c", "一"))
    ]
    candidates = [
        SubtitleCandidate(path=path, name=path.name, source="jimaku", score=80.0, details={})
        for path in files
    ]
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *a, **k: (tmp_path / "ref.npz", {}))
    calls: list[str] = []
    monkeypatch.setattr(
        syncing,
        "synchronize_subtitle",
        lambda video, subtitle, *a, **k: (
            calls.append(subtitle.name)
            or (subtitle, {"sync_was_successful": True, "alignment_score": len(subtitle.name)})
        ),
    )
    captured = {}

    def stop(evaluated, **kwargs):
        captured["rows"] = [
            (row[0].name, row[1].name, row[2].get("alignment_reused_from")) for row in evaluated
        ]
        return evaluated[0], {"offsets_seconds": [], "offset_spread_seconds": 0}

    monkeypatch.setattr(syncing, "_exact_jimaku_audio_clock_consensus", stop)
    parallel_seen = []
    monkeypatch.setattr(syncing, "_candidate_evaluation_workers", lambda count: 2)
    monkeypatch.setattr(
        syncing,
        "_evaluate_candidates_in_processes",
        lambda video, items, *a, **k: (
            parallel_seen.extend(position for position, _c in items)
            or {items[0][0]: (items[0][1].path, {"sync_was_successful": True, "alignment_score": 9})}
        ),
    )
    syncing._optimize_candidates_unguarded(
        tmp_path / "v.mkv", candidates, tmp_path / "cache", SyncConfig(use_container_chapters=False)
    )
    # a and b go to the pool (c repeats a); b failed there and was checked serially.
    assert parallel_seen == [0, 1] and calls == ["b.srt"]
    assert captured["rows"] == [
        ("a.srt", "a.srt", None),
        ("b.srt", "b.srt", None),
        ("c.srt", "a.srt", "a.srt"),
    ]


def test_candidate_pool_runs_real_child_processes(tmp_path):
    from dataclasses import replace

    from pudge import syncing
    from pudge.config import SyncConfig
    from pudge.models import SubtitleCandidate

    files = [_srt_file(tmp_path / f"{name}.srt", name) for name in ("a", "b")]
    items = [
        (index, SubtitleCandidate(path=path, name=path.name, source="jimaku", score=1.0, details={}))
        for index, path in enumerate(files)
    ]
    config = replace(SyncConfig(), enabled=False)  # the checks return at once, through real processes
    results = syncing._evaluate_candidates_in_processes(
        tmp_path / "v.mkv",
        items,
        tmp_path / "cache",
        config,
        ffmpeg_path="ffmpeg",
        force=False,
        reference=tmp_path / "ref.npz",
        workers=2,
    )
    assert sorted(results) == [0, 1]
    assert results[1][0] == files[1] and results[1][1]["reason"] == "disabled"


def test_home_payload_has_no_duplicate_episodes_and_relation_trees_only_for_planning(tmp_path, page):
    from pudge.config import AppConfig, write_config
    from pudge.manager_models import LibraryAnime
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    related = [
        {
            "relation_type": "SEQUEL",
            "media_id": 2,
            "title": "Two",
            "site_url": "",
            "format": "TV",
            "season_year": 2021,
            "start_date": "2021-01-01",
            "episodes": 12,
            "relations": [],
        }
    ]
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=1,
            title="Watching",
            status="CURRENT",
            episodes=12,
            start_date="2020-01-01",
            season_year=2020,
            relations=related,
        )
    )
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=3,
            title="Planned",
            status="PLANNING",
            episodes=12,
            start_date="2020-01-01",
            season_year=2020,
            relations=related,
        )
    )
    state = api.get_state()
    assert "episodes" not in state and "downloaded" not in state  # episodes reach the window via "library"
    assert all(card["relations"] == {} for card in state["current"])
    for cards in state["home"].values():
        for card in cards if isinstance(cards, list) else []:
            assert not (card.get("relations") or {}), card.get("title")
    assert [
        item["media_id"] for level in state["planned"][0]["relations"]["sequel_levels"] for item in level
    ] == [2]
    state = json.loads(json.dumps(state))
    # The window draws the relation tree on Planning cards only, even for a Home card that has one.
    [home_card] = state["home"]["download_available"]
    home_card["relations"] = state["planned"][0]["relations"]
    result = page.run(
        """
        const out = {home: __text('#currentContent'), homeTrees: document.querySelectorAll('#currentContent .relation-diagram').length};
        setPage('planned', true); await __tick(50);
        out.planned = [...document.querySelectorAll('#plannedContent .anime-card')]
          .map(card => [card.dataset.title, card.querySelector('.relation-diagram')?.innerText ?? null]);
        return out;
        """,
        state=state,
    )
    assert "Watching" in result.value["home"] and result.value["homeTrees"] == 0
    [[title, tree]] = result.value["planned"]
    assert title == "Planned" and "Two" in tree


SHOW = {"media_id": 7, "title": "Show", "next_episode": 3, "site_url": "https://anilist.co/anime/7"}


def _library(subtitle_source):
    return [
        {
            "media_id": 7,
            "title": "Show",
            "episodes": [
                {
                    "episode": 3,
                    "state": "ready",
                    "subtitle_source": subtitle_source,
                    "video_path": "/anime/Show 03.mkv",
                }
            ],
        }
    ]


def test_window_redraws_when_only_a_library_episode_changes(page):
    # The foreground check compares the episodes in "library" (the Home payload has no
    # "episodes" list any more): subtitles found for episode 3 bring its card to Home.
    later = json.dumps({"library": _library("jimaku"), "home": {"waiting": [SHOW]}})
    result = page.run(
        """
        const before = __text('#currentContent');
        await __tick(1500);
        return {before, after: __text('#currentContent'), empty: t('home.emptyExplain')};
        """,
        state={"library": _library("")},
        api={"startup_maintenance": {"running": False}},
        before=f"__api.poll_downloads_and_subtitles = async () => ({{state: {{...(await __api.get_state()), ...{later}}}}});",
    )
    assert result.value["empty"] in result.value["before"] and "Show" not in result.value["before"]
    assert "Show" in result.value["after"]
