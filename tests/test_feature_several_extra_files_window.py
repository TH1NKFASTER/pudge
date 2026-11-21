"""Several extras of one title: one card, and a window to delete or keep each file.

User (Yojouhan Shinwa Taikei): "there are two more openings and an ending. When there are
several, let a click open a window where each file can be deleted or kept + now the card does
not go away although I pressed Delete". The card showed one extra at a time: Delete removed the
ED and the card stayed for OP1, then OP2.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_fix_batch_extra_files import _batch, _no_subtitles, _series
from test_web_app import make_api

EXTRAS = [
    "[SubsPlease] Dorohedoro - NCOP1 (1080p).mkv",
    "[SubsPlease] Dorohedoro - NCOP2 (1080p).mkv",
    "[SubsPlease] Dorohedoro - NCED (1080p).mkv",
]


def test_the_card_carries_every_extra_waiting_for_an_answer(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _series(api.manager.db)
    _no_subtitles(monkeypatch)
    folder, item = _batch(tmp_path, ["[SubsPlease] Dorohedoro - 01 (1080p).mkv", *EXTRAS])
    api.manager._register_completed_download(item)
    [card] = api.get_state()["home"]["needs_action"]
    names = [row["name"] for row in card["extra_files"]]
    assert sorted(names) == sorted(EXTRAS) and names == sorted(names)
    assert all(Path(row["video_path"]).is_file() and row["kind"] for row in card["extra_files"])

    ed = next(row["video_path"] for row in card["extra_files"] if "NCED" in row["name"])
    state = api.extra_file_delete(ed)["state"]
    [card] = state["home"]["needs_action"]
    assert [row["name"] for row in card["extra_files"]] == sorted(EXTRAS[:2])
    for row in card["extra_files"]:
        api.extra_file_keep(row["video_path"])
    assert not api.get_state()["home"].get("needs_action")


FILES = [
    {
        "name": "[Elysium]The.Tatami.Galaxy.ED(BD.1080p.FLAC)[8BEF01B1].mkv",
        "kind": "ED",
        "video_path": "/lib/T/ED.mkv",
    },
    {
        "name": "[Elysium]The.Tatami.Galaxy.OP1(BD.1080p.FLAC)[4BC9C7F1].mkv",
        "kind": "OP",
        "video_path": "/lib/T/OP1.mkv",
    },
    {
        "name": "[Elysium]The.Tatami.Galaxy.OP2(BD.1080p.FLAC)[07769A17].mkv",
        "kind": "OP",
        "video_path": "/lib/T/OP2.mkv",
    },
]
CARD = {
    "media_id": 7785,
    "title": "Yojouhan Shinwa Taikei",
    "action_code": "review_extra_file",
    "extra_file": {"name": FILES[0]["name"], "kind": "ED"},
    "extra_files": FILES,
    "local": {"episode": None, "video_path": FILES[0]["video_path"], "state": "waiting_subtitles"},
}

SCRIPT = r"""
renderCurrent();
const out = {note: __text('.extra-file-card .extra-file-note'),
             buttons: [...document.querySelectorAll('.extra-file-card .extra-file-actions button')].map(b => b.dataset.action)};
await __click('[data-action="extra-files-open"]'); await __tick(10);
const rows = () => [...document.querySelectorAll('#modalBody .extra-files-row')].map(n => n.dataset.extraPath);
out.open = !$('modalBackdrop').hidden && $('modalBackdrop').classList.contains('open');
out.title = $('modalTitle').textContent; out.rows = rows();
out.applyDisabled = document.querySelector('[data-action="extra-files-apply"]').disabled;
const choose = async (path, choice) => { await __click(`#modalBody [data-path="${path}"][data-choice="${choice}"]`); await __tick(5); };
await choose('/lib/T/ED.mkv', 'delete'); await choose('/lib/T/OP1.mkv', 'keep');
await choose('/lib/T/OP2.mkv', 'delete'); await choose('/lib/T/OP2.mkv', 'delete');  // a second click takes it back
await __click('[data-action="extra-files-apply"]'); await __tick(10);
out.question = __text('#pudgeConfirmMessage');
await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]'); await __tick(30);
out.left = rows(); out.stillOpen = $('modalBackdrop').classList.contains('open');
await __click('[data-action="extra-files-all"][data-choice="keep"]'); await __tick(5);
await __click('[data-action="extra-files-apply"]'); await __tick(30);  // keeping needs no question
out.closed = !$('modalBackdrop').classList.contains('open');
out.toasts = __toasts();
out.expected = {note: PudgeI18n.t('ui.extraFilesFromDownload', {kinds: 'ED, 2 OP'}),
                title: PudgeI18n.t('ui.extraFilesTitle', {title: 'Yojouhan Shinwa Taikei'}),
                question: PudgeI18n.t('confirm.deleteExtraFiles', {count: 1})};
return out;
"""


def test_the_window_deletes_or_keeps_each_file(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            SCRIPT,
            lang=lang,
            state={"home": {"needs_action": [CARD]}},
            api={"extra_file_delete": {"ok": True}, "extra_file_keep": {"ok": True}},
        )
        out = result.value
        assert out["note"] == out["expected"]["note"] and out["buttons"] == ["extra-files-open"]
        assert out["open"] and out["title"] == out["expected"]["title"]
        assert out["rows"] == ["/lib/T/ED.mkv", "/lib/T/OP1.mkv", "/lib/T/OP2.mkv"]
        assert out["applyDisabled"] is True
        assert out["question"] == out["expected"]["question"]
        assert out["left"] == ["/lib/T/OP2.mkv"] and out["stillOpen"] is True
        assert out["closed"] is True
        assert result.calls_of("extra_file_delete") == [["/lib/T/ED.mkv"]]
        assert result.calls_of("extra_file_keep") == [["/lib/T/OP1.mkv"], ["/lib/T/OP2.mkv"]]
        assert json.dumps(out["toasts"], ensure_ascii=False)


def test_a_single_extra_keeps_its_two_buttons(page) -> None:
    single = {**CARD, "extra_files": FILES[:1]}
    out = page.run(
        "renderCurrent(); return [...document.querySelectorAll('.extra-file-card .extra-file-actions button')].map(b => b.dataset.action);",
        state={"home": {"needs_action": [single]}},
    ).value
    assert out == ["extra-file-delete", "extra-file-keep"]
