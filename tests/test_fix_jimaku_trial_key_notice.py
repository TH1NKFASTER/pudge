"""The shared Jimaku trial key stops working: Pudge notices it, explains it once and takes a
personal key, which Save checks with Jimaku first (behaviour).

Jimaku answers come from httpx.MockTransport (no network); the window runs in node
(tests/page_harness.py, fixture `page`)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pudge import cli
from pudge.config import AppConfig, load_config
from pudge.jimaku_trial import apply_jimaku_trial, jimaku_key_problem, mark_jimaku_key_notice_seen
from pudge.models import AniListAnime, VideoIdentity
from pudge.providers.jimaku import JimakuClient, JimakuError, JimakuKeyRejected
from test_web_app import make_api
from tests.i18n_support import en, ru

TRIAL_KEY = "shared-trial-key"
ENTRY = [{"id": 7, "name": "Anime", "anilist_id": 123}]


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, now: float = 1000.0) -> AppConfig:
    monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", TRIAL_KEY)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = ""
    apply_jimaku_trial(cfg, now=now)
    return cfg


def _client(cfg: AppConfig, handler) -> JimakuClient:
    client = JimakuClient(cfg.jimaku.base_url, cfg.jimaku.api_key, cache_dir=cfg.paths.cache_dir)
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"Authorization": cfg.jimaku.api_key}
    )
    return client


def test_jimaku_refusing_the_trial_key_is_a_problem_until_it_is_accepted_again(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    assert cfg.jimaku.trial_active and cfg.jimaku.api_key == TRIAL_KEY
    assert jimaku_key_problem(cfg) == {}

    answers = [httpx.Response(401, json={"error": "Invalid API key"})]
    client = _client(cfg, lambda request: answers.pop(0))
    with pytest.raises(JimakuKeyRejected) as refused:
        client.search_entries(query="Refused")
    assert refused.value.status_code == 401
    assert TRIAL_KEY not in str(refused.value)
    problem = jimaku_key_problem(cfg)
    assert (problem["kind"], problem["status"], problem["seen"]) == ("trial_rejected", 401, False)

    # Another process (the window) sees it from the cache folder; the key itself is not stored there.
    stored = "".join(path.read_text("utf-8") for path in cfg.paths.cache_dir.rglob("*.json"))
    assert TRIAL_KEY not in stored

    # Jimaku takes the key again (a temporary refusal): the problem is gone.
    answers.append(httpx.Response(200, json=ENTRY))
    assert [entry.id for entry in client.search_entries(query="Accepted")] == [7]
    client.close()
    assert jimaku_key_problem(cfg) == {}


def test_an_html_403_from_a_proxy_in_front_of_jimaku_is_not_a_refused_key(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    client = _client(
        cfg,
        lambda request: httpx.Response(
            403, text="<html>blocked</html>", headers={"content-type": "text/html"}
        ),
    )
    with pytest.raises(JimakuError) as error:
        client.search_entries(query="Blocked")
    client.close()
    assert not isinstance(error.value, JimakuKeyRejected)
    assert jimaku_key_problem(cfg) == {}


def test_a_refused_personal_key_is_not_reported_as_a_trial_problem(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.jimaku.personal_api_key = "my-own-key"
    apply_jimaku_trial(cfg, now=1000.0)
    client = _client(cfg, lambda request: httpx.Response(403, json={"error": "revoked"}))
    with pytest.raises(JimakuKeyRejected):
        client.search_entries(query="Mine")
    client.close()
    # Its own notice (tests/test_fix_jimaku_personal_key_notice.py), not the trial's.
    assert jimaku_key_problem(cfg)["kind"] == "personal_rejected"


def test_the_ended_trial_is_one_notice_per_trial(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, now=1000.0)
    expired = _config(tmp_path, monkeypatch, now=1000.0 + 49 * 3600)
    assert not expired.jimaku.trial_active and expired.jimaku.api_key == ""
    problem = jimaku_key_problem(expired)
    assert (problem["kind"], problem["seen"]) == ("trial_expired", False)

    mark_jimaku_key_notice_seen(expired.paths.cache_dir, problem["id"])
    assert jimaku_key_problem(expired)["seen"] is True
    # Refused before it ended: still the same trial, so still the notice already shown.
    cfg = _config(tmp_path, monkeypatch, now=1000.0)
    client = _client(cfg, lambda request: httpx.Response(401, json={"error": "Invalid API key"}))
    with pytest.raises(JimakuKeyRejected):
        client.search_entries(query="Refused")
    client.close()
    refused = jimaku_key_problem(cfg)
    assert (refused["kind"], refused["id"], refused["seen"]) == ("trial_rejected", problem["id"], True)


def test_a_refused_key_skips_jimaku_instead_of_failing_the_episode(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    requests: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(401, json={"error": "Invalid API key"})

    monkeypatch.setattr(cli, "JimakuClient", lambda *args, **kwargs: _client(cfg, refuse))
    monkeypatch.setattr(cli, "_jimaku_episode_aliases", lambda *_: ())
    anime = AniListAnime(id=123, titles=["Anime"], synonyms=[], season_year=2026, episodes=12, format="TV")

    found = cli._find_online_subtitles(
        tmp_path / "Anime - 05.mkv",
        VideoIdentity(title="Anime", episode=5),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )

    assert found == []
    assert requests == ["/api/entries/search"]
    assert en("cli.jimakuKeyRejected") in capsys.readouterr().out
    assert jimaku_key_problem(cfg)["kind"] == "trial_rejected"


class FakeJimaku:
    """`httpx.get` answered by a MockTransport: the requests the key check makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return answer(request)

        def get(url, **kwargs):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                return client.get(url, **kwargs)

        monkeypatch.setattr(httpx, "get", get)


