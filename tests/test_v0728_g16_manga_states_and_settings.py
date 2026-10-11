from __future__ import annotations

from types import SimpleNamespace

from pudge import web_app
from tests.t71g06_support import STUDY, column, reader


class _LightNovelsStub:
    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, backend: str):
        assert backend == "jiten"
        return {"configured": True, "account_key": "jiten:scope-test"}

    def jiten_live_state(self, word_id: int, reading_index: int, *, force: bool = False):
        return {
            "ok": True,
            "provider": "jiten",
            "wordId": word_id,
            "readingIndex": reading_index,
            "states": ["mature"],
            "normalizedState": "learning",
            "stale": False,
        }

    def jiten_live_states(self, pairs, *, force: bool = False):
        return [
            {
                "ok": True,
                "provider": "jiten",
                "wordId": int(word_id),
                "readingIndex": int(reading_index),
                "states": ["mature"],
                "normalizedState": "learning",
                "stale": False,
            }
            for word_id, reading_index in pairs
        ]

    def jiten_optimal_word_stats(self):
        return {"matureWords": 2000, "optimalWordLimit": 4000}


def test_study_state_responses_are_account_scoped() -> None:
    api = object.__new__(web_app.WebAppApi)
    api.light_novels = _LightNovelsStub()
    single = api.study_state({"backend": "jiten", "word_id": 12, "reading_index": 1})
    assert single["account_scope"] == "jiten:scope-test"
    batch = api.study_states({"backend": "jiten", "words": [[12, 1], [13, 0]]})
    assert batch["account_scope"] == "jiten:scope-test"
    assert len(batch["states"]) == 2


WORDS_PAGE = {0: [column("猫が好き", 0.80, 0.90)]}
WORDS = {"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]}
LIVE = "window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}, '3:0': {states: ['known'], normalizedState: 'known'}};"
PAINT = {"configured": True, "statusHighlight": True}


def test_states_without_an_account_in_the_answer_use_the_signed_in_account(page) -> None:
    # An older backend answers study_states without account_scope; the capability
    # endpoint still names the account, so the states count as confirmed.
    result = reader(
        page,
        "return {marks: marks().map(m => [m.word, m.state]), scope: PudgeReadingTools.study.currentAccountScope()};",
        regions=WORDS_PAGE,
        parse=WORDS,
        prefs=PAINT,
        before=STUDY + LIVE + "window.__scope = '';",
    )
    assert result.value == {"marks": [["猫", "new"], ["好き", "known"]], "scope": "jiten:me"}


def test_ocr_debug_export_reports_the_word_state_layer(page) -> None:
    result = reader(
        page,
        "const payload = await exportDebug(); return payload.events.filter(e => e.event === 'status_layer').pop();",
        regions=WORDS_PAGE,
        parse=WORDS,
        prefs=PAINT,
        before=STUDY + LIVE,
    )
    layer = result.value
    assert (layer["page_index"], layer["region_count"], layer["box_count"], layer["account_scope"]) == (
        0,
        1,
        2,
        "jiten:me",
    )


def test_reader_settings_close_on_click_away_and_escape_before_the_reader(page) -> None:
    result = reader(
        page,
        r"""
        const open = () => $('mangaV2Settings').classList.contains('open');
        const out = {};
        await __click('.manga-v2-toolbar [data-manga-v2-action="settings"]');
        out.opened = open();
        await __click('#mangaV2Settings label');                       // inside the panel: stays open
        out.inside = open();
        await __click('#mangaV2Pages .manga-v2-page-frame img', {clientX: 100, clientY: 100});
        out.clickedAway = open();

        await __click('.manga-v2-toolbar [data-manga-v2-action="settings"]');
        const layout = document.querySelector('#mangaV2Settings [data-manga-setting="mode"]').closest('.pudge-select').querySelector('.pudge-select-button');
        layout.id = 'layoutSelect';
        await __fire('#layoutSelect', 'pointerdown', {button: 0}, 'PointerEvent');
        out.menu = PudgeSelect.isOpen();
        await __click('.pudge-select-menu.open [data-pudge-select-index="0"]');   // choosing in its menu keeps the panel
        out.chosen = {panel: open(), menu: PudgeSelect.isOpen()};
        await __fire('#layoutSelect', 'pointerdown', {button: 0}, 'PointerEvent');
        await __key('Escape'); await __tick(10);                        // one Escape closes the panel and its menu
        out.escape = {panel: open(), menu: PudgeSelect.isOpen(), reader: $('mangaReaderV2').classList.contains('open')};
        await __key('Escape'); await __tick(10);                        // the next one closes the reader
        out.second = $('mangaReaderV2').classList.contains('open');
        return out;
        """,
    )
    assert result.value == {
        "opened": True,
        "inside": True,
        "clickedAway": False,
        "menu": True,
        "chosen": {"panel": True, "menu": False},
        "escape": {"panel": False, "menu": False, "reader": True},
        "second": False,
    }


def test_custom_selects_wrap_a_select_once_and_hide_the_native_control(page) -> None:
    result = page.run(
        r"""
        const make = html => {
          const box = document.createElement('div'); box.innerHTML = html;
          const select = box.querySelector('select'); const styles = [];
          const setProperty = select.style.setProperty.bind(select.style);
          // The small DOM does not keep !important: record what the page asks for.
          Object.defineProperty(select.style, 'setProperty', {value: (name, value, priority) => { styles.push([name, value, priority]); return setProperty(name, value, priority); }});
          return {box, select, styles};
        };
        const fresh = make('<select id="fresh"><option value="a">A</option><option value="b">B</option></select>');
        document.body.appendChild(fresh.box); await __flush();
        PudgeSelect.scan(document); PudgeSelect.scan(fresh.box); await __flush();
        // A select already wrapped by an earlier load of the script.
        const earlier = make('<span class="pudge-select"><select id="earlier" data-pudge-select-enhanced="1"><option>A</option></select><button class="pudge-select-button"></button></span>');
        document.body.appendChild(earlier.box); await __flush();
        const native = select => ({hidden: select.getAttribute('aria-hidden'), tab: select.tabIndex,
          styles: Object.fromEntries(['position', 'width', 'height', 'opacity', 'pointer-events'].map(n => [n, select.style.getPropertyValue(n)]))});
        return {
          freshShells: fresh.box.querySelectorAll('.pudge-select').length, freshButtons: fresh.box.querySelectorAll('.pudge-select-button').length,
          label: fresh.box.querySelector('.pudge-select-label').textContent,
          earlierShells: earlier.box.querySelectorAll('.pudge-select').length, earlierButtons: earlier.box.querySelectorAll('.pudge-select-button').length,
          fresh: native(fresh.select), earlier: native(earlier.select),
          priorities: [...new Set([...fresh.styles, ...earlier.styles].map(([, , priority]) => priority))],
        };
        """
    )
    out = result.value
    assert (out["freshShells"], out["freshButtons"], out["label"]) == (1, 1, "A")
    assert (out["earlierShells"], out["earlierButtons"]) == (1, 1)
    hidden = {
        "hidden": "true",
        "tab": -1,
        "styles": {
            "position": "absolute",
            "width": "1px",
            "height": "1px",
            "opacity": "0",
            "pointer-events": "none",
        },
    }
    assert out["fresh"] == hidden and out["earlier"] == hidden
    # Inline !important: the native control stays hidden even before the stylesheet applies.
    assert out["priorities"] == ["important"]
