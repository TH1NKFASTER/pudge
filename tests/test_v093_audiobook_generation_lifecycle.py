from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.light_novels import LightNovelError
from pudge.web_app import WebAppApi


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebAppApi:
    monkeypatch.setattr(web_app_module, "DATA_DIR", tmp_path / "data")
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def test_ordinary_linked_audiobook_blocks_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "_audiobook_generation_runtime", lambda *_a, **_k: {"provider": "external"})
    monkeypatch.setattr(api.audiobooks, "link_for_light_novel", lambda *_a, **_k: {"book": {"id": 9, "path": str(tmp_path / "ordinary")}})
    monkeypatch.setattr(api, "_audiobook_link_kind", lambda *_a, **_k: "ordinary")

    with pytest.raises(LightNovelError, match="already has a linked audiobook"):
        api.generate_light_novel_tts(1)


def test_generated_link_is_identified_separately_from_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    generated = api._audiobook_generation_root() / "Book [Generated 4]"
    generated.mkdir(parents=True)
    (generated / ".pudge-audiobook-profile.json").write_text("{}", encoding="utf-8")

    assert api._audiobook_link_kind(4, {"book": {"path": str(generated)}}) == "generated"
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    assert api._audiobook_link_kind(4, {"book": {"path": str(ordinary)}}) == "ordinary"


def test_partial_generation_is_resumable_but_not_automatic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    output = api._audiobook_generation_root() / "Book [Generated 5]"
    api._write_audiobook_generation_manifest(
        output,
        {
            "book_id": 5,
            "mode": "generate",
            "state": "paused",
            "current": 40,
            "total": 100,
            "output_dir": str(output),
            "message": "Audiobook generation paused",
        },
    )

    status = api._irodori_tts_status(5, paired_audio=None)
    assert status["active"] is False
    assert status["resumable"] is True
    assert status["state"] == "paused"
    assert status["current"] == pytest.approx(40)
    assert status["total"] == pytest.approx(100)
    # Reading state only reports the checkpoint; it does not start a worker.
    assert 5 not in api._irodori_tts_threads


def test_pause_preserves_partial_files_and_writes_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    output = api._audiobook_generation_root() / "Book [Generated 6]"
    output.mkdir(parents=True)
    partial = output / ".pudge-audiobook-parts" / "0001-x" / "0001.mp3"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"audio")
    job_id = api.job_center.start("audiobook_generation", "test", total=100)
    with api._irodori_tts_lock:
        api._audiobook_generation_controls[6] = "pause_requested"

    stopped = api._audiobook_generation_stop_if_requested(
        book_id=6,
        job_id=job_id,
        output_dir=output,
        manifest={"book_id": 6, "mode": "generate", "output_dir": str(output)},
        current=55,
        total=100,
    )

    assert stopped is True
    assert partial.is_file()
    manifest = json.loads(api._audiobook_generation_manifest_path(output).read_text(encoding="utf-8"))
    assert manifest["state"] == "paused"
    assert manifest["current"] == pytest.approx(55)


def test_cancel_deletes_partial_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    output = api._audiobook_generation_root() / "Book [Generated 7]"
    api._write_audiobook_generation_manifest(
        output,
        {
            "book_id": 7,
            "mode": "generate",
            "state": "paused",
            "current": 20,
            "total": 100,
            "output_dir": str(output),
        },
    )
    (output / "partial.mp3").write_bytes(b"audio")

    result = api.cancel_light_novel_tts(7)

    assert result["ok"] is True
    assert result["deleted"] is True
    assert not output.exists()
    assert api._audiobook_generation_manifest(7) is None


class _Response:
    status_code = 200
    is_error = False
    content = b"A" * 2048
    text = ""


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *_args, **_kwargs):
        return _Response()


def test_regenerate_keeps_old_audiobook_until_new_one_is_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    book_id = 8
    old_id = 88
    staging = api._audiobook_generation_root() / "Book [Regenerating 8-test]"
    monkeypatch.setattr(
        api,
        "_audiobook_generation_runtime",
        lambda *_a, **_k: {
            "provider": "external",
            "base_url": "https://tts.invalid/v1",
            "api_key": "",
            "model": "tts",
            "voice": "voice",
            "caption": "",
            "speed": 1.0,
        },
    )
    monkeypatch.setattr(
        api.light_novels,
        "tts_source",
        lambda _id: {"title": "Book", "chapters": [{"title": "One", "text": "本文です。"}]},
    )
    monkeypatch.setattr(web_app_module.httpx, "Client", _Client)
    monkeypatch.setattr(api, "_merge_audiobook_parts", lambda _parts, output: output.write_bytes(b"M" * 4096))
    monkeypatch.setattr(api.audiobooks, "import_folder", lambda *_a, **_k: {"id": 99})
    delete_calls: list[int] = []

    def link_new(ln_id: int, audiobook_id: int, **_kwargs):
        assert ln_id == book_id
        assert audiobook_id == 99
        # The old audiobook must still exist at the moment the replacement is linked.
        assert delete_calls == []
        return {"ok": True}

    monkeypatch.setattr(api.audiobooks, "link_light_novel", link_new)
    monkeypatch.setattr(api.audiobooks, "delete", lambda audiobook_id, **_kwargs: delete_calls.append(int(audiobook_id)))

    job_id = api.job_center.start("audiobook_generation", "regenerate", total=5)
    with api._irodori_tts_lock:
        api._irodori_tts_job_ids[book_id] = job_id
        api._audiobook_generation_start_params[book_id] = {
            "mode": "regenerate",
            "output_dir": str(staging),
            "old_audiobook_id": old_id,
        }

    api._irodori_tts_worker(book_id, job_id)

    assert api.job_center.get(job_id)["state"] == "succeeded"
    assert delete_calls == [old_id]


def test_v93_ui_exposes_generation_lifecycle_controls() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "Resume generation" in html
    assert "Regenerate audiobook" in html
    assert 'data-ln-tts-action="pause"' in html
    assert 'data-ln-tts-action="cancel"' in html
    assert "Cancel generation and delete generated parts?" in html
    assert "pairedKind!=='ordinary'" in html
    assert "pairedKind==='generated'" in html
    assert "Has audiobook" in html
    assert "Has local audiobook" not in html
    assert "Has TTS-generated audiobook" not in html
