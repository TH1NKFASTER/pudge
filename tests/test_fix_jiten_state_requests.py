"""How often Pudge asks Jiten about words (audit JITEN-2..JITEN-11, JITEN-13).

Word states are kept 15 minutes in SQLite (and checked against Jiten's overview-version at
most once a minute while a reader asks), the N+1 ceiling 6 hours, dictionary entries 30
days; one lookup per pair at a time; manga bubbles are parsed as arrays; the sidebar
reuses the review gate's verified cards; nothing is warmed at launch.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from jiten_fake_server import install
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.review_providers import credential_account_key

KEY = "jiten-test-key"
PAIRS = [(100 + n, 0) for n in range(30)]


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _service(tmp_path: Path) -> LightNovelService:
    service = LightNovelService(_config(tmp_path))
    service.save_settings({"jiten_api_key": KEY, "parse_ahead": "current"})
    return service


def _join(prefix: str) -> None:
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(timeout=5)


# --------------------------------------------------------------- JITEN-3: states


def test_word_states_survive_a_restart_for_fifteen_minutes(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    first = _service(tmp_path)
    assert len(first.jiten_live_states(PAIRS)) == 30
    _join("ln-jiten-overview")
    assert server.count("lookup-vocabulary") == 1
    clock.sleep(10 * 60)
    again = LightNovelService(_config(tmp_path))  # the next launch
    rows = again.jiten_live_states(PAIRS)
    assert [row["knowledgeStatus"] for row in rows] == ["live_state_cache"] * 30
    assert server.count("lookup-vocabulary") == 1
    clock.sleep(6 * 60)
    again.jiten_live_states(PAIRS)
    assert server.count("lookup-vocabulary") == 2


def test_a_changed_overview_version_makes_every_state_stale(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    service.jiten_live_states(PAIRS)
    _join("ln-jiten-overview")
    clock.sleep(61)
    service.jiten_live_states(PAIRS)  # fresh: no lookup, but the version is checked again
    _join("ln-jiten-overview")
    assert server.count("lookup-vocabulary") == 1 and server.count("overview-version") == 2
    service.jiten_live_states(PAIRS)  # checked a few seconds ago: not again
    assert server.count("overview-version") == 2
    server.overview_version = 2  # a review on the Jiten site
    server.states[(100, 0)] = [2]
    clock.sleep(61)
    service.jiten_live_states(PAIRS)
    _join("ln-jiten-overview")
    rows = service.jiten_live_states(PAIRS)
    assert server.count("lookup-vocabulary") == 2
    assert rows[0]["states"] == ["mature"]


def test_old_one_off_study_parses_are_pruned_but_chapters_stay(tmp_path):
    service = LightNovelService(_config(tmp_path))
    now = time.time()
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_books(id,title,file_path,file_type,created_at,updated_at) VALUES(1,'b','/x','txt',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(1,0,'c','猫','chapter')"
        )
        for key, used in (
            ("chapter", now - 400 * 86400),
            ("old", now - 100 * 86400),
            ("a", now - 3),
            ("b", now - 2),
            ("c", now - 1),
        ):
            conn.execute(
                "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at,used_at) VALUES(?,?,?,?,?)",
                (key, "{}", "jiten-v1", used, used),
            )
    service.STUDY_PARSE_KEEP_ROWS = 2
    assert service.prune_study_parses() == 2
    with service._connection() as conn:
        kept = {row[0] for row in conn.execute("SELECT text_hash FROM ln_parse_cache")}
    assert kept == {"chapter", "b", "c"}


# ---------------------------------------------------------- JITEN-13: in-flight


def test_concurrent_callers_share_one_lookup_of_the_same_words(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    gate, entered = threading.Event(), threading.Event()
    original = server.handle

    def handle(method, url, **kwargs):
        if url.endswith("lookup-vocabulary"):
            entered.set()
            assert gate.wait(5)
        return original(method, url, **kwargs)

    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: handle("POST", str(url), **kwargs))
    results: dict[str, list] = {}
    first = threading.Thread(target=lambda: results.update(a=service.jiten_live_states(PAIRS)))
    second = threading.Thread(target=lambda: results.update(b=service.jiten_live_states(PAIRS[:10])))
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.3)  # the second caller is waiting for the first one's answer
    gate.set()
    first.join(5)
    second.join(5)
    assert len(results["a"]) == 30 and len(results["b"]) == 10
    assert server.count("lookup-vocabulary") == 1


# ------------------------------------------------- JITEN-4: known-ids/amount


def test_the_n_plus_one_ceiling_is_cached_for_hours_and_never_waited_for(tmp_path, monkeypatch):
    from pudge.web_app import WebAppApi

    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = service
    first = api.study_states({"backend": "jiten", "words": [[100, 0]]})
    assert first["optimalWordLimit"] == 1000  # the default now; Jiten is asked in the background
    _join("ln-jiten-amount")
    for _ in range(30):  # half an hour of reading, one batch a minute
        clock.sleep(60)
        result = api.study_states({"backend": "jiten", "words": [[100, 0]]})
    assert result["optimalWordLimit"] == 3000
    assert server.count("known-ids/amount") == 1
    # A failure is not asked again for 10 minutes.
    clock.sleep(6 * 3600)
    server.fail["known-ids/amount"] = [500, 500, 500]
    assert service.jiten_optimal_word_stats() == {"matureWords": 1500, "optimalWordLimit": 3000}
    calls = server.count("known-ids/amount")
    clock.sleep(5 * 60)
    service.jiten_optimal_word_stats()
    assert server.count("known-ids/amount") == calls
    clock.sleep(6 * 60)
    service.jiten_optimal_word_stats()
    assert server.count("known-ids/amount") == calls + 1


# ----------------------------------------------- audiobook live text (JITEN-2)


def test_an_audiobook_minute_of_live_text_costs_at_most_a_few_jiten_requests(tmp_path, monkeypatch):
    """The sidebar re-renders the live window about every 15 s; each time it asks for the
    window's words. Before: 3 lookup POSTs (and an amount GET) per re-render."""
    from pudge.web_app import WebAppApi

    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = service
    chapter_pairs = [[100 + n, 0] for n in range(1500)]
    for second in range(0, 60, 15):
        window = chapter_pairs[second * 5 : second * 5 + 40]
        api.study_states({"backend": "jiten", "words": window})
        clock.sleep(15)
    _join("ln-jiten-")
    assert server.count() <= 6
    assert server.count("lookup-vocabulary") <= 4


