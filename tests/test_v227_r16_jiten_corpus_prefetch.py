from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    return service


def test_batch_live_state_lookup_populates_same_r15_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    requests: list[tuple[str, dict]] = []

    def fake_request(action: str, payload=None):
        requests.append((action, payload))
        return {"result": [[2], [4, 1]], "decks": []}

    monkeypatch.setattr(service, "_jiten_request", fake_request)

    rows = service.jiten_live_states([(120, 2), (44, 0), (120, 2)])

    assert requests == [
        (
            "reader/lookup-vocabulary",
            {"words": [[120, 2], [44, 0]]},
        )
    ]
    assert [(row["wordId"], row["readingIndex"]) for row in rows] == [(120, 2), (44, 0)]
    assert rows[0]["states"] == ["mature"]
    assert rows[0]["rawStateLabel"] == "Mature"
    assert rows[1]["states"] == ["due", "young"]
    assert rows[1]["normalizedState"] == "due"

    parsed = {
        "paragraphs": ["少し"],
        "vocabulary": [{"wordId": 120, "readingIndex": 2, "spelling": "少し", "knownState": []}],
        "tokens": [[{"wordId": 120, "readingIndex": 2, "start": 0, "end": 2}]],
    }
    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")
    assert vocabulary[0]["states"] == ["mature"]
    assert tokens[0][0]["card"]["states"] == ["mature"]

    # Fresh cache means the next renderer hydration is local-only.
    assert service.jiten_live_states([(120, 2), (44, 0)])
    assert len(requests) == 1


def test_batch_failure_keeps_stale_r15_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_jiten_request",
        lambda *_args, **_kwargs: {"result": [[2]]},
    )
    service.jiten_live_states([(120, 2)])
    key = service._jiten_live_state_key(120, 2)
    _ts, payload = service._jiten_live_state_cache[key]
    service._jiten_live_state_cache[key] = (0.0, payload)

    def offline(*_args, **_kwargs):
        raise LightNovelError("offline")

    monkeypatch.setattr(service, "_jiten_request", offline)
    result = service.jiten_live_states([(120, 2)], force=True)

    assert result[0]["states"] == ["mature"]
    assert result[0]["stale"] is True
    assert result[0]["knowledgeStatus"] == "stale_live_state_cache"


def test_corpus_orders_global_frequency_first_then_local_occurrences(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parsed = {
        "paragraphs": ["dummy"],
        "vocabulary": [
            {"wordId": 10, "readingIndex": 0, "frequencyRank": 100},
            {"wordId": 20, "readingIndex": 0, "frequencyRank": 50},
            {"wordId": 30, "readingIndex": 1, "frequencyRank": 100},
            {"wordId": 40, "readingIndex": 0},
        ],
        "tokens": [
            [
                {"wordId": 10, "readingIndex": 0},
                {"wordId": 20, "readingIndex": 0},
                {"wordId": 30, "readingIndex": 1},
                {"wordId": 30, "readingIndex": 1},
                {"wordId": 40, "readingIndex": 0},
            ]
        ],
    }
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
            ("corpus", json.dumps(parsed), "jiten-v1", 1.0),
        )

    assert service.jiten_corpus_pairs() == [(20, 0), (30, 1), (10, 0), (40, 0)]


def test_corpus_prefetch_uses_batches_in_priority_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "jiten_corpus_pairs", lambda: [(1, 0), (2, 0), (3, 0)])
    calls: list[list[tuple[int, int]]] = []

    def fake_live_states(pairs, *, force=False):
        assert force is False
        calls.append(list(pairs))
        return [{"ok": True} for _ in pairs]

    monkeypatch.setattr(service, "jiten_live_states", fake_live_states)
    monkeypatch.setattr(service, "JITEN_STATE_PREFETCH_PAUSE_SECONDS", 0.0)

    result = service.jiten_prefetch_corpus_states(batch_size=2)

    assert calls == [[(1, 0), (2, 0)], [(3, 0)]]
    assert result == {"ok": True, "reason": "complete", "total": 3, "warmed": 3, "batches": 2}


def test_web_api_exposes_batch_state_lookup() -> None:
    class LightNovels:
        class Settings:
            study_backend = "jiten"

        def settings(self):
            return self.Settings()

        def jiten_live_states(self, pairs, *, force=False):
            return [
                {"wordId": word_id, "readingIndex": reading_index, "force": force}
                for word_id, reading_index in pairs
            ]

    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = LightNovels()

    result = api.light_novel_study_states({"backend": "jiten", "words": [[120, 2], [44, 0]], "force": True})

    assert result == {
        "ok": True,
        "provider": "jiten",
        "states": [
            {"wordId": 120, "readingIndex": 2, "force": True},
            {"wordId": 44, "readingIndex": 0, "force": True},
        ],
    }


