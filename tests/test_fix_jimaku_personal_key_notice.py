"""Jimaku refuses the user's own API key (401/403, not a proxy's HTML page): the window shows the
same notice as for the shared trial key, with texts for one's own key, once per refused key,
again from Settings, and the problem ends when a working key is saved (behaviour).

Jimaku answers come from httpx.MockTransport (no network); the window runs in node
(tests/page_harness.py, fixture `page`)."""

from __future__ import annotations

import httpx
import pytest

from pudge.config import AppConfig, load_config
from pudge.jimaku_trial import apply_jimaku_trial, jimaku_key_problem, mark_jimaku_key_notice_seen
from pudge.providers.jimaku import JimakuError, JimakuKeyRejected
from test_fix_jimaku_trial_key_notice import ENTRY, NOTICE, FakeJimaku, _client
from test_web_app import make_api
from tests.i18n_support import en, ru

OWN_KEY = "my-own-key"


def _refuse(status: int = 401):
    return lambda request: httpx.Response(status, json={"error": "Invalid API key"})


def _search(cfg: AppConfig, handler):
    client = _client(cfg, handler)
    try:
        return client.search_entries(query="Anime")
    finally:
        client.close()


def _own_key_config(tmp_path, monkeypatch, *, release_build: bool = True, key: str = OWN_KEY) -> AppConfig:
    if release_build:
        monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", "shared-trial-key")
    else:
        monkeypatch.delenv("PUDGE_BUNDLED_JIMAKU_API_KEY", raising=False)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = key
    apply_jimaku_trial(cfg, now=1000.0)
    assert cfg.jimaku.personal_api_key == key and not cfg.jimaku.trial_active
    return cfg


@pytest.mark.parametrize("release_build", [True, False], ids=["with-trial-key", "without-trial-key"])
@pytest.mark.parametrize("status", [401, 403])
def test_jimaku_refusing_your_own_key_is_a_problem_of_its_own(tmp_path, monkeypatch, status, release_build):
    cfg = _own_key_config(tmp_path, monkeypatch, release_build=release_build)
    assert jimaku_key_problem(cfg) == {}

    with pytest.raises(JimakuKeyRejected):
        _search(cfg, _refuse(status))

    problem = jimaku_key_problem(cfg)
    assert (problem["kind"], problem["status"], problem["seen"]) == ("personal_rejected", status, False)
    assert problem["id"].startswith("personal-") and OWN_KEY not in problem["id"]
    stored = "".join(path.read_text("utf-8") for path in cfg.paths.cache_dir.rglob("*.json"))
    assert OWN_KEY not in stored
    mark_jimaku_key_notice_seen(cfg.paths.cache_dir, problem["id"])
    assert jimaku_key_problem(cfg)["seen"] is True


def test_an_html_403_from_a_proxy_is_not_a_refused_own_key(tmp_path, monkeypatch):
    cfg = _own_key_config(tmp_path, monkeypatch)
    blocked = lambda request: httpx.Response(
        403, text="<html>blocked</html>", headers={"content-type": "text/html"}
    )  # noqa: E731
    with pytest.raises(JimakuError) as error:
        _search(cfg, blocked)
    assert not isinstance(error.value, JimakuKeyRejected)
    assert jimaku_key_problem(cfg) == {}


def test_the_notice_is_once_per_refused_key(tmp_path, monkeypatch):
    cfg = _own_key_config(tmp_path, monkeypatch)
    with pytest.raises(JimakuKeyRejected):
        _search(cfg, _refuse())
    first = jimaku_key_problem(cfg)
    mark_jimaku_key_notice_seen(cfg.paths.cache_dir, first["id"])
    with pytest.raises(JimakuKeyRejected):  # refused again later: the same key, shown already
        _search(cfg, _refuse(403))
    assert (jimaku_key_problem(cfg)["id"], jimaku_key_problem(cfg)["seen"]) == (first["id"], True)

    # Another key: no problem until Jimaku refuses that one too, then a new notice.
    other = _own_key_config(tmp_path, monkeypatch, key="another-key")
    assert jimaku_key_problem(other) == {}
    with pytest.raises(JimakuKeyRejected):
        _search(other, _refuse())
    second = jimaku_key_problem(other)
    assert second["kind"] == "personal_rejected"
    assert second["id"] != first["id"] and second["seen"] is False


def test_the_problem_ends_when_jimaku_takes_the_key_again(tmp_path, monkeypatch):
    cfg = _own_key_config(tmp_path, monkeypatch)
    with pytest.raises(JimakuKeyRejected):
        _search(cfg, _refuse())
    assert [entry.id for entry in _search(cfg, lambda request: httpx.Response(200, json=ENTRY))] == [7]
    assert jimaku_key_problem(cfg) == {}


# ---------------------------------------------------------------------------------------
# The window's backend.
def _refused_own_key_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", "shared-trial-key")
    api = make_api(tmp_path)
    api.save_settings({"jimaku_api_key": OWN_KEY})
    assert api.config.jimaku.api_key == OWN_KEY
    with pytest.raises(JimakuKeyRejected):
        _search(api.config, _refuse())
    return api


