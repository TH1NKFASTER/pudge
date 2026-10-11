"""Jiten word card (live state, header layout) and manga reading progress (behaviour; audit
71Б: the manga progress scenarios of tests/js/manga_progress_x01_x03.cjs, which rewrote
manga_reader_v2.js to reach its internals, now run in the page harness; the header's CSS
contract is read through css_support)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from css_support import css_rules
from t71g03_support import MANGA, READER
from t71js_support import SAVES, STRIP, VERTICAL, pages

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("tests/js/jiten_live_card_s5.cjs", "jiten live card S5: PASS"),
        ("tests/js/jiten_card_header_s10.cjs", "jiten card header S10: PASS"),
    ],
)
def test_frontend_behaviour(script: str, marker: str) -> None:
    result = subprocess.run(
        ["node", str(ROOT / script), str(ROOT / "pudge/web")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout


def test_card_header_columns_let_the_headword_wrap_instead_of_clipping() -> None:
    rules = css_rules("reading_tools.css")
    head = rules.declarations(".pudge-study-head")
    assert head["display"] == "grid" and head["grid-template-columns"] == "minmax(0,1fr) auto auto"
    assert (
        rules.get('.pudge-study-head[data-layout="stacked"]', "grid-template-areas")
        == '"term close" "actions actions"'
    )
    term = rules.declarations(".pudge-study-term")
    assert term["white-space"] == "normal" and term["min-width"] == "0"
    assert (
        term.get("word-break") != "keep-all"
        and term.get("overflow") != "hidden"
        and "text-overflow" not in term
    )
    assert rules.get(".pudge-study-term-controls", "flex") == "0 0 auto"
    assert rules.get(".pudge-study-close", "grid-area") == "close"
    assert rules.get(".pudge-study-head-actions", "grid-area") == "actions"


def test_failed_progress_saves_are_retried_and_reported_once_per_failure(page) -> None:
    result = page.run(
        READER
        + STRIP
        + r"""
        await openStrip(3); await see(0);
        const out = {};
        SAVES.failing = true;
        await see(3); out.failed = saves();               // read through page 3, resume at page 3: both fail
        await again(); out.retried = saves();             // the same saves are sent again
        SAVES.failing = false; SAVES.hold = true;
        await again(); out.sending = saves();
        await again(); out.whileSending = saves();        // already on its way: not sent twice
        SAVES.settle('reject'); await __tick(10);
        await again(); out.afterReject = saves();         // a save that failed on its way is sent again
        SAVES.settle('resolve'); SAVES.hold = false; await __tick(10);
        await again(); out.saved = saves();               // saved: nothing to send
        SAVES.failing = true;
        await see(5); out.newFailure = saves();
        out.messages = shownToasts;
        out.expected = PudgeI18n.t('manga.couldNotSaveMangaProgressError', {error: 'bridge down'});
        return out;
        """,
        before=MANGA + pages(10) + SAVES,
        **VERTICAL,
    )
    out = result.value
    assert out["failed"] == {"position": [3], "read": [2], "toasts": 1}
    assert out["retried"] == {
        "position": [3, 3],
        "read": [2, 2],
        "toasts": 1,
    }  # one message per failure streak
    assert out["sending"] == {"position": [3, 3, 3], "read": [2, 2, 2], "toasts": 1}
    assert out["whileSending"] == out["sending"]
    assert out["afterReject"] == {"position": [3, 3, 3, 3], "read": [2, 2, 2, 2], "toasts": 1}
    assert out["saved"] == {**out["afterReject"]}
    # After a success, a new failure is reported again.
    assert out["newFailure"] == {"position": [3, 3, 3, 3, 5], "read": [2, 2, 2, 2, 4], "toasts": 2}
    assert out["messages"] == [out["expected"]] * 2


def test_vertical_strip_shows_the_most_visible_of_all_tracked_pages(page) -> None:
    # The observer reports only pages whose visibility changed; the current page is the
    # most visible one among all pages it has reported, not only the latest batch.
    result = page.run(
        READER
        + STRIP
        + r"""
        await openStrip(3);
        const report = async entries => {
          visibility().__fire(entries.map(([i, ratio]) => ({target: frame(i), isIntersecting: ratio > 0, intersectionRatio: ratio})));
          await __tick(50);
          return __text('#mangaV2PageLabel');
        };
        return [
          await report([[0, .9], [1, .1]]),
          await report([[1, .25]]),              // page 0 is still 90 % visible
          await report([[0, .2], [1, .75]]),
          await report([[0, 0], [2, .05]]),      // page 1 (unchanged, 75 %) stays
        ];
        """,
        before=MANGA + pages(10) + SAVES,
        **VERTICAL,
    )
    assert result.value == ["1 / 10", "1 / 10", "2 / 10", "2 / 10"]
