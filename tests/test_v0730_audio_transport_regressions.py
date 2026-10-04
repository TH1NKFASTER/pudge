from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.player import build_mpv_command


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe unavailable")
@pytest.mark.parametrize("directory", ["audio", "reader's audio"])
def test_precision_window_extracts_across_file_boundary(tmp_path: Path, monkeypatch, directory: str) -> None:
    # A literal backslash-n or unescaped apostrophe makes real concat fail.
    folder = tmp_path / directory
    folder.mkdir()
    files = []
    for index, frequency in enumerate((440, 880)):
        source = folder / f"part-{index}.flac"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
             f"sine=frequency={frequency}:sample_rate=16000:duration=1", "-c:a", "flac", str(source)],
            check=True,
            capture_output=True,
        )
        files.append({"file_index": index, "path": str(source), "start": float(index), "end": float(index + 1)})
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_file_rows", lambda _book_id: files)
    destination = folder / "boundary.flac"

    start, duration = service._extract_global_audio_window(7, destination, start=0.75, duration=0.75)

    assert (start, duration) == pytest.approx((0.75, 0.75))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(destination)],
        check=True, capture_output=True, text=True,
    )
    assert float(json.loads(probe.stdout)["format"]["duration"]) == pytest.approx(0.75, abs=0.01)
    assert not list(folder.glob("pudge-chapter-start-parts-*"))


class _IPCSocket:
    """Supply exact stream read boundaries that real Unix sockets may expose."""

    def __init__(self, replies, *, clock=None) -> None:
        self.replies = replies
        self.clock = clock
        self.chunks = []
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, _path):
        return None

    def sendall(self, payload):
        request = json.loads(payload)
        self.chunks = list(self.replies(request.get("request_id", 0)))

    def recv(self, _size):
        if self.clock is not None:
            self.clock[0] += 0.35
        return self.chunks.pop(0) if self.chunks else b""


@pytest.mark.parametrize("framing", ["fragmented", "events-and-other-reply"])
def test_ipc_returns_only_matching_complete_reply(tmp_path: Path, monkeypatch, framing: str) -> None:
    # One recv is neither one full JSON message nor necessarily the reply.
    def replies(request_id):
        expected = json.dumps({"request_id": request_id, "error": "success", "data": 42.5}).encode() + b"\n"
        if framing == "fragmented":
            return [expected[:7], expected[7:19], expected[19:]]
        return [
            b'{"event":"property-change","data":13}\n'
            + json.dumps({"request_id": request_id + 1, "error": "success", "data": 99}).encode() + b"\n"
            + expected
        ]

    client = _IPCSocket(replies)
    monkeypatch.setattr("pudge.audiobooks.socket.socket", lambda *_args: client)

    response = AudiobookService._ipc_command(tmp_path / "book.sock", ["get_property", "time-pos"])

    assert response is not None
    assert response["error"] == "success"
    assert response["data"] == 42.5
    assert isinstance(response["request_id"], int)


def test_ipc_does_not_accept_an_unsolicited_event_as_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    client = _IPCSocket(lambda _request_id: [b'{"event":"seek"}\n', b""])
    monkeypatch.setattr("pudge.audiobooks.socket.socket", lambda *_args: client)

    assert AudiobookService._ipc_command(tmp_path / "book.sock", ["seek", 70, "absolute"]) is None


def test_ipc_bounds_total_reply_bytes(tmp_path: Path, monkeypatch) -> None:
    client = _IPCSocket(lambda request_id: [
        json.dumps({"request_id": request_id, "error": "success", "data": "x" * (1024 * 1024)}).encode() + b"\n"
    ])
    monkeypatch.setattr("pudge.audiobooks.socket.socket", lambda *_args: client)

    assert AudiobookService._ipc_command(tmp_path / "book.sock", ["get_property", "time-pos"]) is None


def test_ipc_events_cannot_extend_total_timeout(tmp_path: Path, monkeypatch) -> None:
    clock = [10.0]
    client = _IPCSocket(lambda _request_id: [b'{"event":"tick"}\n'] * 20, clock=clock)
    monkeypatch.setattr("pudge.audiobooks.socket.socket", lambda *_args: client)
    monkeypatch.setattr("pudge.audiobooks.time.monotonic", lambda: clock[0])

    assert AudiobookService._ipc_command(tmp_path / "book.sock", ["get_property", "time-pos"]) is None
    assert clock[0] <= 11.1
    assert client.timeouts[-1] < client.timeouts[0]


