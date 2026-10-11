from __future__ import annotations

import subprocess
from pathlib import Path

from tests.css_support import css_rules
from tests.t71g06_support import STUDY, column, reader

ROOT = Path(__file__).resolve().parents[1]


def _write_srt(path: Path, end_seconds: int) -> None:
    minutes, seconds = divmod(end_seconds, 60)
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nstart\n\n"
        f"2\n00:{minutes:02d}:{max(0, seconds - 1):02d},000 --> 00:{minutes:02d}:{seconds:02d},000\nend\n",
        encoding="utf-8",
    )


def test_assassination_classroom_singleton_tail_clock_is_rejected() -> None:
    from pudge.subtitles.timeline_alignment import _suppress_weak_singleton_tail_transition

    source = [(0.0, 10.0, "start"), (1216.298, 1222.429, "late"), (1369.910, 1374.915, "song")]
    reference = [(0.0, 10.0, "start"), (1385.0, 1390.950, "credits")]
    segments = [
        {"offset_seconds": 0.0, "support": 22, "kind": "stable"},
        {"offset_seconds": 59.0, "support": 1, "kind": "stable"},
    ]

    result, boundaries, diagnostics = _suppress_weak_singleton_tail_transition(
        source, segments, [1216.0], reference
    )

    assert [row["offset_seconds"] for row in result] == [0.0]
    assert boundaries == []
    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "weak_singleton_tail_clock_reference_overshoot"
    assert diagnostics["tail_duration_seconds"] == 158.915
    assert diagnostics["reference_overshoot_seconds"] == 42.965


def test_embedded_reference_gate_rejects_59_second_tail_growth(tmp_path: Path) -> None:
    from pudge.syncing import _validate_embedded_reference_output

    reference = tmp_path / "reference.srt"
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    _write_srt(reference, 1391)
    _write_srt(source, 1375)
    _write_srt(aligned, 1434)

    ok, reason, details = _validate_embedded_reference_output(source, aligned, reference)
    assert ok is False
    assert reason == "aligned_too_long"
    assert details["alignment_extension_seconds"] == 59
    assert details["reference_overshoot_seconds"] == 43


# Three bubbles of words on per-character OCR geometry.
WORDS_PAGE = {
    0: [column("猫が好き", 0.80, 0.90), column("犬と鳥", 0.70, 0.90), column("花も木も", 0.60, 0.90)]
}
WORDS = {
    "猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]],
    "犬と鳥": [["犬", 4, 0], ["と", 5, 0], ["鳥", 6, 0]],
    "花も木も": [["花", 7, 0], ["も", 8, 0], ["木", 9, 0], ["も", 8, 0]],
}
LIVE = r"""
window.__live = {
  '1:0': {states: ['new'], normalizedState: 'new'},
  '3:0': {states: ['mature', 'due'], normalizedState: 'due'},        // due Mature: learning, due outline
  '4:0': {states: ['known', 'due'], normalizedState: 'due'},         // due Known stays known
  '6:0': {states: ['blacklisted'], normalizedState: 'blacklisted'},  // ignored words are not painted
  '7:0': {states: ['redundant'], normalizedState: 'blacklisted'},
  '9:0': {states: ['known'], normalizedState: 'known', stale: true}, // a last-known (stale) state is not confirmed
};
"""
REPAINT = "await __key('ArrowLeft'); await __tick(100); await __key('ArrowRight'); await __tick(300);"
PAINT = {"configured": True, "statusHighlight": True}


def _painted(marks: list[dict]) -> dict:
    return {mark["word"]: (mark["state"], mark["due"]) for mark in marks}


