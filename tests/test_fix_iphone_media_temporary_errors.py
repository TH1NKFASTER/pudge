"""Review of the media error mapping: some MediaErrors were temporary, but a 422 is final for the
phone (a watched mark is not sent again). "The Mac app is still starting", "Jiten states are
unavailable", an ffmpeg run that hit its time limit and "the OCR of this volume is not finished
yet" are now MediaTemporaryError: HTTP 503 with the same reason, sent again later. Real refusals
("no longer on the Mac", not set up, invalid request) stay 422 with their reason."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from pudge import iphone_media
from pudge.database import Database
from pudge.iphone_http import IPhoneHTTPServer, media_refusal
from pudge.iphone_media import MediaError, MediaTemporaryError
from pudge.iphone_service import IPhoneService
from pudge.light_novels import LightNovelSetupRequired


@pytest.fixture
def service(tmp_path):
    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _raised(call) -> BaseException:
    with pytest.raises(MediaError) as caught:
        call()
    return caught.value


def test_the_mac_app_still_starting_is_temporary(service):
    service.api = None
    error = _raised(lambda: service.media.api)
    assert isinstance(error, MediaTemporaryError) and str(error) == "The Mac app is still starting"


def test_jiten_states_unavailable_is_temporary(service):
    service.api = SimpleNamespace(study_states=lambda _value: {"ok": False})
    error = _raised(lambda: service.media.study_states({"pairs": [[1, 0]]}))
    assert isinstance(error, MediaTemporaryError) and str(error) == "Jiten states are unavailable"


def test_an_ffmpeg_run_over_its_time_limit_is_temporary(tmp_path, monkeypatch):
    from pudge.subtitles import stt

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda _source, _ffprobe: (1, {}))
    monkeypatch.setattr(iphone_media, "video_prepare_timeout", lambda _duration: 1.0)
    probe = tmp_path / "ffprobe"
    probe.write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps({{'streams': [{{'index': 0, 'codec_type': 'video', "
        "'codec_name': 'vp9'}], 'format': {'duration': '60'}}))\n"
    )
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(
        f"#!{sys.executable}\nimport time\nprint('out_time_us=0', flush=True)\ntime.sleep(60)\n"
    )
    for tool in (probe, ffmpeg):
        tool.chmod(0o755)
    source = tmp_path / "Show - 01.mkv"
    source.write_bytes(b"x")
    error = _raised(
        lambda: iphone_media.prepare_video(source, tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe))
    )
    assert isinstance(error, MediaTemporaryError) and "did not finish" in str(error)


def test_an_unfinished_ocr_is_temporary(service, monkeypatch):
    reading = service.media.reading
    monkeypatch.setattr(reading, "ocr_status", lambda _item: {"complete": False})
    error = _raised(lambda: reading.prepare("manga-ocr", 3))
    assert isinstance(error, MediaTemporaryError)
    assert str(error) == "The Mac has not finished the OCR of this volume yet"


class _Media:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def handle(self, command, route, query, body, device=""):
        raise self.error


class _Service:
    server_id = "server"

    def __init__(self, error: BaseException) -> None:
        self.media = _Media(error)

    def authenticate(self, token):
        return "phone"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MediaTemporaryError("The Mac app is still starting"), 503),
        (MediaTemporaryError("Jiten states are unavailable"), 503),
        (MediaTemporaryError("ffmpeg did not finish preparing the episode within 30 min"), 503),
        (MediaTemporaryError("The Mac has not finished the OCR of this volume yet"), 503),
        (MediaError("This episode is no longer on the Mac"), 422),
        (MediaError("Invalid attempt"), 422),
        (LightNovelSetupRequired("Jiten API token is not configured"), 422),
        (RuntimeError("Jiten API token is not configured"), 503),  # only the marked class is final
    ],
)
def test_temporary_failures_are_503_and_refusals_422_with_the_same_reason(error, status):
    assert media_refusal(error) is (status == 422)
    server = IPhoneHTTPServer(("127.0.0.1", 0), _Service(error))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("GET", "/api/v2/media/study/options", headers={"Authorization": "Bearer token"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        assert (response.status, body["error"]) == (status, str(error))
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