AUDIO_WINDOW = r"""
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_states = async payload => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) =>
  ({ok: true, wordId: w, readingIndex: r, states: ['young'], normalizedState: 'young'}))});
"""


def test_a_live_text_window_asks_only_for_the_words_it_shows(page) -> None:
    result = page.run(
        AUDIO_WINDOW
        + r"""
        const vocabulary = Array.from({length: 1500}, (_, n) => ({wordId: 100 + n, readingIndex: 0, spelling: 'w' + n}));
        const tokens = [{wordId: 100, readingIndex: 0, start: 0, end: 1}, {wordId: 905, readingIndex: 0, start: 1, end: 2}];
        const box = document.createElement('div');
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['猫犬'], tokens: [tokens], vocabulary}, {backend: 'jiten'});
        document.body.appendChild(box);
        await __tick(100);
        return __apiCalls.filter(c => c.name === 'study_states').map(c => c.args[0].words);
        """,
    )
    assert result.value == [[[100, 0], [905, 0]]]


# ------------------------------------------------------------- JITEN-5: sidebar


class _Anime:
    def anime_list(self):
        return []


def _sidebar_api(service):
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = service
    api.manager = SimpleNamespace(db=_Anime())
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None)
    api._review_gate_lock = threading.RLock()
    api._review_gate_prefetch_account_key = credential_account_key("jiten", KEY)
    api._review_gate_verified_keys = set()
    api._review_gate_prefetch_cards = []
    api._review_gate_prefetch_fetched_at = 0.0
    return api


def _due(n: int) -> list[dict]:
    return [{"wordId": 500 + i, "readingIndex": 0, "wordTextPlain": f"語{i}"} for i in range(n)]


