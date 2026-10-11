"""Statistics and Visual novels never stay blank (behaviour, page harness).

After an update both pages showed nothing and the log said nothing: the loaders returned
early (no bridge call / no module) and the failure was swallowed. Now the page shows the
error with Retry and the window log gets a FAIL ui.js_error line, and errors from before
the bridge exists (a page script that did not load) are sent once it is ready.
"""

from __future__ import annotations

OPEN = r"""
setPage(%r);
for (let i = 0; i < 8; i++) await __tick(50);
const section = document.getElementById(%r);
const box = section.querySelector('[data-page-load-error]');
return {
  error: box?.querySelector('p')?.innerText || '',
  retry: !!box?.querySelector('[data-page-retry]'),
  content: section.querySelector(%r)?.innerHTML.length || 0,
};
"""


def _open(page, name: str, content: str, **options):
    return page.run(OPEN % (name, name, content), allow_errors=True, **options)


def _page_errors(result) -> list[dict]:
    return [args[0] for args in result.calls_of("log_ui_error") if args and args[0].get("kind") == "page"]


def test_statistics_without_its_bridge_call_shows_an_error_and_logs_it(page) -> None:
    for lang in ("en", "ru"):
        result = _open(
            page, "statistics", "#statisticsContent", lang=lang, missingApi=["consumption_statistics_query"]
        )
        expected = page.run(
            "return t('ui.pageLoadFailed',{error:'consumption_statistics_query is not available'})", lang=lang
        ).value
        assert result.value["error"] == expected
        assert result.value["retry"] is True
        assert result.value["content"] > 0  # the filter bar is drawn, not a blank page
        [logged] = _page_errors(result)
        assert "consumption_statistics_query" in logged["message"]
        assert logged["source"] == "statistics.js"


def test_visual_novels_without_its_bridge_call_shows_an_error_and_logs_it(page) -> None:
    result = _open(page, "visualnovels", "#visualNovelsContent", missingApi=["visual_novel_state"])
    assert "visual_novel_state is not available" in result.value["error"]
    assert result.value["content"] > 0
    [logged] = _page_errors(result)
    assert "visual_novel_state" in logged["message"] and logged["source"] == "visual_novels.js"


def test_a_missing_page_module_is_loaded_again_then_reported(page) -> None:
    result = page.run(
        r"""
        delete window.PudgeStatistics;
        setPage('statistics');
        for (let i = 0; i < 4; i++) await __tick(50);
        const reload = [...document.querySelectorAll('script')].map(s => s.getAttribute('src') || '')
          .filter(src => src.startsWith('statistics.js?reload='));
        await __tick(5000);
        return {reload: reload.length, error: __text('#statistics [data-page-load-error]') || ''};
        """,
        allow_errors=True,
    )
    assert result.value["reload"] == 1
    assert "statistics.js is not loaded" in result.value["error"]
    assert [args[1] for args in result.calls_of("log_ui_event") if args[0] == "page.module_reload"] == [
        {"page": "statistics", "script": "statistics.js"}
    ]
    [logged] = _page_errors(result)
    assert "statistics.js is not loaded" in logged["message"]


def test_a_failed_page_load_retries_and_a_working_page_has_no_error_box(page) -> None:
    result = page.run(
        r"""
        __api.visual_novel_state = async () => {
          if (!window.__vnReady) throw new Error('capture helper is busy');
          return {running: false, transcript: [{id: 1, text: 'こんにちは'}]};
        };
        setPage('visualnovels');
        for (let i = 0; i < 8; i++) await __tick(50);
        const before = document.querySelector('#visualnovels [data-page-load-error] p')?.innerText || '';
        window.__vnReady = true;
        await __click('[data-page-retry="visualnovels"]');
        for (let i = 0; i < 8; i++) await __tick(50);
        return {before, after: document.querySelector('#visualnovels [data-page-load-error]') ? 'box' : '',
                transcript: __text('#vnTranscript')};
        """,
        allow_errors=True,
    )
    assert "capture helper is busy" in result.value["before"]
    assert result.value["after"] == ""
    assert result.value["transcript"] == "こんにちは"
    plain = page.run(
        "setPage('statistics'); for (let i = 0; i < 8; i++) await __tick(50);"
        "return document.querySelector('#statistics [data-page-load-error]') ? 'box' : ''"
    )
    assert plain.value == ""
    assert plain.calls_of("consumption_statistics_query")


def test_a_script_that_did_not_load_before_the_bridge_is_logged_once_it_is_ready(page) -> None:
    result = page.run(
        r"""
        window.__logged = [];
        const node = document.querySelector('script[src="statistics.js"]');
        node.dispatchEvent(new Event('error'));
        await __tick(0);
        const toast = __toasts().join(' ');
        const api = new Proxy({}, {get: (_, name) => name === 'log_ui_error'
          ? async payload => { window.__logged.push(payload); return {ok: true}; }
          : undefined});
        window.pywebview = {api};
        window.dispatchEvent(new Event('pywebviewready'));
        await __tick(0);
        return {toast, logged: window.__logged.filter(item => item.kind === 'load')};
        """,
        pywebview=False,
        ready=False,
        allow_errors=True,
    )
    assert "statistics.js" in result.value["toast"]
    [logged] = result.value["logged"]
    assert logged["message"] == "Script did not load: statistics.js"
    assert logged["source"].endswith("statistics.js")