class _Player:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def _live_book(tmp_path: Path, monkeypatch, *, multifile: bool = False, paused: bool = False):
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_probe", lambda _path: (100.0, []))
    monkeypatch.setattr(service, "prepare_transcription", lambda *_args, **_kwargs: {"status": "queued", "ready": False})
    monkeypatch.setattr(service, "_tracked_thread", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_tempo_filter_args", lambda: ())
    folder = tmp_path / "book"
    folder.mkdir()
    for index in range(2 if multifile else 1):
        (folder / f"{index:02d}.mp3").write_bytes(b"audio")
    book_id = service.import_folder(folder)["id"]
    service.set_position(book_id, 20.0)
    process = _Player()
    ipc_path = tmp_path / "book.sock"
    service._players[book_id] = process
    service._ipc_paths[book_id] = ipc_path
    service._playback_sessions[book_id] = "before-seek"
    service._last_positions[book_id] = 20.0
    live = {"time-pos": 20.0, "playlist-pos": 0, "pause": paused, "speed": 1.75}

    def command(_path, payload):
        if payload[0] == "seek":
            live["time-pos"] = float(payload[1]) + (float(live["time-pos"]) if payload[2] == "relative" else 0.0)
        elif payload[0] == "set_property":
            live[payload[1]] = payload[2]
        return {"request_id": 1, "error": "success", "data": None}

    monkeypatch.setattr(service, "_ipc_command", command)
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: live.get(name))
    monkeypatch.setattr(service, "_ipc_commands_no_wait", lambda _path, _commands: True)
    return service, book_id, process, ipc_path, live


@pytest.mark.parametrize("control", ["absolute", "relative"])
def test_seek_then_immediate_stop_persists_accepted_position(tmp_path: Path, monkeypatch, control: str) -> None:
    # Stop must consume the same position that seek publishes to the UI/DB.
    service, book_id, _process, _ipc_path, _live = _live_book(tmp_path, monkeypatch)

    result = service.seek_to(book_id, 70.0) if control == "absolute" else service.seek(book_id, 50.0)
    assert result["book"]["position"] == 70.0
    service.stop(book_id)

    assert service.book(book_id)["position"] == 70.0


