"""A start where the page's scripts did not load heals itself.

User: "a pile of interface errors at the bottom right, log collection by shortcut does not
work, every screen broke". The log showed every script after the head failing to load in
the first second and a half after the start ("Script did not load: planning.js", ...) while
the page server answered all of them normally a minute later. Without its scripts every
page threw ("Can't find variable: updateWeeklyLoad") until Pudge was restarted.

Now the page notes such failures from the head on and reloads itself once it has loaded,
at most twice in a row, and the log says so. The page server also keeps a longer queue of
waiting connections for the burst of script requests.
"""

from __future__ import annotations

import json

from pudge import web_app

FAIL_ONE = r"""
document.addEventListener('DOMContentLoaded', () => {
  const node = document.createElement('script');
  node.setAttribute('src', 'planning.js');
  document.head.appendChild(node);
  node.dispatchEvent(new Event('error'));   // what WebKit fires for a script that did not load
});
"""
READ = r"""
let stored = null; try { stored = JSON.parse(sessionStorage.getItem('pudge.loadRetries') || 'null'); } catch (e) {}
return {reloads: window.__reloads || 0, stored, failures: window.__pudgeLoadFailures};
"""


def test_a_clean_start_does_not_reload(page) -> None:
    out = page.run(READ).value
    assert out == {"reloads": 0, "stored": None, "failures": []}


def test_a_script_that_did_not_load_reloads_the_page_once_and_the_log_says_so(page) -> None:
    first = page.run(READ, before=FAIL_ONE, allow_errors=True).value
    assert first["reloads"] == 1
    assert first["stored"] == {"count": 1, "files": ["planning.js"]}

    # The reloaded page loads everything: the counter goes and the log has the reload.
    second = page.run(READ, sessionStorage={"pudge.loadRetries": json.dumps(first["stored"])})
    assert second.value["reloads"] == 0 and second.value["stored"] is None
    reports = [args[0] for args in second.calls_of("log_ui_error")]
    assert [r["kind"] for r in reports] == ["reload"]
    assert "planning.js" in reports[0]["message"] and "(1)" in reports[0]["message"]


def test_no_third_reload_in_a_row(page) -> None:
    stored = json.dumps({"count": 2, "files": ["planning.js"]})
    out = page.run(
        READ, before=FAIL_ONE, allow_errors=True, sessionStorage={"pudge.loadRetries": stored}
    ).value
    assert out["reloads"] == 0 and out["stored"]["count"] == 2


def test_the_page_server_keeps_a_longer_queue() -> None:
    assert web_app._AssetServer.request_queue_size >= 64
    assert web_app._AssetServer.daemon_threads is True
