"""S3: live jpdb card state (lookup-vocabulary) instead of the parse snapshot."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def _fake_lookup(calls: list[Any], info: list[Any]):
    def fake(action, payload=None):
        calls.append((action, payload))
        assert action == "lookup-vocabulary"
        return {"vocabulary_info": info[: len(payload["list"])]}

    return fake


def test_jpdb_live_states_distinguish_unknown_id_outside_deck_and_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    calls: list[Any] = []
    info = [[["redundant", "known"]], [None], None, [["due"]]]
    monkeypatch.setattr(service, "_jpdb_request", _fake_lookup(calls, info))
    rows = service.jpdb_live_states([(1, 10), (2, 20), (1, 10), (0, 5), (3, 30), (4, 40)])
    assert calls == [
        ("lookup-vocabulary", {"list": [[1, 10], [2, 20], [3, 30], [4, 40]], "fields": ["card_state"]})
    ]
    by_id = {row["wordId"]: row for row in rows}
    assert by_id[1]["ok"] is True and by_id[1]["normalizedState"] == "redundant"
    assert by_id[1]["states"] == ["redundant", "known"] and by_id[1]["inDeck"] is True
    assert (
        by_id[2]["ok"] is True
        and by_id[2]["inDeck"] is False
        and by_id[2]["normalizedState"] == "not_in_deck"
    )
    # Unknown vid/sid is never reported as "New".
    assert by_id[3] == {
        "wordId": 3,
        "readingIndex": 30,
        "idNamespace": "jpdb",
        "provider": "jpdb",
        "ok": False,
        "reason": "unknown_id",
    }
    assert by_id[4]["normalizedState"] == "due"
    assert all(row["idNamespace"] == "jpdb" for row in rows)


def test_jpdb_live_states_require_token(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(LightNovelError):
        service.jpdb_live_states([(1, 1)])


def _api(service: LightNovelService) -> Any:
    api = types.SimpleNamespace(light_novels=service)
    for name in ("_jpdb_account_scope", "_jpdb_study_states", "_jpdb_study_state"):
        setattr(api, name, types.MethodType(getattr(WebAppApi, name), api))
    return api


def test_bridge_routes_jpdb_states_with_account_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    monkeypatch.setattr(service, "_jpdb_request", _fake_lookup([], [[["known"]]]))
    monkeypatch.setattr(service, "jiten_live_states", lambda *a, **k: pytest.fail("Jiten must not be used"))
    api = _api(service)
    scope = service.study_provider_capabilities("jpdb")["account_key"]
    assert scope

    batch = WebAppApi.study_states(api, {"backend": "jpdb", "words": [[7, 1]]})
    assert batch["ok"] is True and batch["provider"] == "jpdb" and batch["account_scope"] == scope
    assert batch["states"][0]["normalizedState"] == "mature"

    one = WebAppApi.study_state(api, {"backend": "jpdb", "word_id": 7, "reading_index": 1})
    assert one["ok"] is True and one["account_scope"] == scope and one["wordId"] == 7


def test_bridge_jpdb_failure_is_not_a_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})

    def boom(*_a, **_k):
        raise LightNovelError("JPDB request failed: timeout")

    monkeypatch.setattr(service, "_jpdb_request", boom)
    api = _api(service)
    batch = WebAppApi.study_states(api, {"backend": "jpdb", "words": [[7, 1]]})
    assert batch["ok"] is False and batch["states"] == []
    assert WebAppApi.study_state(api, {"backend": "jpdb", "word_id": 7, "reading_index": 1})["ok"] is False


# The study backend answers live card states for whatever account window.ACCOUNT names;
# window.RESPONSE_SCOPE is the account the answer was made for (a token switch mid-call).
LIVE = """
window.LIVE = 'known'; window.ACCOUNT = 'jpdb:acc-1'; window.RESPONSE_SCOPE = null;
__api.study_states = async p => ({ok: true, provider: p.backend,
  account_scope: p.backend === 'jpdb' ? (window.RESPONSE_SCOPE || window.ACCOUNT) : 'jiten:x',
  states: p.words.map(([vid, sid]) => ({ok: true, wordId: vid, readingIndex: sid, provider: p.backend, states: [window.LIVE],
                                        normalizedState: window.LIVE, stale: false}))});
__api.study_provider_capabilities = async backend => ({configured: true, account_key: backend === 'jpdb' ? window.ACCOUNT : 'jiten:x'});
__api.study_decks = async () => [{id: 'd1', name: 'Mining'}];
"""
# A cached jpdb parse: 猫 has a native jpdb id (snapshot "new"); 好き is a Jiten-id fallback.
RENDER = """
const box = document.createElement('div'); document.body.appendChild(box);
const payload = {paragraphs: ['猫が好き'],
  tokens: [[{start: 0, end: 1, wordId: 7, readingIndex: 0, idNamespace: 'jpdb'}, {start: 2, end: 4, wordId: 9001, readingIndex: 0, idNamespace: 'jiten'}]],
  vocabulary: [{wordId: 7, readingIndex: 0, idNamespace: 'jpdb', cardState: ['new'], states: ['new'], inDeck: true, spelling: '猫', reading: 'ねこ'},
               {wordId: 9001, readingIndex: 0, idNamespace: 'jiten', states: [], spelling: '好き'}]};
