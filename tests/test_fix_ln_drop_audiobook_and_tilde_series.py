"""The light-novel screen takes a dropped audiobook (the native drop imports it as one), and a
volume whose title has a wave dash 〜 joins the series written with a tilde ～ (invented titles)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from css_support import css_rules
from page_harness import web_module
from test_v0728_ln_snapshot_recovery_g7 import insert_book, service_at

from pudge.light_novels import _series_key
from pudge.manga import _manga_series_key
from tests.i18n_support import i18n_source


def test_an_audiobook_dropped_on_the_light_novel_screen_goes_through() -> None:
    setup = r"""
const assert=require('assert'); const listeners={}; const classes=new Set(); let notices=0;
const box={dataset:{},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
const page={classList:{contains:x=>x==='active'}};
global.window=global;window.ui={lang:'en'};window.addEventListener=(k,f)=>listeners[k]=f;
global.document={getElementById:id=>id==='lightnovels'?page:box,addEventListener:(k,f)=>listeners[k]=f};
window.toast=()=>notices++;
function event(type,names){return {type,dataTransfer:{types:['Files'],files:names.map(name=>({name}))},
  preventDefault(){this.prevented=true},stopImmediatePropagation(){this.stopped=true},stopPropagation(){}}}
"""
    checks = r"""
for (const name of ['book.m4b', 'part 01.mp3', 'a.epub']) {
  const over=event('dragover',[name]); listeners.dragover(over); assert(over.prevented, name); assert(classes.has('ln-file-drag'));
  const drop=event('drop',[name]); listeners.drop(drop); assert(!drop.stopped, name); assert(!classes.has('ln-file-drag'));
}
const bad=event('drop',['a.pdf']); listeners.drop(bad); assert(bad.stopped); assert.equal(notices,1);
"""
    script = setup + i18n_source() + "\n;\n" + web_module("ln_drop.js") + checks
    subprocess.run(["node", "-"], input=script, text=True, check=True)


def test_a_wave_dash_and_a_tilde_name_one_series() -> None:
    wave, tilde, ascii_tilde = "作品 〜副題〜 1 (文庫)", "作品 ～副題～ 2 (文庫)", "作品 ~副題~"
    assert _series_key(wave) == _series_key(tilde) == _series_key(ascii_tilde) != ""
    assert _manga_series_key("作品 〜副題〜 1") == _manga_series_key("作品 ～副題～ 2")
    assert _series_key("作品 〜副題〜") != _series_key("別の作品 〜副題〜")


def test_the_wave_dash_volume_inherits_the_series_anilist_link(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    first = insert_book(service, "作品 〜副題〜 1 (文庫)", str(tmp_path / "one.epub"), None)
    second = insert_book(service, "作品 ～副題～ 2 (文庫)", str(tmp_path / "two.epub"), 123)
    assert service._reconcile_local_series_links() == 1
    assert service.book(first)["anilist_id"] == 123
    keys = {int(book["id"]): book["series_key"] for book in service._state_payload_fast()["books"]}
    assert keys[first] == keys[second] == "anilist:123"


def test_a_word_without_kanji_gets_no_second_line_of_kana(page) -> None:
    result = page.run(
        r"""
        const line = card => PudgeJitenWords.readingsLine(card);
        return {
          kana: line({wordTextPlain: 'もたらす', readingIndex: 0,
                      readings: [{readingIndex: 0, text: 'もたらす'}, {readingIndex: 1, text: 'もらたす'}]}),
          katakana: line({wordTextPlain: 'テスト', readingIndex: 0, readings: [{readingIndex: 0, text: 'てすと'}]}),
          kanji: line({wordTextPlain: '作品', readingIndex: 0,
                       readings: [{readingIndex: 0, text: 'さくひん'}, {readingIndex: 1, text: 'さくほん'}]}),
        };
        """
    )
    assert result.value == {"kana": "", "katakana": "", "kanji": "さくほん"}


def test_the_pitch_line_takes_the_answer_scrollbar_back_to_centre_under_the_word(page) -> None:
    from study_page_support import GATE, GATE_VIEW

    result = page.run(
        GATE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20); await space();
        const back = gate().querySelector('.pudge-review-gate-back');
        Object.defineProperty(back, 'offsetWidth', {value: 600, configurable: true});
        Object.defineProperty(back, 'clientWidth', {value: 585, configurable: true});   // a 15 px scrollbar
        PudgeReviewGate.alignAnswerUnderWord(gate().querySelector('.pudge-review-gate-card'));
        return back.style.getPropertyValue('--pudge-gate-gutter');
        """,
        before=GATE,
    )
    assert result.value == "15px"
    rules = css_rules("review_gate.css")
    for selector in (
        ".pudge-review-gate-back .pudge-review-gate-pitch",
        ".pudge-review-gate-back .pudge-review-gate-reading",
    ):
        assert rules.get(selector, "padding-left") == "var(--pudge-gate-gutter,0px)"


def test_digits_before_kanji_share_their_furigana(page) -> None:
    from study_page_support import GATE, GATE_VIEW

    before = (
        GATE
        + "window.GATE_CARDS[0] = reviewCard(1, '１０万', 'じゅうまん', {readings: ["
        + "{readingIndex: 0, text: 'じゅうまん', rubyText: '１０万[じゅうまん]'}, {readingIndex: 1, text: '100000'}]});"
        + "window.GATE_CARDS[1] = reviewCard(2, '第2', 'だいに', {readings: [{readingIndex: 0, text: 'だいに', rubyText: '第[だい]2[に]'}]});"
    )
    result = page.run(
        GATE_VIEW
        + r"""
        const ruby = () => [...frontWord().querySelectorAll('ruby')].map(r => [r.firstChild.textContent, r.querySelector('rt').textContent]);
        await startPlay(EPISODE); await __tick(20); await space();
        const out = {big: ruby(), readings: __text('.pudge-review-gate-reading'),
                     kana: PudgeJitenWords.kana('１０万[じゅうまん]'), tea: PudgeJitenWords.kana('お茶[ちゃ]')};
        await gradeKey(3); await space(); await __tick(20); await space();
        out.ordinal = ruby();
        return out;
        """,
        before=before,
    )
    assert result.value["big"] == [["１０万", "じゅうまん"]]  # was じゅうまん over 万 alone
    assert result.value["readings"] is None  # no "100000 ・ じゅうまん" line under it
    assert (result.value["kana"], result.value["tea"]) == ("じゅうまん", "おちゃ")
    assert result.value["ordinal"] == [["第", "だい"], ["2", "に"]]
