from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.light_novels import LightNovelError
from pudge.web_app import WebAppApi
from tests.i18n_support import en, has_text


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


def test_ordinary_linked_audiobook_blocks_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
    monkeypatch.setattr(api, "_audiobook_generation_runtime", lambda *_a, **_k: {"provider": "external"})
    monkeypatch.setattr(
        api.audiobooks,
        "link_for_light_novel",
        lambda *_a, **_k: {"book": {"id": 9, "path": str(tmp_path / "ordinary")}},
    )
    monkeypatch.setattr(api, "_audiobook_link_kind", lambda *_a, **_k: "ordinary")

    with pytest.raises(LightNovelError, match="already has a linked audiobook"):
        api.generate_light_novel_tts(1)


def test_generated_link_is_identified_separately_from_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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


def test_cancel_deletes_partial_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(
        api.audiobooks, "delete", lambda audiobook_id, **_kwargs: delete_calls.append(int(audiobook_id))
    )

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


GENERATION_BOOKS = [
    {"id": 1, "title": "Plain", "volume": 1},
    {
        "id": 2,
        "title": "Generated",
        "volume": 2,
        "irodori_tts": {"paired_kind": "generated"},
        "paired_audio": {"audiobook_id": 21, "book": {"id": 21}},
    },
    {
        "id": 3,
        "title": "Paused",
        "volume": 3,
        "irodori_tts": {"resumable": True, "state": "paused", "current": 40, "total": 100},
    },
    {
        "id": 4,
        "title": "Ordinary",
        "volume": 4,
        "irodori_tts": {"paired_kind": "ordinary"},
        "paired_audio": {"audiobook_id": 41, "book": {"id": 41}},
    },
    {
        "id": 5,
        "title": "Running",
        "volume": 5,
        "irodori_tts": {"active": True, "state": "running", "current": 10, "total": 100},
    },
    {
        "id": 6,
        "title": "Ordinary paused",
        "volume": 6,
        "irodori_tts": {"paired_kind": "ordinary", "resumable": True, "state": "paused"},
        "paired_audio": {"audiobook_id": 61, "book": {"id": 61}},
    },
]
LN_STATE = {"version": 1, "settings": {"audiobook_generation_provider": "irodori"}, "books": GENERATION_BOOKS}
GENERATION_ACTIONS = {"generate-audiobook", "regenerate-audiobook", "resume-audiobook"}


def test_ln_menu_offers_the_generation_step_that_fits_the_book(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels', true); await __tick(200);
        const out = {};
        for (const id of [1, 2, 3, 4, 5, 6]) {
          await __fire(`[data-ln-book="${id}"]`, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
          out[id] = [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].filter(b => b.checkVisibility())
            .map(b => [b.dataset.lnContextAction, b.innerText]);
          hideContextMenu();
        }
        out.labels = {generate: t('ui.generateAudiobook'), regenerate: t('ui.regenerateAudiobook'), resume: t('ui.resumeGeneration2')};
        return out;
        """,
        api={"light_novel_state": LN_STATE},
    )
    out = result.value
    generation = {key: [pair for pair in out[key] if pair[0] in GENERATION_ACTIONS] for key in "123456"}
    labels = out["labels"]
    assert generation["1"] == [["generate-audiobook", labels["generate"]]]
    assert generation["2"] == [["regenerate-audiobook", labels["regenerate"]]]
    assert generation["3"] == [["resume-audiobook", labels["resume"]]]
    # An ordinary (downloaded) audiobook is never replaced by a generated one; a running job offers nothing new.
    assert generation["4"] == generation["5"] == generation["6"] == []
    assert (
        en("ui.resumeGeneration2") == "Resume generation"
        and en("ui.regenerateAudiobook") == "Regenerate audiobook"
    )


def test_ln_card_controls_pause_resume_and_cancel_generation(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels', true); await __tick(200);
        const actions = id => [...document.querySelectorAll(`[data-ln-tts-status="${id}"] [data-ln-tts-action]`)]
          .filter(b => b.checkVisibility()).map(b => [b.dataset.lnTtsAction, b.innerText]);
        const out = {running: actions(5), paused: actions(3), ordinaryPaused: actions(6), idle: actions(1)};
        await __click('[data-ln-tts-status="5"] [data-ln-tts-action="pause"]'); await __tick(50);
        await __click('[data-ln-tts-status="3"] [data-ln-tts-action="resume"]'); await __tick(50);
        await __click('[data-ln-tts-status="3"] [data-ln-tts-action="cancel"]'); await __tick(50);
        out.question = __visible('#pudgeConfirmBackdrop') ? __text('#pudgeConfirmMessage') : null;
        await __click('#pudgeConfirmBackdrop [data-pudge-confirm="cancel"]'); await __tick(50);
        out.cancelsAfterNo = __apiCalls.filter(c => c.name === 'cancel_light_novel_tts').length;
        await __click('[data-ln-tts-status="3"] [data-ln-tts-action="cancel"]'); await __tick(50);
        await __click('#pudgeConfirmBackdrop [data-pudge-confirm="ok"]'); await __tick(50);
        out.labels = {pause: t('ui.pause'), cancel: t('ui.cancel'), resume: t('ui.resumeGeneration'), question: t('ui.cancelGenerationAndDeleteGenerated')};
        return out;
        """,
        api={
            "light_novel_state": LN_STATE,
            "pause_light_novel_tts": {"ok": True},
            "resume_light_novel_tts": {"started": True},
            "cancel_light_novel_tts": {"ok": True, "deleted": True},
        },
    )
    out = result.value
    labels = out["labels"]
    assert out["running"] == [["pause", labels["pause"]], ["cancel", labels["cancel"]]]
    assert out["paused"] == [["resume", labels["resume"]], ["cancel", labels["cancel"]]]
    assert out["ordinaryPaused"] == [["cancel", labels["cancel"]]]
    assert out["idle"] == []
    assert result.calls_of("pause_light_novel_tts") == [[5]]
    assert result.calls_of("resume_light_novel_tts") == [[3]]
    # Cancelling deletes the generated parts: only after the user confirms.
    assert out["question"] == labels["question"]
    assert out["cancelsAfterNo"] == 0 and result.calls_of("cancel_light_novel_tts") == [[3]]
    assert en("ui.resumeGeneration") == "Resume generation"
    assert en("ui.cancelGenerationAndDeleteGenerated") == "Cancel generation and delete generated parts?"


def test_paired_badge_says_has_audiobook_for_any_kind_of_audiobook(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels', true); await __tick(200);
            const tip = async id => {
              await __fire(`[data-ln-book="${id}"] .ln-paired-audio-badge`, 'mouseover', {}, 'MouseEvent');
              const node = document.getElementById('titleTooltip');
              return node.classList.contains('show') ? node.innerText : null;
            };
            return {generated: await tip(2), ordinary: await tip(4), plain: Boolean(document.querySelector('[data-ln-book="1"] .ln-paired-audio-badge')),
                    label: t('ui.hasAudiobook')};
            """,
            lang=lang,
            api={"light_novel_state": LN_STATE},
        )
        out = result.value
        assert out["generated"] == out["ordinary"] == out["label"]
        assert out["plain"] is False
    assert en("ui.hasAudiobook") == "Has audiobook"
    assert not has_text("Has local audiobook") and not has_text("Has TTS-generated audiobook")