def _refused_trial_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", TRIAL_KEY)
    api = make_api(tmp_path)
    assert api.config.jimaku.trial_active
    client = _client(api.config, lambda request: httpx.Response(401, json={"error": "Invalid API key"}))
    with pytest.raises(JimakuKeyRejected):
        client.search_entries(query="Refused")
    client.close()
    return api


def test_the_window_learns_about_the_refused_trial_key_and_only_once(tmp_path, monkeypatch):
    api = _refused_trial_api(tmp_path, monkeypatch)
    problem = api._settings_payload()["jimaku_key_problem"]
    assert (problem["kind"], problem["seen"]) == ("trial_rejected", False)

    assert api.mark_jimaku_key_notice_seen(problem["id"]) == {"ok": True}
    assert api._settings_payload()["jimaku_key_problem"]["seen"] is True


@pytest.mark.parametrize("status", [401, 403])
def test_save_does_not_keep_a_key_jimaku_refuses(tmp_path, monkeypatch, status):
    api = _refused_trial_api(tmp_path, monkeypatch)
    jimaku = FakeJimaku(
        monkeypatch, lambda request: httpx.Response(status, json={"error": "Invalid API key"})
    )

    result = api.save_jimaku_api_key("  wrong-key  ")

    assert (result["ok"], result["saved"], result["status"]) == (False, False, "invalid")
    [request] = jimaku.requests
    assert request.headers["Authorization"] == "wrong-key"
    assert request.url.path == "/api/entries/search"
    assert api.config.jimaku.personal_api_key == ""
    assert load_config(api.config_path).jimaku.personal_api_key == ""
    assert api._settings_payload()["jimaku_key_problem"]["kind"] == "trial_rejected"


def test_save_checks_the_key_once_then_saves_it_and_the_problem_is_gone(tmp_path, monkeypatch):
    api = _refused_trial_api(tmp_path, monkeypatch)
    jimaku = FakeJimaku(monkeypatch, lambda request: httpx.Response(200, json=ENTRY))

    result = api.save_jimaku_api_key("my-own-key")

    assert (result["ok"], result["saved"], result["verified"]) == (True, True, True)
    assert len(jimaku.requests) == 1
    assert api.config.jimaku.personal_api_key == "my-own-key"
    assert api.config.jimaku.api_key == "my-own-key"
    assert load_config(api.config_path).jimaku.personal_api_key == "my-own-key"
    assert result["settings"]["jimaku_key_problem"] == {}
    assert result["settings"]["jimaku_api_key"] != "my-own-key"  # masked for the window


def test_save_keeps_a_key_it_could_not_check_and_says_so(tmp_path, monkeypatch):
    api = _refused_trial_api(tmp_path, monkeypatch)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    FakeJimaku(monkeypatch, offline)
    result = api.save_jimaku_api_key("my-own-key")

    assert (result["ok"], result["saved"], result["verified"], result["status"]) == (
        True,
        True,
        False,
        "unreachable",
    )
    assert api.config.jimaku.personal_api_key == "my-own-key"


def test_save_without_a_key_asks_nothing(tmp_path, monkeypatch):
    api = _refused_trial_api(tmp_path, monkeypatch)
    jimaku = FakeJimaku(monkeypatch, lambda request: httpx.Response(200, json=ENTRY))
    assert api.save_jimaku_api_key("   ")["status"] == "missing"
    assert jimaku.requests == []


# ---------------------------------------------------------------------------------------
# The window.
REFUSED = {"kind": "trial_rejected", "status": 401, "id": "trial-173000", "seen": False}
NOTICE = r"""
const notice = () => {
  const body = document.getElementById('modalBody');
  const open = document.getElementById('modalBackdrop').classList.contains('open') && !!body.querySelector('[data-jimaku-key-notice]');
  return open ? {title: __text('#modalTitle'), text: __text('#modalBody'), urls: [...body.querySelectorAll('[data-action="url"]')].map(b => b.dataset.url), field: !!body.querySelector('#jimakuKeyNoticeInput'), result: __visible('#jimakuKeyNoticeResult') ? __text('#jimakuKeyNoticeResult') : null} : null;
};
"""