def test_the_window_learns_about_the_refused_own_key(tmp_path, monkeypatch):
    api = _refused_own_key_api(tmp_path, monkeypatch)
    problem = api._settings_payload()["jimaku_key_problem"]
    assert (problem["kind"], problem["seen"]) == ("personal_rejected", False)
    api.mark_jimaku_key_notice_seen(problem["id"])
    assert api._settings_payload()["jimaku_key_problem"]["seen"] is True


@pytest.mark.parametrize("key", ["a-new-key", OWN_KEY], ids=["new-key", "same-key-works-again"])
def test_saving_a_working_key_ends_the_problem(tmp_path, monkeypatch, key):
    api = _refused_own_key_api(tmp_path, monkeypatch)
    jimaku = FakeJimaku(monkeypatch, lambda request: httpx.Response(200, json=ENTRY))

    result = api.save_jimaku_api_key(key)

    assert (result["saved"], result["verified"]) == (True, True)
    assert [request.headers["Authorization"] for request in jimaku.requests] == [key]
    assert result["settings"]["jimaku_key_problem"] == {}
    assert api._settings_payload()["jimaku_key_problem"] == {}
    assert load_config(api.config_path).jimaku.personal_api_key == key


def test_a_key_jimaku_refuses_too_is_not_saved_and_the_problem_stays(tmp_path, monkeypatch):
    api = _refused_own_key_api(tmp_path, monkeypatch)
    before = api._settings_payload()["jimaku_key_problem"]
    FakeJimaku(monkeypatch, _refuse(403))

    result = api.save_jimaku_api_key("wrong-key")

    assert (result["saved"], result["status"]) == (False, "invalid")
    assert api.config.jimaku.personal_api_key == OWN_KEY
    assert api._settings_payload()["jimaku_key_problem"]["id"] == before["id"]


# ---------------------------------------------------------------------------------------
# The window.
REFUSED = {"kind": "personal_rejected", "status": 401, "id": "personal-0123456789ab", "seen": False}


def _state(problem: dict | None) -> dict:
    return {
        "settings": {
            "jimaku_trial_active": False,
            "jimaku_trial_expires_at": 0,
            "jimaku_trial_remaining_seconds": 0,
            "jimaku_api_key": "my-o••••",
            "jimaku_key_problem": problem or {},
        }
    }


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
def test_the_notice_opens_once_and_says_jimaku_refuses_your_key(page, lang, tr):
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        const first = notice();
        document.getElementById('modalClose').click();
        await __tick(10);
        renderDataPages(true); await __tick(10);
        return {first, again: notice()};
        """,
        lang=lang,
        state=_state(REFUSED),
    )
    first = result.value["first"]
    assert first is not None, result.console
    assert first["title"] == tr("jimakuKey.title")
    for key in ("jimakuKey.personalRejected", "jimakuKey.why", "jimakuKey.howToPersonal"):
        assert tr(key) in first["text"]
    for key in ("jimakuKey.trialRejected", "jimakuKey.trialExpired", "jimakuKey.howTo"):
        assert tr(key) not in first["text"]
    assert first["urls"] == ["https://jimaku.cc/login", "https://jimaku.cc/account"]
    assert first["field"] is True
    assert result.calls_of("mark_jimaku_key_notice_seen") == [[REFUSED["id"]]]
    assert result.value["again"] is None


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
def test_settings_show_the_refused_key_and_open_the_notice_again(page, lang, tr):
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        setPage('settings'); await __tick(50);
        const callout = __text('[data-jimaku-key-callout]'), button = __text('[data-jimaku-key-open]');
        await __click('[data-jimaku-key-open]'); await __tick(10);
        return {callout, button, label: __text('label[for="s_jimaku"]'), reopened: notice()};
        """,
        lang=lang,
        state=_state({**REFUSED, "seen": True}),
    )
    out = result.value
    assert tr("jimakuKey.personalRejected") in out["callout"]
    assert out["button"] == tr("jimakuKey.openNoticePersonal")
    assert out["label"] == tr("settings.jimakuKey")
    assert out["reopened"] is not None and tr("jimakuKey.howToPersonal") in out["reopened"]["text"]
    assert not result.called("mark_jimaku_key_notice_seen")


def test_saving_a_working_key_in_the_notice_clears_the_warning_in_settings(page):
    saved = {"jimaku_api_key": "a-ne••••", "jimaku_key_problem": {}, "language": "en"}
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        setPage('settings'); await __tick(50);
        await __click('[data-jimaku-key-open]'); await __tick(10);
        await __input('#jimakuKeyNoticeInput', 'a-new-key');
        await __click('#jimakuKeyNoticeSave'); await __tick(10);
        return {result: notice().result, callouts: document.querySelectorAll('[data-jimaku-key-callout]').length,
                problem: ui.state.settings.jimaku_key_problem};
        """,
        state=_state({**REFUSED, "seen": True}),
        api={
            "save_jimaku_api_key": {
                "ok": True,
                "saved": True,
                "status": "ok",
                "verified": True,
                "settings": saved,
            }
        },
    )
    assert result.value == {"result": en("jimakuKey.saved"), "callouts": 0, "problem": {}}
    assert result.calls_of("save_jimaku_api_key") == [["a-new-key"]]
