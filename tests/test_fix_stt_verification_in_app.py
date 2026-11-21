"""Japanese speech verification from the app (Black Clover S2 02 went "Action required").

Opened from the Dock, Pudge has no Homebrew folder on PATH, and MLX Whisper runs "ffmpeg"
by name: every STT check in the app failed with "No such file or directory: 'ffmpeg'"
(the background agent, which has Homebrew on PATH, succeeded later). And the first such
failure already marked the episode "Action required", because the job's attempts also
counted the dozens of earlier "no subtitles on Jimaku yet" retries.
"""

from __future__ import annotations

from pathlib import Path

from pudge.subtitles import stt


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_speech_worker_finds_the_ffmpeg_pudge_uses(tmp_path: Path, monkeypatch) -> None:
    # FFmpeg as Homebrew installs it: Pudge knows its full path, PATH does not list it.
    ffmpeg = _script(
        tmp_path / "homebrew" / "bin" / "ffmpeg", 'for last; do :; done\nprintf flac > "$last"\n'
    )
    segments = ",".join(f'{{"start":{n},"end":{n}.5,"text":"line {n}"}}' for n in range(10))
    # The STT worker as MLX Whisper behaves: it needs "ffmpeg" on PATH to read the audio.
    worker = _script(
        tmp_path / "python",
        '[ "$3" = --check ] && exit 0\n'
        "command -v ffmpeg >/dev/null || "
        "{ echo \"MLX Whisper failed: [Errno 2] No such file or directory: 'ffmpeg'\" >&2; exit 4; }\n"
        f"printf '%s' '{{\"segments\":[{segments}]}}' > \"$5\"\n",
    )
    monkeypatch.setenv("PUDGE_PYTHON", str(worker))
    monkeypatch.setenv("PATH", str(tmp_path / "no-homebrew"))
    video = tmp_path / "Show - 02.mkv"
    video.write_bytes(b"video" * 1000)

    reference, info = stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path=str(ffmpeg), model="model", timeout_seconds=60
    )

    assert (info.get("reason"), info.get("error", "")) == (None, "")
    assert reference is not None and "line 9" in reference.read_text(encoding="utf-8")


def _manager(tmp_path: Path):
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(config, log=lambda *_a: None)


def test_earlier_no_subtitle_retries_do_not_count_against_speech_verification(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge.manager_models import LibraryEpisode

    manager = _manager(tmp_path)
    video = (tmp_path / "Show - 02.mkv").resolve()
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=2, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, 1, 2)
    with manager.db.connect() as conn:
        # A week of "no subtitles on Jimaku yet" before they appeared.
        conn.execute("UPDATE subtitle_jobs SET attempts=40")

    class SpeechWorkerFailed:
        returncode = 4

        def __init__(self, *_a, **_k):
            pass

        def poll(self):
            return 4

        def communicate(self):
            return (
                'PREPARED_SUBTITLE_META={"alignment":{"stt":{"reason":"stt_worker_failed"}}}\n'
                "PREPARE_STATUS=waiting_verification\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", SpeechWorkerFailed)
    states = []
    for _ in range(3):
        manager.process_subtitle_jobs(limit=1)
        states.append(manager.db.subtitle_jobs()[0]["state"])
        with manager.db.connect() as conn:
            conn.execute("UPDATE subtitle_jobs SET next_check=0")
    # Two more tries, as for an episode whose subtitles were there from the start.
    assert states == ["pending", "pending", "needs_action"]
