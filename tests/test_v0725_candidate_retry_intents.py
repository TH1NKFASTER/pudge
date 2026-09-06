from __future__ import annotations

from pathlib import Path

from pudge.cli import (
    _subtitle_candidate_set_fingerprint,
    build_parser,
    process_video,
)
from pudge.config import AppConfig
from pudge.download_intents import DownloadIntentStore
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryEpisode
from pudge.models import SubtitleCandidate


class FakeStateDb:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def delete_state(self, key: str) -> None:
        self.state.pop(key, None)


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg, log=lambda _message: None)


def test_download_intent_complete_if_present_does_not_create_missing() -> None:
    db = FakeStateDb()
    store = DownloadIntentStore(db)

    assert store.complete_if_present(10, 3, False) is False
    assert store.get(10, 3, False) is None

    store.begin(10, 3, False, [], backend="aria2")
    store.update(10, 3, False, state="downloading", backend="aria2")
    assert store.complete_if_present(10, 3, False) is True

    payload = store.get(10, 3, False)
    assert payload is not None
    assert payload["state"] == "complete"
    assert payload["detail"] == "Download completed"

    persisted = dict(payload)
    assert store.complete_if_present(10, 3, False) is False
    assert store.get(10, 3, False) == persisted


def test_manager_completed_download_closes_existing_intent(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.download_intents.begin(190569, 9, False, [], backend="aria2")
    manager.download_intents.update(190569, 9, False, state="downloading")
    item = DownloadItem(
        torrent_hash="abc",
        name="Tenmaku no Jaadugar - 09.mkv",
        state="complete",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Tenmaku no Jaadugar - 09.mkv"),
        media_id=190569,
        episode=9,
        media_episode=9,
    )

    assert manager._complete_download_intent(item) is True
    payload = manager.download_intents.get(190569, 9, False)
    assert payload is not None
    assert payload["state"] == "complete"
    assert manager._complete_download_intent(item) is False


def _candidate(path: Path, name: str, body: str) -> SubtitleCandidate:
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n" + body + "\n",
        encoding="utf-8",
    )
    return SubtitleCandidate(
        path=path,
        source="jimaku",
        score=100.0,
        name=name,
        episode=9,
        verified_japanese=True,
        details={"url": f"https://example.test/{path.name}"},
    )


def test_candidate_set_fingerprint_is_order_independent_and_changes_for_new_file(
    tmp_path: Path,
) -> None:
    one = _candidate(tmp_path / "one.srt", "one", "字幕一")
    two = _candidate(tmp_path / "two.srt", "two", "字幕二")
    three = _candidate(tmp_path / "three.srt", "three", "字幕三")

    first = _subtitle_candidate_set_fingerprint([one, two])
    assert first == _subtitle_candidate_set_fingerprint([two, one])
    assert first != _subtitle_candidate_set_fingerprint([one, two, three])


def test_prepare_only_skips_alignment_when_candidate_set_is_unchanged(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    video = tmp_path / "Tenmaku no Jaadugar - 09.mkv"
    video.write_bytes(b"video")
    one = _candidate(tmp_path / "one.srt", "one", "字幕一")
    two = _candidate(tmp_path / "two.srt", "two", "字幕二")
    fingerprint = _subtitle_candidate_set_fingerprint([one, two])

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.matching.local_min_score = 0.0
    cfg.matching.evaluate_all_jimaku = True
    cfg.sync.enabled = True

    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr("pudge.cli.find_local_subtitles", lambda **_kwargs: [one, two])

    def should_not_align(*_args, **_kwargs):
        raise AssertionError("alignment should be skipped for unchanged candidates")

    monkeypatch.setattr("pudge.cli.optimize_candidates", should_not_align)
    args = build_parser().parse_args(
        [
            "--prepare-only",
            "--offline",
            "--previous-candidate-fingerprint",
            fingerprint,
            str(video),
        ]
    )

    assert process_video(video, args, cfg, None) == 4
    stdout = capsys.readouterr().out
    assert f"SUBTITLE_CANDIDATE_FINGERPRINT={fingerprint}" in stdout
    assert "PREPARE_STATUS=waiting_unchanged_candidates" in stdout


def _queued_manager(tmp_path: Path, attempts: int) -> tuple[AnimeManager, Path]:
    manager = make_manager(tmp_path)
    video = manager.config.library.root_dir / "Tenmaku no Jaadugar - 09.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=190569,
            title="Tenmaku no Jaadugar",
            episode=9,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 190569, 9)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=?,next_check=0 WHERE video_path=?",
            (attempts, str(video.resolve())),
        )
    return manager, video.resolve()


def test_unchanged_candidate_probe_preserves_attempt_count(tmp_path: Path, monkeypatch) -> None:
    manager, video = _queued_manager(tmp_path, attempts=5)
    key = manager._subtitle_candidate_fingerprint_state_key(video)
    manager.db.set_state(key, "same")
    calls: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            calls.append(command)
            self.returncode = 4

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                "SUBTITLE_CANDIDATE_FINGERPRINT=same\nPREPARE_STATUS=waiting_unchanged_candidates\n",
                "",
            )

    # Exercise the macOS-only battery/thermal probes too. subprocess.run()
    # uses the same patched Popen object, so pmset commands may appear before
    # the actual subtitle worker command.
    monkeypatch.setattr("pudge.work_scheduler.sys.platform", "darwin")
    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 0

    job = manager.db.subtitle_jobs()[0]
    assert int(job["attempts"]) == 5
    prepare_calls = [command for command in calls if "--prepare-only" in command]
    assert len(prepare_calls) == 1
    command = prepare_calls[0]
    idx = command.index("--previous-candidate-fingerprint")
    assert command[idx + 1] == "same"


def test_new_candidate_set_resets_retry_backoff_before_failed_full_try(tmp_path: Path, monkeypatch) -> None:
    manager, video = _queued_manager(tmp_path, attempts=5)
    key = manager._subtitle_candidate_fingerprint_state_key(video)
    manager.db.set_state(key, "old")

    class FakeProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 4

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                "SUBTITLE_CANDIDATE_FINGERPRINT=new\n"
                "Subtitles rejected by quality validation\n"
                "PREPARE_STATUS=waiting_subtitles\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 0

    job = manager.db.subtitle_jobs()[0]
    assert int(job["attempts"]) == 1
    assert manager.db.get_state(key, "") == "new"