const words = () => [...box.querySelectorAll('[data-pudge-study-token]')].map(n => ({
  state: [...n.classList].filter(c => c.startsWith('state-')).join(' '), verified: n.dataset.pudgeStudyVerified === '1'}));
const lookups = () => __apiCalls.filter(c => c.name === 'study_states').map(c => c.args[0]);
"""


def test_rendered_jpdb_text_shows_live_jpdb_states(page) -> None:
    result = page.run(
        RENDER
        + """
        const out = {};
        out.batch = await PudgeReadingTools.study.lookupLiveStates([[5, 1], [5, 1], [0, 1]], {backend: 'jpdb'});
        window.RESPONSE_SCOPE = 'jpdb:old';                                  // the token changed during the call
        out.otherAccount = await PudgeReadingTools.study.lookupLiveStates([[5, 1]], {backend: 'jpdb'});
        window.RESPONSE_SCOPE = null;
        __apiCalls.length = 0;
        box.innerHTML = PudgeReadingTools.study.renderParsedText(payload, {backend: 'jpdb'});
        out.snapshot = words();
        await __tick(100);
        out.live = words(); out.lookups = lookups();
        __apiCalls.length = 0;
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['犬'], tokens: [[{start: 0, end: 1, wordId: 3, readingIndex: 0}]],
                                                                  vocabulary: [{wordId: 3, readingIndex: 0}]}, {backend: 'jiten'});
        await __tick(100);
        out.jiten = lookups().map(call => call.backend);
        return out;
        """,
        before=LIVE,
    )
    out = result.value
    assert [(row["wordId"], row["provider"], row["account_scope"]) for row in out["batch"]] == [
        (5, "jpdb", "jpdb:acc-1")
    ]
    assert out["otherAccount"] == []
    assert out["snapshot"] == [
        {"state": "state-new", "verified": False},
        {"state": "state-new", "verified": False},
    ]
    # Only the native jpdb id is looked up, and the live state replaces the snapshot.
    assert out["lookups"] == [{"backend": "jpdb", "words": [[7, 0]]}]
    # jpdb's "known" is Jiten's Mature (an older answer naming it "known" still colours).
    assert out["live"] == [
        {"state": "state-mature", "verified": True},
        {"state": "state-new", "verified": False},
    ]
    assert out["jiten"] == ["jiten"]


def test_review_and_add_refresh_jpdb_native_state_after_mutation(page) -> None:
    result = page.run(
        RENDER
        + """
        box.innerHTML = PudgeReadingTools.study.renderParsedText(payload, {backend: 'jpdb'});
        await __tick(100);
        const run = async (button, deck, liveAfter) => {
          __apiCalls.length = 0;
          await __click('[data-pudge-study-token]'); await __tick(20);
          if (deck) await __input('#pudgeStudyDeck', deck);
          __apiCalls.length = 0;
          window.LIVE = liveAfter;                                         // jpdb's state after the change
          let release; __api.study_action = () => new Promise(r => { release = r; });
          await __click(button); await __tick(5);
          const pending = words()[0];
          release({ok: true, outcome: 'confirmed'}); await __tick(50);
          return {pending, after: words()[0], lookups: lookups(),
                  action: __apiCalls.filter(c => c.name === 'study_action').map(c => [c.args[0].action, c.args[0].backend, c.args[0].grade ?? null])};
        };
        const review = await run('[data-pudge-study-review="okay"]', null, 'learning');
        const add = await run('[data-pudge-study-add]', 'd1', 'known');
        return {review, add};
        """,
        before=LIVE
        + "__api.light_novel_state = async () => ({version: 1, settings: {study_backend: 'jpdb'}, books: []});",
    )
    review, add = result.value["review"], result.value["add"]
    # The word stops claiming a verified state while the change is on its way ...
    # jpdb known/learning are Jiten's Mature/Young.
    assert review["pending"] == {"state": "state-mature", "verified": False}
    assert review["action"] == [["review", "jpdb", "okay"]]
    # ... and gets the authoritative jpdb state right after it.
    assert review["lookups"] == [{"backend": "jpdb", "words": [[7, 0]]}]
    assert review["after"] == {"state": "state-young", "verified": True}
    assert add["pending"] == {"state": "state-young", "verified": False}
    assert add["action"] == [["add", "jpdb", None]]
    assert add["lookups"] == [{"backend": "jpdb", "words": [[7, 0]]}]
    assert add["after"] == {"state": "state-mature", "verified": True}