def test_app_start_warms_the_jiten_states_of_the_whole_corpus_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    write_config(cfg, cfg.config_path)
    LightNovelService(cfg).save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    runs: list[dict] = []

    def prefetch(self, **kwargs):
        runs.append(kwargs)
        return {"ok": True, "reason": "complete", "total": 3, "warmed": 3, "batches": 1}

    monkeypatch.setattr(LightNovelService, "jiten_prefetch_corpus_states", prefetch)
    api = WebAppApi(cfg.config_path)
    for thread in threading.enumerate():
        if "jiten-state-prefetch" in thread.name:
            thread.join(timeout=5)

    assert len(runs) == 1 and runs[0]["cancel_event"].is_set() is False
    # A finished warm-up is not repeated on the next request.
    again = api.jiten_state_prefetch()
    assert (again["started"], again["reason"], again["warmed"]) == (False, "complete", 3)
    assert len(runs) == 1
    # Without Jiten there is nothing to warm.
    api.light_novels.save_settings({"study_backend": "jpdb"})
    assert api.jiten_state_prefetch(force=True) == {"started": False, "reason": "jiten_disabled"}


# A reader paragraph of Jiten words (vocabulary = the parse snapshot) and live Jiten states.
LIVE = r"""
window.__live = {'120:2': {states: ['mature'], normalizedState: 'learning'}, '44:0': {states: ['due', 'young'], normalizedState: 'due'}};
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_states = async payload => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) =>
  ({ok: true, wordId: w, readingIndex: r, ...(window.__live[`${w}:${r}`] || {states: [], normalizedState: 'new'})}))});
const stateOf = node => [...node.classList].filter(c => c.startsWith('state-')).sort();
const lookups = since => __apiCalls.slice(since).filter(c => c.name === 'study_states').map(c => c.args[0]);
"""


def test_reader_colours_words_from_one_batch_lookup_before_any_click(page) -> None:
    result = page.run(
        LIVE
        + """
        const words = [['少し', 120, 2], ['猫', 44, 0], ['犬', 8, 0], ['少し', 120, 2]];
        let pos = 0;
        const tokens = words.map(([text, wordId, readingIndex]) => { const start = pos; pos += text.length; return {wordId, readingIndex, start, end: pos}; });
        const vocabulary = words.slice(0, 3).map(([text, wordId, readingIndex]) => ({wordId, readingIndex, spelling: text, knownState: []}));
        const box = document.createElement('div');
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: [words.map(w => w[0]).join('')], tokens: [tokens], vocabulary}, {backend: 'jiten'});
        document.body.appendChild(box);
        const parsed = [...box.querySelectorAll('[data-pudge-study-token]')].map(stateOf);
        await __tick(100);
        const hydrated = {lookups: lookups(0), states: [...box.querySelectorAll('[data-pudge-study-token]')].map(stateOf)};
        const since = __apiCalls.length;
        await __click(`[data-pudge-study-token="${box.querySelector('[data-pudge-study-token]').dataset.pudgeStudyToken}"]`);
        await __tick(50);
        return {parsed, hydrated, clicked: lookups(since), fetches: __fetches.slice()};
        """,
    )
    out = result.value
    assert out["parsed"] == [["state-new"]] * 4
    # One batch for the paragraph's distinct words, before the user clicks anything.
    assert out["hydrated"]["lookups"] == [
        {"backend": "jiten", "words": [[120, 2], [44, 0], [8, 0]], "force": False}
    ]
    # Jiten's colour priority: the raw Mature wins over an older cache's "learning"; Due over Young.
    assert out["hydrated"]["states"] == [["state-mature"], ["state-due"], ["state-new"], ["state-mature"]]
    # Opening a word still refreshes exactly that word.
    assert out["clicked"] == [{"backend": "jiten", "words": [[120, 2]], "force": False}]
    # The window never talks to Jiten itself: every lookup goes through the app.
    assert out["fetches"] == []


def test_light_novel_chapter_is_coloured_from_live_states(page) -> None:
    result = page.run(
        LIVE
        + """
        __api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['少しの猫'],
          vocabulary: [{wordId: 120, readingIndex: 2, spelling: '少し', knownState: []}, {wordId: 44, readingIndex: 0, spelling: '猫', knownState: []}],
          tokens: [[{wordId: 120, readingIndex: 2, start: 0, end: 2}, {wordId: 44, readingIndex: 0, start: 3, end: 4}]]});
        await __tick(100);
        const since = __apiCalls.length;
        await __settle(openLightNovel(5));
        await __tick(100);
        return {lookups: lookups(since),
                words: [...document.querySelectorAll('#lnReader [data-ln-token]:not([data-ln-fallback])')].map(n => [n.textContent, stateOf(n)])};
        """,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    # The cached chapter parse says New; the chapter is re-coloured from one live batch.
    assert result.value["lookups"] == [{"backend": "jiten", "words": [[120, 2], [44, 0]], "force": False}]
    assert result.value["words"] == [
        # One colour class per word (Jiten's priority), no second colour state's class.
        ["少し", ["state-mature"]],
        ["猫", ["state-due"]],
    ]