def test_relative_seek_publishes_target_before_live_clock_catches_up(tmp_path: Path, monkeypatch) -> None:
    service, book_id, _process, _ipc_path, _live = _live_book(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_ipc_command", lambda _path, _command: {"request_id": 1, "error": "success", "data": None})

    result = service.seek(book_id, 50.0)
    service.stop(book_id)

    assert result["book"]["position"] == 70.0
    assert service.book(book_id)["position"] == 70.0


def test_monitor_discards_position_read_started_before_seek(tmp_path: Path, monkeypatch) -> None:
    service, book_id, process, ipc_path, _live = _live_book(tmp_path, monkeypatch)

    def delayed_position(_book_id, _ipc_path):
        # The old 20s read completes only after a successful seek to 70s.
        service.seek_to(book_id, 70.0)
        process.returncode = 0
        return 20.0

    monkeypatch.setattr(service, "_global_position", delayed_position)

    service._monitor(book_id, process, ipc_path, "before-seek")

    assert service.book(book_id)["position"] == 70.0


def test_monitor_keeps_accepted_seek_until_delayed_clock_confirms(tmp_path: Path, monkeypatch) -> None:
    service, book_id, process, ipc_path, live = _live_book(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_ipc_command", lambda *_args: {"error": "success"})
    service.seek_to(book_id, 70.0)

    def one_sample(*_args):
        process.returncode = 0
        return live["time-pos"]

    monkeypatch.setattr(service, "_global_position", one_sample)
    service._monitor(book_id, process, ipc_path, "before-seek")

    assert service.book(book_id)["position"] == 70.0


def test_paired_reader_waits_for_post_ack_clock_then_resumes_tracking(tmp_path: Path, monkeypatch) -> None:
    service, book_id, _process, _ipc_path, live = _live_book(tmp_path, monkeypatch, paused=True)
    with service.db.connect() as conn:
        conn.execute("CREATE TABLE ln_chapters (book_id INTEGER)")
    monkeypatch.setattr(service, "link_for_light_novel", lambda *_args, **_kwargs: {"book": service.book(book_id)})
    monkeypatch.setattr(service, "_load_alignment", lambda *_args: None)
    monkeypatch.setattr(service, "alignment_status", lambda *_args: {"status": "not_started", "ready": False})
    monkeypatch.setattr(service, "_ipc_command", lambda *_args: {"error": "success"})
    service.seek_to(book_id, 70.0)

    assert service.paired_state(9)["position"] == 70.0
    assert service.paired_state(9)["position"] == 70.0
    live["time-pos"] = 70.0
    assert service.paired_state(9)["position"] == 70.0
    live["time-pos"] = 72.5
    assert service.paired_state(9)["position"] == 72.5


def test_seek_clock_guard_expires_when_player_never_confirms(tmp_path: Path, monkeypatch) -> None:
    service, book_id, _process, ipc_path, _live = _live_book(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("pudge.audiobooks.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "_ipc_command", lambda *_args: {"error": "success"})
    service.seek_to(book_id, 70.0)

    assert service._reconcile_startup_position(book_id, ipc_path, 20.0) == 70.0
    clock[0] += 7.0
    assert service._reconcile_startup_position(book_id, ipc_path, 20.0) == 20.0


def test_cross_file_restart_repairs_unhonored_start_before_clock_guard(tmp_path: Path, monkeypatch) -> None:
    service, book_id, _process, _ipc_path, _live = _live_book(tmp_path, monkeypatch, multifile=True, paused=True)
    clock, commands = [100.0], []
    monkeypatch.setattr("pudge.audiobooks.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("pudge.audiobooks.subprocess.Popen", lambda *_args, **_kwargs: _Player())

    def delayed_ack(_path, command):
        commands.append(command)
        return {"error": "success"}

    monkeypatch.setattr(service, "_ipc_command", delayed_ack)
    assert service.seek_to(book_id, 125.0)["book"]["position"] == 125.0
    commands.clear()
    ipc_path = service._ipc_paths[book_id]

    assert service._reconcile_startup_position(book_id, ipc_path, 20.0) == 125.0
    assert ["playlist-play-index", 1] in commands
    assert ["seek", 25.0, "absolute", "exact"] in commands
    assert service._reconcile_startup_position(book_id, ipc_path, 20.0) == 125.0
    assert service._reconcile_startup_position(book_id, ipc_path, 125.0) == 125.0
    assert service._reconcile_startup_position(book_id, ipc_path, 126.0) == 126.0


def test_monitor_exit_does_not_replace_new_seek_with_its_previous_sample(tmp_path: Path, monkeypatch) -> None:
    service, book_id, process, ipc_path, _live = _live_book(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_global_position", lambda *_args: 20.0)

    def exit_after_seek(_book_id, _position):
        service.seek_to(book_id, 70.0)
        process.returncode = 0
        return False

    monkeypatch.setattr(service, "_sleep_reached", exit_after_seek)

    service._monitor(book_id, process, ipc_path, "before-seek")

    assert service.book(book_id)["position"] == 70.0


def test_paired_reader_discards_position_read_started_before_seek(tmp_path: Path, monkeypatch) -> None:
    service, book_id, _process, _ipc_path, _live = _live_book(tmp_path, monkeypatch, paused=True)
    with service.db.connect() as conn:
        conn.execute("CREATE TABLE ln_chapters (book_id INTEGER)")
    monkeypatch.setattr(service, "link_for_light_novel", lambda *_args, **_kwargs: {"book": service.book(book_id)})
    monkeypatch.setattr(service, "_load_alignment", lambda *_args: None)
    monkeypatch.setattr(service, "alignment_status", lambda *_args: {"status": "not_started", "ready": False})

    def delayed_position(_book_id, _ipc_path):
        service.seek_to(book_id, 70.0)
        return 20.0

    monkeypatch.setattr(service, "_global_position", delayed_position)

    result = service.paired_state(9)

    assert result["position"] == 70.0
    assert service.book(book_id)["position"] == 70.0


@pytest.mark.parametrize("transition,paused", [("cross-file", True), ("cross-file", False), ("relative-cross-file", True), ("seek-error", True)])
def test_seek_restart_preserves_transport_context(tmp_path: Path, monkeypatch, transition: str, paused: bool) -> None:
    service, book_id, _process, _ipc_path, live = _live_book(tmp_path, monkeypatch, multifile=True, paused=paused)
    service.set_speed(book_id, 1.75)
    service.set_sleep_timer(book_id, seconds=60.0)
    deadline = service._sleep_deadlines[book_id]
    # Chapter-end timers and paired review state survive the same transition.
    service._sleep_chapter_ends[book_id] = 160.0
    context = {"origin": "ln", "consumer": "ln", "ln_book_id": 9, "chapter": 1,
               "ranges": [{"chapter_index": 1, "start": 100.0, "end": 160.0}],
               "pending_chapter": 2, "session": "before-seek"}
    service._review_contexts[book_id] = dict(context)
    launches = []

    def launch(command, **_kwargs):
        launches.append(list(command))
        live["pause"] = "--pause=yes" in command
        live["speed"] = float(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--speed=")))
        return _Player()

    monkeypatch.setattr("pudge.audiobooks.subprocess.Popen", launch)
    if transition == "seek-error":
        monkeypatch.setattr(service, "_ipc_command", lambda _path, _command: {"request_id": 1, "error": "error", "data": None})
    target = 70.0 if transition == "seek-error" else 125.0

    result = service.seek(book_id, 105.0) if transition == "relative-cross-file" else service.seek_to(book_id, target)

    assert result["book"]["position"] == target
    assert result["book"]["paused"] is paused
    assert result["book"]["speed"] == 1.75
    assert service._sleep_deadlines[book_id] == deadline
    assert service._sleep_chapter_ends[book_id] == 160.0
    retained = service._review_contexts[book_id]
    assert {key: value for key, value in retained.items() if key != "session"} == {
        key: value for key, value in context.items() if key != "session"
    }
    assert retained["session"] == service._playback_sessions[book_id]
    assert len(launches) == 1
    if paused:
        assert "--pause=yes" in launches[0]


@pytest.fixture
def legacy_mpv(tmp_path: Path) -> Path:
    executable = tmp_path / "legacy-mpv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '--list-options' in sys.argv:\n"
        "    print(' --keep-open Choices: no yes always')\n"
        "    raise SystemExit(0)\n"
        "if any(arg.startswith('--macos-app-activation-policy=') for arg in sys.argv):\n"
        "    print('Error parsing option macos-app-activation-policy (option not found)', file=sys.stderr)\n"
        "    raise SystemExit(64)\n"
        "print('playback started')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_legacy_mpv_can_start_video_on_macos(legacy_mpv: Path, monkeypatch) -> None:
    monkeypatch.setattr("pudge.player.sys.platform", "darwin")

    command = build_mpv_command(str(legacy_mpv), Path("episode.mkv"), None, None, [])
    completed = subprocess.run(command, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "playback started"


def test_legacy_mpv_can_start_audiobook_on_macos(tmp_path: Path, monkeypatch, legacy_mpv: Path) -> None:
    service, book_id, _process, _ipc_path, _live = _live_book(tmp_path, monkeypatch)
    service.mpv = str(legacy_mpv)
    monkeypatch.setattr("pudge.audiobooks.sys.platform", "darwin")

    service.play(book_id, start=20.0)
    process = service._players[book_id]

    assert process.wait(timeout=5) == 0


@pytest.mark.parametrize("extra_args", [["--keep-open=always"], ["--keep-open=yes"], ["--keep-open"], ["--no-keep-open"]])
def test_video_keeps_explicit_user_keep_open_setting(extra_args: list[str]) -> None:
    command = build_mpv_command("mpv", Path("episode.mkv"), None, None, extra_args)

    assert "--keep-open=no" not in command
    assert all(arg in command for arg in extra_args)