def test_a_sidebar_refill_reuses_the_review_gates_verified_cards(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    api = _sidebar_api(service)
    server.due_cards = _due(24)
    # Cold: every candidate's review history is read once (the gate's warm-up does this).
    cold = api.sidebar_due_review_cards(8)
    assert len(cold["cards"]) == 8
    assert server.count("review-history") == 24
    # The verified cards are trusted now: the next refill reads no history again
    # (before: 24 history reads on every refill).
    server.log.clear()
    api.sidebar_due_review_cards(8, [f"{c['wordId']}:0" for c in cold["cards"]])
    assert server.count("review-history") == 0
    assert server.count("study-batch") >= 1
    # With the gate's warm pool fresh, a refill sends nothing to Jiten for cards.
    api._review_gate_prefetch_cards = [{**card, "pudgeCardKey": f"{card['wordId']}:0"} for card in _due(40)]
    api._review_gate_prefetch_fetched_at = time.time()
    server.log.clear()
    pooled = api.sidebar_due_review_cards(8, ["500:0"])
    assert len(pooled["cards"]) == 8 and "500:0" not in {f"{c['wordId']}:0" for c in pooled["cards"]}
    assert server.count("study-batch") == 0 and server.count("review-history") == 0
    # A card graded in the sidebar leaves the gate's pool.
    monkeypatch.setattr(service, "strict_review_submit", lambda *a, **k: {"saved": True})
    api.sidebar_due_review_submit(501, 0, "good", "attempt-1")
    assert "501:0" not in {card["pudgeCardKey"] for card in api._review_gate_prefetch_cards}


SIDEBAR_EMPTY = r"""
__api.sidebar_due_review_cards = async () => ({ok: true, cards: []});
__api.sidebar_review_settings = async value => ({interval: value || 'continuous'});
"""


def test_an_empty_sidebar_queue_backs_off_instead_of_polling_every_minute(page) -> None:
    result = page.run(
        r"""
        await __tick(1100);
        const count = () => __apiCalls.filter(c => c.name === 'sidebar_due_review_cards').length;
        const start = count();
        await __tick(30 * 60_000);  // half an hour with nothing due
        return {start, total: count()};
        """,
        before=SIDEBAR_EMPTY,
    )
    assert result.value["start"] >= 1
    assert result.value["total"] <= 8  # before: about one every 45-60 s (30+)


# ------------------------------------------------------------- JITEN-6: manga


def test_manga_bubbles_are_parsed_as_arrays_and_each_stays_cached_on_its_own(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    bubbles = [f"吾輩は猫である{n}。\n名前はまだ無い{n}。" for n in range(300)]
    assert service.parse_study_texts(bubbles) == 300
    assert server.count("reader/parse") == 1  # before: 300 requests of at least 1000 characters
    for bubble in bubbles[:20]:
        parsed = service.parse_study_text(bubble)
        assert len(parsed["paragraphs"]) == 2 and len(parsed["tokens"]) == 2
    assert server.count("reader/parse") == 1
    assert service.parse_study_texts(bubbles) == 0


# ------------------------------------------------------------- JITEN-7: words


def test_a_parsed_text_fetches_no_dictionary_entries(tmp_path, monkeypatch):
    from pudge.web_app import WebAppApi

    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = service
    result = api.study_parse_text("吾輩は猫である。")
    assert result["vocabulary"]
    service._word_repository().pool.shutdown(wait=True)
    assert server.count("/info") == 0


def test_dictionary_entries_are_kept_thirty_days(tmp_path):
    from pudge.jiten_words import JitenWordRepository

    repository = JitenWordRepository(tmp_path / "words.sqlite")
    try:
        repository.put(
            "acct", {"wordId": 1, "readingIndex": 0, "reading": "ねこ", "dictionary_complete": True}
        )
        with repository.lock:
            key = ("acct", repository.key({"wordId": 1, "readingIndex": 0}))
            data, _updated = repository.memory[key]
            repository.memory[key] = (data, time.time() - 20 * 86400)
        fetched = []
        repository.resolve("acct", {"wordId": 1, "readingIndex": 0}, lambda: fetched.append(1))
        repository.pool.shutdown(wait=True)
        assert fetched == []
        assert repository.NOT_FOUND_TTL == 86400
    finally:
        repository.pool.shutdown()


# ------------------------------------------------------- JITEN-9: bound decks


def test_a_bound_decks_stats_are_cached(tmp_path, monkeypatch):
    service = _service(tmp_path)
    calls: list = []

    def detail(deck_id):
        calls.append(deck_id)
        return {"title": "Deck", "coverage": 80, "difficulty": 2.0, "characterCount": 1000}, []

    monkeypatch.setattr(service, "_jiten_detail_with_subdecks", detail)
    monkeypatch.setattr(service, "book", lambda book_id: {"jiten_deck_id": 77, "anilist_id": 0})
    first = service.jiten_book_stats(1)
    second = service.jiten_book_stats(1)
    assert first == second and calls == [77]


# ------------------------------------------------------------ JITEN-10: tokens


def test_reader_payloads_carry_no_api_keys(tmp_path, monkeypatch):
    _clock, _server = install(monkeypatch)
    service = _service(tmp_path)
    settings = service.parse_study_text("吾輩は猫である。")["settings"]
    assert settings["jiten_api_key"] == "" and settings["jiten_configured"] is True
    assert settings["jpdb_configured"] is False
    # The Settings form gets the mask, never the key (saving the mask keeps the key).
    from pudge.secrets_store import SECRET_MASK

    assert service.settings_payload()["jiten_api_key"] == SECRET_MASK
    assert service.settings().jiten_api_key == KEY


def test_clearing_the_key_in_pudge_clears_jitenmpvs_copy(tmp_path, monkeypatch):
    from pudge import first_experience

    config = tmp_path / "jiten-mpv" / "config.json"
    config.parent.mkdir()
    monkeypatch.setattr(first_experience, "_jiten_paths", lambda: (tmp_path, tmp_path / "x.lua", config))
    config.write_text(json.dumps({"api_key": "old", "other": 1}), encoding="utf-8")
    assert first_experience.forget_jiten_api_key("old") is True
    assert json.loads(config.read_text()) == {"other": 1}
    # A key the user gave JitenMPV itself stays.
    config.write_text(json.dumps({"api_key": "their-own"}), encoding="utf-8")
    assert first_experience.forget_jiten_api_key("old") is False
    assert json.loads(config.read_text()) == {"api_key": "their-own"}


# ------------------------------------------------------- JITEN-11: parseVersion


def test_parse_version_is_kept_and_an_older_parser_is_noticed_once_per_launch(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    service.parse_study_text("吾輩は猫である。")  # also starts the once-per-launch version check
    _join("ln-jiten-version")
    assert server.count("reader/version") == 1
    with service._connection() as conn:
        assert {row[0] for row in conn.execute("SELECT parse_version FROM ln_parse_cache")} == {"pv-1"}
        conn.execute(
            "INSERT INTO ln_books(id,title,file_path,file_type,created_at,updated_at) VALUES(5,'b','/y','txt',0,0)"
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(5,0,'c','猫だ','ch-5')"
        )
    service.jiten_preparse("猫だ", "ch-5")
    server.parse_version = "pv-2"
    assert service._check_jiten_reader_version() == "pv-2"
    with service._connection() as conn:
        stale = {row[0] for row in conn.execute("SELECT text_hash FROM ln_parse_stale")}
    assert stale == {"ch-5"}  # re-parsed in the background when it is opened
    before = server.count("reader/parse")
    service.parse_study_text("吾輩は猫である。")  # a study text is simply parsed again
    assert server.count("reader/parse") == before + 1
    service.parse_study_text("吾輩は猫である。")
    assert server.count("reader/parse") == before + 1
    # Once per launch: later openings do not ask again.
    service._ensure_jiten_reader_version()
    _join("ln-jiten-version")
    assert server.count("reader/version") == 2  # the start-up check and the explicit one above