def test_only_confirmed_states_of_the_current_jiten_account_are_painted(page) -> None:
    result = reader(
        page,
        f"""
        const out = {{otherAnswer: marks()}};
        window.__scope = undefined; {REPAINT}
        out.confirmed = marks();
        window.__account = 'jiten:other';                          // another Jiten account signs in ...
        window.__live = {{'1:0': {{states: ['known'], normalizedState: 'known'}}}}; window.__onlyListed = true;
        await __click('#mangaV2Pages .manga-v2-page-frame img', {{clientX: 825, clientY: 200}}); await __tick(300);  // ... and 猫 is looked up
        await __key('Escape'); await __tick(50);
        out.switched = marks();
        return out;
        """,
        regions=WORDS_PAGE,
        parse=WORDS,
        prefs=PAINT,
        before=STUDY + LIVE + "window.__scope = 'jiten:old';",  # answers made for the previous account
    )
    out = result.value
    assert out["otherAnswer"] == []
    assert _painted(out["confirmed"]) == {
        "猫": ("new", False),
        "好き": ("learning", True),
        "犬": ("known", True),
    }
    # Words not looked up again for the new account lose the old account's colours.
    assert _painted(out["switched"]) == {"猫": ("known", False)}


def test_a_review_repaints_only_its_word_once_jiten_confirms_it(page) -> None:
    result = reader(
        page,
        r"""
        const ids = () => [...document.querySelectorAll('#mangaV2Pages [data-occurrence-id]')];
        const out = {ids: ids().map(n => n.dataset.occurrenceId)};
        await __click('#mangaV2Pages .manga-v2-page-frame img', {clientX: 825, clientY: 490}); await __tick(50);  // on 好き
        out.card = __text('#pudgeStudyCard .pudge-study-word, #pudgeStudyCard h3') ?? __text('#pudgeStudyCard');
        let release; __api.study_action = () => new Promise(resolve => { release = resolve; });
        await __click('#pudgeStudyCard [data-pudge-study-review]'); await __tick(20);
        out.pending = _w(marks());
        const pending = ids();
        window.__live['3:0'] = {states: ['known'], normalizedState: 'known', studyDeckIds: [1]};
        release({ok: true, outcome: 'confirmed'}); await __tick(200);
        out.after = _w(marks());
        out.kept = ids().filter(node => pending.includes(node)).length;
        out.review = calls('study_action').map(([payload]) => [payload.word_id, payload.action]);

        setRegions(0, window.__regions[0].map(row => ({...row, alignment_revision: 'r8'})));   // OCR read again
        await __key('o'); await __tick(300);
        out.reread = ids().map(n => n.dataset.occurrenceId);
        return out;
        function _w(rows) { return Object.fromEntries(rows.map(m => [m.word, m.state + (m.due ? '+due' : '')])); }
        """,
        regions={0: [{**row, "alignment_revision": "r7"} for row in WORDS_PAGE[0]]},
        parse=WORDS,
        prefs=PAINT,
        before=STUDY + LIVE + "window.__live['3:0'].studyDeckIds = [1];",
    )
    out = result.value
    # Each mark is one painted word of one OCR alignment: page:region:revision:word:mark.
    assert out["ids"] == ["0:0:r7:0:0", "0:0:r7:2:1", "0:1:r7:0:0"]
    assert "好き" in out["card"]
    assert out["review"] == [[3, "review"]]
    # While the review is on its way the word claims no state; Jiten's answer repaints it.
    assert out["pending"] == {"猫": "new", "犬": "known+due"}
    assert out["after"] == {"猫": "new", "好き": "known", "犬": "known+due"}
    assert out["kept"] == 2  # the other words' marks were not rebuilt
    # Marks of an older OCR alignment are replaced, not reused.
    assert out["reread"] == ["0:0:r8:0:0", "0:0:r8:2:1", "0:1:r8:0:0"]


def test_word_state_marks_never_catch_clicks() -> None:
    rules = css_rules("manga_reader_v2.css")
    assert rules.get(".manga-v2-status-layer", "pointer-events") == "none"


def test_g12_javascript_syntax() -> None:
    for rel in ("pudge/web/reading_tools.js", "pudge/web/manga_reader_v2.js"):
        result = subprocess.run(
            ["node", "--check", str(ROOT / rel)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