def _state(problem: dict | None, **settings) -> dict:
    return {
        "settings": {
            "jimaku_trial_active": True,
            "jimaku_trial_expires_at": 173000,
            "jimaku_trial_remaining_seconds": 3600,
            "jimaku_key_problem": problem or {},
            **settings,
        }
    }


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
def test_the_notice_opens_once_and_says_what_happened_and_where_to_get_a_key(page, lang, tr):
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
    for key in ("jimakuKey.trialRejected", "jimakuKey.why", "jimakuKey.howTo"):
        assert tr(key) in first["text"]
    assert first["urls"] == ["https://jimaku.cc/login", "https://jimaku.cc/account"]
    assert first["field"] is True and first["result"] is None
    assert result.calls_of("mark_jimaku_key_notice_seen") == [[REFUSED["id"]]]
    assert result.value["again"] is None


def test_the_notice_stays_closed_when_it_was_shown_before_or_there_is_no_problem(page):
    for problem in ({**REFUSED, "seen": True}, None):
        result = page.run(NOTICE + "await __tick(50); return notice();", state=_state(problem))
        assert result.value is None
        assert not result.called("mark_jimaku_key_notice_seen")


def test_the_notice_waits_while_another_window_is_open(page):
    # The problem shows up while the releases window is open: the notice waits for it to close.
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        document.getElementById('modalBackdrop').classList.add('open');
        document.getElementById('modalBody').innerHTML = '<p id="other">Releases</p>';
        ui.state.settings.jimaku_key_problem = PROBLEM;
        renderDataPages(true); await __tick(10);
        const during = notice(), other = !!document.getElementById('other');
        closeModal(); renderDataPages(true); await __tick(10);
        return {during, other, after: notice()};
        """.replace("PROBLEM", json.dumps(REFUSED)),
        state=_state(None),
    )
    assert result.value["during"] is None and result.value["other"] is True
    assert result.value["after"] is not None
    assert result.calls_of("mark_jimaku_key_notice_seen") == [[REFUSED["id"]]]


def test_settings_show_the_problem_and_open_the_notice_again(page):
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        document.getElementById('modalClose').click();
        setPage('settings'); await __tick(50);
        const callout = __text('[data-jimaku-key-callout]');
        await __click('[data-jimaku-key-open]'); await __tick(10);
        return {callout, label: __text('label[for="s_jimaku"]'), reopened: notice()};
        """,
        state=_state({**REFUSED, "seen": True}),
    )
    assert en("jimakuKey.trialRejected") in result.value["callout"]
    assert result.value["label"] == en("settings.jimakuKey")
    assert result.value["reopened"] is not None
    assert not result.called("mark_jimaku_key_notice_seen")


def test_the_ended_trial_has_its_own_explanation(page):
    result = page.run(
        NOTICE + "await __tick(50); return notice();",
        state=_state(
            {"kind": "trial_expired", "status": 0, "id": "trial-173000", "seen": False},
            jimaku_trial_active=False,
            jimaku_trial_remaining_seconds=0,
        ),
    )
    assert en("jimakuKey.trialExpired") in result.value["text"]
    assert en("jimakuKey.trialRejected") not in result.value["text"]


SAVE = (
    NOTICE
    + r"""
await __tick(50);
const answers = [];
__api.save_jimaku_api_key = async key => { answers.push(key); return window.ANSWERS.shift(); };
const results = [];
await __click('#jimakuKeyNoticeSave'); await __tick(10);
results.push(notice().result);
for (const value of [' wrong ', 'right', 'later']) {
  await __input('#jimakuKeyNoticeInput', value);
  await __click('#jimakuKeyNoticeSave'); await __tick(10);
  results.push(notice().result);
}
return {results, answers, problem: ui.state.settings.jimaku_key_problem, field: document.getElementById('jimakuKeyNoticeInput').value};
"""
)


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
def test_save_reports_whether_jimaku_took_the_key(page, lang, tr):
    saved_settings = {"jimaku_api_key": "righ••••", "jimaku_key_problem": {}, "language": lang}
    answers = [
        {"ok": False, "saved": False, "status": "invalid"},
        {"ok": True, "saved": True, "status": "ok", "verified": True, "settings": saved_settings},
        {"ok": True, "saved": True, "status": "unreachable", "verified": False, "settings": saved_settings},
    ]
    result = page.run(
        SAVE,
        lang=lang,
        state=_state(REFUSED),
        before=f"window.ANSWERS = {json.dumps(answers)};",
    )
    out = result.value
    assert out["answers"] == ["wrong", "right", "later"]
    assert out["results"] == [
        tr("jimakuKey.enterKey"),
        tr("jimakuKey.invalid"),
        tr("jimakuKey.saved"),
        tr("jimakuKey.savedUnchecked"),
    ]
    assert out["problem"] == {} and out["field"] == ""


def test_a_failed_save_call_is_shown_in_the_notice(page):
    result = page.run(
        NOTICE
        + r"""
        await __tick(50);
        __api.save_jimaku_api_key = async () => { throw new Error('bridge gone'); };
        await __input('#jimakuKeyNoticeInput', 'key');
        await __click('#jimakuKeyNoticeSave'); await __tick(10);
        return {result: notice().result, enabled: !document.getElementById('jimakuKeyNoticeSave').disabled};
        """,
        state=_state(REFUSED),
    )
    assert "bridge gone" in result.value["result"]
    assert result.value["enabled"] is True
