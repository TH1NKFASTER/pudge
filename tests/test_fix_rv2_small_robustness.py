"""Small robustness fixes (invented data, no network)."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from pudge import audio_fingerprints, manga, subtitle_formats
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import MokuroImportError
from pudge.manga_ocr_artifact import read_artifact
from pudge.providers import jimaku
from pudge.providers.jimaku import JimakuClient, JimakuError
from pudge.subtitles import stt
from pudge.subtitles.speech_recovery import recover_constant_speech_clock


def _png(width: int, height: int) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(data, format="PNG")
    return data.getvalue()


def _volume(tmp_path: Path, pages: list[bytes]) -> tuple[MangaService, int]:
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        for index, data in enumerate(pages):
            handle.writestr(f"{index + 1:03d}.png", data)
    service = MangaService(Database(tmp_path / "manga.sqlite3"), cache_dir=tmp_path / "cache")
    return service, int(service.import_file(archive)["id"])


def test_ocr_artifact_page_sizes_come_from_the_image_header(tmp_path: Path, monkeypatch) -> None:
    service, book_id = _volume(tmp_path, [_png(30, 50), _png(31, 51)])

    def whole_page(*_args, **_kwargs):
        raise AssertionError("a whole page was read for its size")

    monkeypatch.setattr(manga, "read_page_member", whole_page)
    fingerprint, generation, _revision = service._ocr_context(book_id)
    for index in range(2):
        assert service._commit_ocr_page_updates(
            book_id,
            source_fingerprint=fingerprint,
            generation=generation,
            updates=[(index, [{"text": "テキスト"}], "ready", "", False)],
        )
    service._flush_ocr_artifact(book_id)
    pages = read_artifact(service._ocr_artifact_path(book_id))["pages"]
    assert [(page["width"], page["height"]) for page in pages] == [(30, 50), (31, 51)]


def test_mokuro_import_of_a_volume_with_a_damaged_page_is_an_import_error(tmp_path: Path) -> None:
    service, book_id = _volume(tmp_path, [_png(30, 50), b"not an image at all"])
    sidecar = tmp_path / "book.mokuro"
    sidecar.write_text(json.dumps({"pages": []}), encoding="utf-8")
    with pytest.raises(MokuroImportError, match="002.png"):
        service.import_mokuro(book_id, sidecar)


def test_speech_recovery_ignores_non_numeric_segment_metrics(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "avg_logprob": "n/a",
                        "no_speech_prob": None,
                        "compression_ratio": "inf",
                        "review_flags": 3,
                        "words": [{"word": "こんにちは", "start": 1.0, "end": 1.5, "probability": 0.9}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    path, info = recover_constant_speech_clock(source, transcript, tmp_path / "cache")
    assert path is None and isinstance(info, dict)


def test_a_truncated_fingerprint_cache_file_is_computed_again(tmp_path: Path) -> None:
    path = tmp_path / "fp.npz"
    fingerprint = audio_fingerprints.AudioFingerprint(
        np.arange(50, dtype=np.uint32), np.zeros(50, dtype=bool), 0.0, 5.0
    )
    audio_fingerprints.save_fingerprint(path, fingerprint, meta={"engine": audio_fingerprints.ENGINE_VERSION})
    assert audio_fingerprints.load_fingerprint(path) is not None
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    assert audio_fingerprints.load_fingerprint(path) is None


def test_stt_run_files_are_named_per_process(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video" * 100)
    seen: list[str] = []

    def run(command, **_kwargs):
        if "pudge.subtitles.stt_worker" in command:
            seen.append(Path(command[-3]).name)
            seen.append(Path(command[-2]).name)
            segments = [{"start": i * 2.0, "end": i * 2.0 + 1.5, "text": f"台詞{i}"} for i in range(10)]
            Path(command[-2]).write_text(json.dumps({"segments": segments}), encoding="utf-8")
        else:
            Path(command[-1]).write_bytes(b"flac")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda *_a: (None, {}))
    monkeypatch.setattr(stt, "_worker_availability", lambda _python: {"available": True})
    monkeypatch.setattr(stt.subprocess, "run", run)
    reference, info = stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    assert reference is not None and info["available"]
    audio_name, result_name = seen
    assert audio_name != "audio.flac" and str(os.getpid()) in audio_name
    assert result_name != "transcription.json" and str(os.getpid()) in result_name
    folder = reference.parent
    assert sorted(path.name for path in folder.iterdir()) == [
        "metadata.json",
        "reference.ja.srt",
        "transcription.json",
    ]


def test_ass_conversion_copies_are_named_per_process(tmp_path: Path, monkeypatch) -> None:
    subtitle = tmp_path / "episode.ass"
    subtitle.write_bytes(
        (
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは\n"
        ).encode("cp932")
    )
    names: list[str] = []

    def run(command, **_kwargs):
        names.extend([Path(command[-2]).name, Path(command[-1]).name])
        Path(command[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(subtitle_formats.subprocess, "run", run)
    output, info = subtitle_formats.convert_to_plain_srt(
        subtitle, tmp_path / "cache", ffmpeg_path="/fake/ffmpeg"
    )
    assert info["method"] == "ffmpeg"
    assert len(names) == 2 and all(f".{os.getpid()}-" in name for name in names)
    assert not [path for path in output.parent.iterdir() if path.name.startswith(".")]


def test_jimaku_download_stops_at_the_size_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jimaku, "JIMAKU_DOWNLOAD_LIMIT", 1000)
    client = JimakuClient("https://jimaku.invalid", "key")
    client.client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 5000))
    )
    monkeypatch.setattr(client, "_acquire_request_slot", lambda _kind: None)
    item = SimpleNamespace(name="big.zip", url="https://jimaku.invalid/big.zip", size=0, entry_id=1)
    try:
        with pytest.raises(JimakuError, match="big.zip"):
            client.download(item, tmp_path)
    finally:
        client.close()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_jimaku_download_is_written_from_the_stream(tmp_path: Path, monkeypatch) -> None:
    client = JimakuClient("https://jimaku.invalid", "key")
    body = b"1\n00:00:01,000 --> 00:00:02,000\n" + "字幕".encode() * 1000
    client.client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body)))
    monkeypatch.setattr(client, "_acquire_request_slot", lambda _kind: None)
    item = SimpleNamespace(name="ep.srt", url="https://jimaku.invalid/ep.srt", size=len(body), entry_id=1)
    try:
        assert client.download(item, tmp_path).read_bytes() == body
    finally:
        client.close()
