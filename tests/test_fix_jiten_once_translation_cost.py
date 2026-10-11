"""A Jiten review/add with an unknown outcome is never sent twice; mining needs a fresh
deck list; Google only as the fallback; the subtitle prewarm is small, hits, and is off
for paid LLMs unless turned on."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge import mpv_study
from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.review_providers import ReviewOutcomeUnknown
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


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


class Reviews:
    """JitenReviewProvider.submit_review: answers from `outcomes` (an exception or a dict)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.attempts: list[str] = []

    def __call__(self, word_id, reading_index, grade, *, attempt_id, **_kw):
        self.attempts.append(attempt_id)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "unknown":
            raise ReviewOutcomeUnknown("jiten", attempt_id, "Jiten review outcome is unknown: timed out")
        return {"ok": True, "outcome": "confirmed", "provider": "jiten", "attempt_id": attempt_id}


# ------------------------------------------------------------- 1. review sent once


def test_regrade_after_unknown_review_resends_the_same_attempt(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    reviews = Reviews("unknown", "ok", "ok")
    monkeypatch.setattr("pudge.light_novels.JitenReviewProvider.submit_review", reviews)

    review = lambda attempt: service.study_action(  # noqa: E731
        "jiten", "review", 120, 2, grade="good", attempt_id=attempt, id_namespace="jiten"
    )
    first = review("ui-attempt-1")
    assert first["outcome"] == "unknown" and first["ok"] is False
    # The user grades again: Jiten gets the first attempt id again (its ClientRequestId
    # dedupe answers it), not a fresh one that would save a second review.
    assert review("ui-attempt-2")["outcome"] == "confirmed"
    # Confirmed: the next review of the word is a new review with its own id.
    review("ui-attempt-3")
    assert reviews.attempts == ["ui-attempt-1", "ui-attempt-1", "ui-attempt-3"]


def test_unknown_review_attempt_is_per_word_and_expires(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    reviews = Reviews("unknown", "ok", "unknown", "ok")
    monkeypatch.setattr("pudge.light_novels.JitenReviewProvider.submit_review", reviews)
    service.study_action("jiten", "review", 120, 2, attempt_id="a-120", id_namespace="jiten")
    service.study_action("jiten", "review", 121, 0, attempt_id="b-121", id_namespace="jiten")
    service.study_action("jiten", "review", 122, 0, attempt_id="c-122", id_namespace="jiten")
    monkeypatch.setattr(service, "STUDY_UNKNOWN_REVIEW_WINDOW_SECONDS", -1.0)
    service.study_action("jiten", "review", 122, 0, attempt_id="d-122", id_namespace="jiten")
    assert reviews.attempts == ["a-120", "b-121", "c-122", "d-122"]


# ------------------------------------------------------------- 1. add sent once


def _jiten_http(monkeypatch, answers):
    """httpx.post for Jiten: `answers` per URL suffix (a list consumed in order)."""
    calls: list[tuple[str, dict]] = []

    def post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        for suffix, queue in answers.items():
            if url.endswith(suffix):
                answer = queue.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(200, request=httpx.Request("POST", url), json=answer)
        raise AssertionError(url)

    monkeypatch.setattr("pudge.light_novels.httpx.post", post)
    return calls


def test_add_with_timeout_is_unknown_and_the_retry_checks_the_deck_first(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    calls = _jiten_http(
        monkeypatch,
        {
            "srs/study-decks/12/words": [httpx.ReadTimeout("timed out")],
            # The retry reads the membership: the first add did reach Jiten.
            "reader/lookup-vocabulary": [{"result": [[0]], "decks": [[12]]}],
            "user/example-sentences/120/2": [{}],
        },
    )
    add = lambda: service.study_action(  # noqa: E731
        "jiten", "add", 120, 2, deck_id="12", sentence="猫が好きだ。", surface="猫", media_source="manga"
    )
    first = add()
    assert first["ok"] is False and first["outcome"] == "unknown"
    assert first["message"] == en("error.ln.jitenAddOutcomeUnknown")
    second = add()
    assert second["ok"] is True and second.get("already_added") is True
    urls = [url.rsplit("/api/", 1)[-1] for url, _ in calls]
    # One add only; the custom sentence the first attempt never reached is sent once.
    assert urls == ["srs/study-decks/12/words", "reader/lookup-vocabulary", "user/example-sentences/120/2"]


def test_add_retry_adds_when_the_first_did_not_arrive_and_waits_when_jiten_cannot_say(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    mutations: list[str] = []
    state = {"rows": []}

    def mutation(action, payload=None):
        mutations.append(action)
        if len(mutations) == 1:
            from pudge.light_novels import JitenMutationUnknown

            raise JitenMutationUnknown("Jiten mutation outcome is unknown after HTTP 502")
        return {}

    def live(pairs, *, force=False):
        assert force is True
        rows = state["rows"]
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(service, "_jiten_mutation", mutation)
    monkeypatch.setattr(service, "jiten_live_states", live)
    add = lambda: service.study_action("jiten", "add", 7, 0, deck_id="12")  # noqa: E731
    assert add()["outcome"] == "unknown"
    # Jiten cannot say now (lookup fails / no deck list): nothing is added again.
    state["rows"] = LightNovelError("offline")
    blocked = add()
    assert blocked["outcome"] == "unknown" and blocked["message"] == en("error.ln.jitenAddRecheckFailed")
    state["rows"] = [{"states": ["new"], "knowledgeStatus": "live_provider_batch", "stale": False}]
    assert add()["outcome"] == "unknown"
    assert mutations == ["srs/study-decks/12/words"]
    # Jiten says the word is in no deck: the first add did not arrive, add it now.
    state["rows"] = [{"states": ["new"], "studyDeckIds": [], "knowledgeStatus": "live_provider_batch"}]
    assert add()["ok"] is True
    assert mutations == ["srs/study-decks/12/words", "srs/study-decks/12/words"]
    # Settled: a later add does not need the check.
    state["rows"] = LightNovelError("not called")
    assert add()["ok"] is True


def test_review_that_mines_with_unknown_add_reviews_nothing_and_regrade_does_not_add_again(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    mutations: list[str] = []
    sentences: list[str] = []
    memberships = [[], [12]]

    def mutation(action, payload=None):
        mutations.append(action)
        from pudge.light_novels import JitenMutationUnknown

        raise JitenMutationUnknown("Jiten mutation outcome is unknown: timed out")

    monkeypatch.setattr(service, "_jiten_mutation", mutation)
    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [
            {"states": ["new"], "studyDeckIds": memberships.pop(0), "knowledgeStatus": "live_provider_batch"}
        ],
    )
    monkeypatch.setattr(service, "_jiten_add_custom_sentence", lambda _w, _r, text: sentences.append(text))
    reviews = Reviews("ok")
    monkeypatch.setattr("pudge.light_novels.JitenReviewProvider.submit_review", reviews)
    grade = lambda attempt: service.study_action(  # noqa: E731
        "jiten",
        "review",
        9,
        0,
        deck_id=12,
        sentence="猫だ。",
        surface="猫",
        attempt_id=attempt,
        id_namespace="jiten",
    )
    first = grade("g1")
    assert first["outcome"] == "unknown" and first["stage"] == "add"
    assert first["message"] == en("error.ln.jitenMineOutcomeUnknown") and reviews.attempts == []
    second = grade("g2")
    # The word is in the deck now (the add arrived): no second add; the review and the
    # card's sentence are sent once.
    assert second["outcome"] == "confirmed" and second["auto_added"] is True
    assert mutations == ["srs/study-decks/12/words"]
    assert sentences == ["**猫**だ。"] and reviews.attempts == ["g2"]


# ------------------------------------------------------------- 2. mining needs fresh membership


@pytest.mark.parametrize(
    "rows",
    [
        # Jiten answered without the deck list: membership unknown.
        [{"states": ["new"], "knowledgeStatus": "live_provider_batch", "stale": False}],
        # The lookup failed and the last cached snapshot came back.
        [{"states": ["new"], "studyDeckIds": [], "knowledgeStatus": "stale_live_state_cache", "stale": True}],
        LightNovelError("Jiten request failed"),
    ],
    ids=["no-deck-list", "stale-cache", "lookup-failed"],
)
def test_review_does_not_mine_without_a_fresh_deck_list(tmp_path, monkeypatch, rows) -> None:
    service = _service(tmp_path)
    mutations: list[str] = []

    def live(pairs, *, force=False):
        assert force is True
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(service, "jiten_live_states", live)
    monkeypatch.setattr(
        service, "_jiten_mutation", lambda action, payload=None: mutations.append(action) or {}
    )
    reviews = Reviews("ok")
    monkeypatch.setattr("pudge.light_novels.JitenReviewProvider.submit_review", reviews)
    result = service.study_action("jiten", "review", 5, 0, deck_id=12, attempt_id="m1", id_namespace="jiten")
    assert mutations == []
    # The review itself is still saved, and the reader is told the word was not added.
    assert (
        result["outcome"] == "confirmed" and result["mining_skipped"] is True and reviews.attempts == ["m1"]
    )


def test_stale_cached_membership_never_mines_through_the_real_lookup(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    lookups = [{"result": [[0]], "decks": [[]]}, httpx.ConnectError("offline")]
    calls = _jiten_http(monkeypatch, {"reader/lookup-vocabulary": lookups})
    monkeypatch.setattr("pudge.light_novels.time.sleep", lambda _s: None)
    reviews = Reviews("ok")
    monkeypatch.setattr("pudge.light_novels.JitenReviewProvider.submit_review", reviews)
    assert service.jiten_live_states([(5, 0)])[0]["studyDeckIds"] == []  # cached "in no deck"
    lookups.extend([httpx.ConnectError("offline")] * 2)  # the forced re-read fails (with retries)
    result = service.study_action("jiten", "review", 5, 0, deck_id=12, attempt_id="m2", id_namespace="jiten")
    assert result["mining_skipped"] is True
    assert not [url for url, _ in calls if "study-decks" in url]


# ------------------------------------------------------------- 3. Google only as the fallback


def test_llm_translates_without_context_and_google_only_when_the_fallback_is_on(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path)
    service.config.llm.enabled = True
    service.config.llm.model = "local-model"
    llm: list[tuple[str, str]] = []
    google: list[str] = []
    monkeypatch.setattr(
        service,
        "_translate_local_llm",
        lambda text, context, target, glossary: llm.append((text, context)) or "LLM",
    )
    monkeypatch.setattr(service, "_translate_online", lambda text, target: google.append(text) or "G")
    # A manga bubble / first line of a chapter / first cue: no context, still the LLM.
    assert service.translate_selection("猫だ。", "")["provider"] == "local_llm"
    assert llm == [("猫だ。", "")] and google == []

    def down(*_a):
        raise RuntimeError("llm down")

    monkeypatch.setattr(service, "_translate_local_llm", down)
    service.save_settings({"translation_google_fallback": False})
    with pytest.raises(LightNovelError):
        service.translate_selection("犬だ。", "")
    assert google == []
    service.save_settings({"translation_google_fallback": True})
    assert service.translate_selection("鳥だ。", "")["provider"] == "google"
    # The prewarm never falls back to Google, whatever the setting.
    with pytest.raises(LightNovelError):
        service.translate_selection("魚だ。", "", google_fallback=False)
    assert google == ["鳥だ。"]
    # LLM off: Google is the translator.
    service.config.llm.enabled = False
    assert service.translate_selection("馬だ。", "")["provider"] == "google"


# ------------------------------------------------------------- 4. prewarm


def _srt(path: Path, count: int, step: float = 4.0) -> Path:
    rows = []
    for i in range(count):
        start, end = i * step, i * step + 2

        def stamp(value: float) -> str:
            return f"00:{int(value) // 60:02d}:{int(value) % 60:02d},000"

        rows.append(f"{i + 1}\n{stamp(start)} --> {stamp(end)}\n台詞{i}\n")
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def _player(monkeypatch, states):
    """mpv over IPC: each poll answers the next (time-pos, pause, path, hits, misses)."""
    polls: list[int] = []

    class Player:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def settimeout(self, _seconds):
            pass

        def connect(self, _path):
            polls.append(len(polls))
            if not states:
                raise OSError("mpv exited")

        def sendall(self, _data):
            pass

        def makefile(self, _mode):
            position, paused, path, hits, misses = states.pop(0)
            lines = [
                {"request_id": 1, "data": position, "error": "success"},
                {"request_id": 2, "data": paused, "error": "success"},
                {"request_id": 3, "data": path, "error": "success" if path else "property unavailable"},
                {"request_id": 4, "data": hits, "error": "success"},
                {"request_id": 5, "data": misses, "error": "success"},
            ]
            return io.StringIO("".join(json.dumps(line) + "\n" for line in lines))

    monkeypatch.setattr(mpv_study, "socket", SimpleNamespace(AF_UNIX=0, SOCK_STREAM=0, socket=Player))
    monkeypatch.setattr(mpv_study.time, "sleep", lambda _s: None)
    return polls


def _fake_api(llm=None, **settings):
    calls: list[tuple[str, str, bool]] = []

    def translate_selection(text, context, *, media_id=None, google_fallback=True):
        calls.append((text, context, google_fallback))
        return {"translation": text, "provider": "local_llm", "cached": False}

    api = mpv_study.SubtitleStudyApi.__new__(mpv_study.SubtitleStudyApi)
    api.media_id = 3
    api.logger = logging.getLogger("pudge")
    api.service = SimpleNamespace(
        config=SimpleNamespace(
            llm=llm or SimpleNamespace(enabled=True, base_url="http://127.0.0.1:11434", api_key="")
        ),
        translate_selection=translate_selection,
        settings=lambda: SimpleNamespace(**settings),
    )
    return api, calls


def test_prewarm_keeps_only_the_next_cues_translated_while_playing(tmp_path, monkeypatch, caplog) -> None:
    subtitles = _srt(tmp_path / "ep.ja.srt", 40)
    episode = "/media/Show - 05.mkv"
    states = (
        [(0.0, False, episode, 0, 0)] * 12  # at the start: cues 0-7, then nothing more
        + [(9.0, False, episode, 0, 0)] * 6  # two cues later: cues 8 and 9
        + [(60.0, True, episode, 1, 0)] * 5  # paused far ahead: no requests
        + [(60.0, False, episode, 2, 1)]  # playing again
        + [(64.0, False, "/media/Show - 06.mkv", 0, 0)]  # the next episode
    )
    _player(monkeypatch, states)
    api, calls = _fake_api()
    caplog.set_level(logging.INFO, logger="pudge")

    result = api.prewarm_file(subtitles, ipc_socket=str(tmp_path / "mpv.sock"))

    texts = [text for text, _context, _fallback in calls]
    assert texts == [f"台詞{i}" for i in range(0, 10)] + ["台詞15"]
    assert all(fallback is False for _t, _c, fallback in calls)
    assert result["stop"] == "episode_changed"
    # One summary line for the episode, with the player's on-demand hits/misses.
    summary = [r.getMessage() for r in caplog.records if "subtitle.prewarm_summary" in r.getMessage()]
    assert len(summary) == 1
    assert (
        "prewarmed=11" in summary[0]
        and "on_demand_hits=2" in summary[0]
        and "on_demand_misses=1" in summary[0]
    )


def test_on_demand_translation_uses_the_prewarmed_cache(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.config.llm.enabled = True
    service.config.llm.model = "local-model"
    llm_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_translate_local_llm",
        lambda text, context, target, glossary: llm_calls.append(text) or f"T:{text}",
    )
    monkeypatch.setattr(service, "_translate_online", lambda *_a: pytest.fail("no Google"))
    subtitles = _srt(tmp_path / "ep.ja.srt", 30)
    api = mpv_study.SubtitleStudyApi.__new__(mpv_study.SubtitleStudyApi)
    api.service, api.media_id, api.logger = service, None, logging.getLogger("pudge")
    monkeypatch.setattr(mpv_study.time, "sleep", lambda _s: None)

    api.prewarm_file(subtitles, start_seconds=80.0)  # cues 20-27 (one window, no player)
    assert llm_calls == [f"台詞{i}" for i in range(20, 28)]

    # The player resumed at 80 s: its own history holds only what was seen since then.
    seen = "Previous Japanese subtitles:\n台詞20\n台詞21"
    hit = api.translate_visible("台詞22", seen, subtitle_path=subtitles, position=89.0)
    assert hit["cached"] is True and hit["translation"] == "T:台詞22"
    assert len(llm_calls) == 8
    # Text not in the file (another track) keeps the player's own context.
    other = api.translate_visible("別の字幕", seen, subtitle_path=subtitles, position=89.0)
    assert other["cached"] is False and llm_calls[-1] == "別の字幕"


@pytest.mark.parametrize(
    ("base_url", "api_key", "allowed", "runs"),
    [
        ("https://api.openai.com/v1", "sk-1", False, False),
        ("https://openrouter.ai/api/v1", "", False, False),
        ("http://127.0.0.1:1234/v1", "sk-local-proxy", False, False),
        ("https://api.openai.com/v1", "sk-1", True, True),
        ("http://127.0.0.1:11434", "", False, True),
        ("http://192.168.1.20:11434", "", False, True),
        ("http://studio.local:1234/v1", "", False, True),
    ],
)
def test_prewarm_is_off_for_paid_llms_unless_turned_on(tmp_path, base_url, api_key, allowed, runs) -> None:
    subtitles = _srt(tmp_path / "ep.ja.srt", 3)
    llm = SimpleNamespace(enabled=True, provider="openai", base_url=base_url, api_key=api_key)
    api, calls = _fake_api(llm, translation_prewarm_remote=allowed)
    result = api.prewarm_file(subtitles)
    assert bool(calls) is runs
    if not runs:
        assert result["reason"] == "remote_llm"


def test_prewarm_remote_setting_is_saved(tmp_path) -> None:
    service = _service(tmp_path)
    assert service.settings().translation_prewarm_remote is False
    service.save_settings({"translation_prewarm_remote": True})
    assert service.settings().translation_prewarm_remote is True
    assert service.settings_payload()["translation_prewarm_remote"] is True
    assert en("ui.prewarmSubtitleTranslationsRemote").startswith("Also prepare subtitle translations")
    assert ru("ui.prewarmSubtitleTranslationsRemote").startswith("Готовить переводы субтитров")


def test_prewarm_remote_switch_is_in_llm_settings(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const box = $('s_ln_translation_prewarm_remote'), shown = box.checked;
        box.checked = true; box.dispatchEvent(new Event('change', {bubbles: true}));
        await __click('#saveSettings'); await __tick(50);
        return {shown, label: box.parentElement.innerText};
        """,
        state={"settings": {"llm_enabled": True}},
    )
    assert result.value["shown"] is False
    assert result.value["label"] == en("ui.prewarmSubtitleTranslationsRemote")
    assert [call[0]["ln_translation_prewarm_remote"] for call in result.calls_of("save_settings")] == [True]


TRANSLATE_TWICE = r"""
local mp = require('mp')
local original = mp.command_native_async
mp.command_native_async = function(tbl, cb)
    if tbl.name == 'subprocess' then
        print('CALL\t' .. tostring(tbl.playback_only) .. '\t' .. (table.concat(tbl.args, '\31'):gsub('\n', '\\n')))
    end
    return original(tbl, cb)
end
sim.set('sub-text', '')
sim.fire('file-loaded')
sim.play(3)
sim.set('time-pos', 41.5)
sim.set('sub-text', '台詞10')
sim_stdout = 'PUDGE_TRANSLATION_JSON={"translation":"Line ten","cached":true}\n'
assert(sim.press('Ctrl+y'))
sim_stdout = 'PUDGE_TRANSLATION_JSON={"translation":"Line ten","cached":false}\n'
assert(sim.press('Ctrl+y'))
sim.set('secondary-sub-text', '- Wait!')
assert(sim.press('Ctrl+y'))
print('COUNTS\t' .. tostring(sim.props['user-data/pudge/translation-hits']) .. '/' .. tostring(sim.props['user-data/pudge/translation-misses']))
sim.fire('file-loaded')
print('RESET\t' .. tostring(sim.props['user-data/pudge/translation-hits']) .. '/' .. tostring(sim.props['user-data/pudge/translation-misses']))
"""


@needs_lua
def test_player_asks_for_the_file_line_and_counts_cache_hits(tmp_path: Path) -> None:
    out = run_mpv_scenario(
        tmp_path,
        TRANSLATE_TWICE,
        scripts=[ANILIST],
        env={
            "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE": "Ctrl+y",
            "PUDGE_SUBTITLE_PATH": "/media/Show - 05.ja.srt",
            "PUDGE_PYTHON": "/app/python",
        },
    )
    rows = [line.split("\t", 2) for line in out.splitlines() if "\t" in line]
    calls = [row[2].split("\x1f") for row in rows if row[0] == "CALL"]
    values = {row[0]: row[1] for row in rows if row[0] in ("COUNTS", "RESET")}
    translates = [args for args in calls if "--subtitle-translate" in args]
    translate = translates[0]
    options = dict(zip(translate[4::2], translate[5::2]))
    # The on-demand request names the prepared file and the position, so Python builds
    # the same text/context as the prewarm.
    assert options["--subtitle-study-file"] == "/media/Show - 05.ja.srt"
    assert float(options["--subtitle-study-time"]) == pytest.approx(41.5)
    assert values == {"COUNTS": "1/2", "RESET": "0/0"}
    # The English line (supporting context) goes as one argument: it may start with "-".
    assert translates[-1][-1] == "--subtitle-study-secondary=- Wait!"
    from pudge.cli import build_parser

    parsed = build_parser().parse_args(translates[-1][4:])
    assert parsed.subtitle_study_secondary == "- Wait!" and parsed.subtitle_study_time == pytest.approx(41.5)
    assert str(parsed.subtitle_study_file) == "/media/Show - 05.ja.srt"


def test_on_demand_context_with_the_english_line_matches_the_player_format(tmp_path) -> None:
    subtitles = _srt(tmp_path / "ep.ja.srt", 3)
    api, calls = _fake_api()
    api.translate_visible("台詞2", "", subtitle_path=subtitles, position=8.5, secondary="- Wait!")
    assert calls[-1][:2] == (
        "台詞2",
        "Previous Japanese subtitles:\n台詞0\n台詞1\n"
        "Corresponding English subtitle (supporting context only):\n- Wait!",
    )


# ------------------------------------------------------------- 1. the reader card

READER = r"""
window.__answers = []; window.__held = null;
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_states = async payload => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) =>
  ({ok: true, wordId: w, readingIndex: r, states: ['young'], normalizedState: 'learning', studyDeckIds: [12]}))});
__api.study_action = payload => window.__held
  ? new Promise(resolve => { window.__release = () => resolve(window.__answers.shift() || {ok: true, outcome: 'confirmed'}); })
  : Promise.resolve(window.__answers.shift() || {ok: true, outcome: 'confirmed'});
"""

READER_RUN = r"""
const words = [['少し', 1], ['猫', 2]];
let pos = 0;
const tokens = words.map(([text, wordId]) => { const start = pos; pos += text.length; return {wordId, readingIndex: 0, start, end: pos}; });
const vocabulary = words.map(([text, wordId]) => ({wordId, readingIndex: 0, spelling: text, states: ['young'], studyDeckIds: [12]}));
const box = document.createElement('div');
box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['少し猫'], tokens: [tokens], vocabulary}, {backend: 'jiten'});
document.body.appendChild(box);
await __tick(100);
const open = async text => {
  const node = [...box.querySelectorAll('[data-pudge-study-token]')].find(n => n.textContent === text);
  await __click(`[data-pudge-study-token="${node.dataset.pudgeStudyToken}"]`); await __tick(50);
};
const good = async () => { await __click('#pudgeStudyCard [data-pudge-study-review="good"]'); await __tick(50); };
const actions = kind => __apiCalls.filter(c => c.name === 'study_action' && c.args[0].action === kind).length;
const stateReads = () => __apiCalls.filter(c => c.name === 'study_states').length;
const out = {};
"""


def test_reader_card_cannot_send_a_second_review_while_the_first_is_on_its_way(page) -> None:
    result = page.run(
        READER_RUN
        + r"""
        window.__held = true;
        await open('猫'); await good();
        await open('猫');                      // reopened while the first review waits for Jiten
        const button = document.querySelector('#pudgeStudyCard [data-pudge-study-review="good"]');
        out.blocked = {disabled: button.getAttribute('aria-disabled'), title: button.title};
        await good();
        out.whileWaiting = actions('review');
        window.__answers.push({ok: false, outcome: 'unknown', attempt_id: 'a1', message: 'Jiten review outcome is unknown: timed out'});
        window.__release(); window.__held = false; await __tick(50);
        out.toasts = __toasts();
        // The answer came: the reopened card can grade (Jiten gets the same attempt id again).
        out.reopened = document.querySelector('#pudgeStudyCard [data-pudge-study-review="good"]').getAttribute('aria-disabled');
        await good();
        out.afterAnswer = actions('review');
        out.reason = t('reading.reviewIsAlreadyBeingSent');
        out.unknown = t('reading.reviewOutcomeUnknownGradeAgain');
        return out;
        """,
        before=READER,
        api={"study_decks": [{"id": 12, "name": "Deck A"}]},
    )
    out = result.value
    assert out["blocked"] == {"disabled": "true", "title": out["reason"]}
    assert out["whileWaiting"] == 1
    # The unknown outcome says that grading again is safe (it is resent with the same id).
    assert out["unknown"] in out["toasts"]
    assert out["reopened"] == "false" and out["afterAnswer"] == 2


def test_reader_add_with_unknown_outcome_is_not_rolled_back(page) -> None:
    result = page.run(
        READER_RUN
        + r"""
        window.__answers.push({ok: false, outcome: 'unknown', provider: 'jiten', message: __unknownAdd});
        await open('猫');
        await __input('#pudgeStudyDeck', '12');
        const reads = stateReads();
        await __click('#pudgeStudyCard [data-pudge-study-add]'); await __tick(4000);
        out.toasts = __toasts();
        out.refreshed = stateReads() - reads;
        return out;
        """,
        before=READER + f"window.__unknownAdd = {json.dumps(en('error.ln.jitenAddOutcomeUnknown'))};",
        api={"study_decks": [{"id": 12, "name": "Deck A"}]},
        lang="ru",
    )
    out = result.value
    # Shown in the app language, not as a failed add (no "could not add", no rollback re-read).
    assert out["toasts"] == [ru("error.ln.jitenAddOutcomeUnknown")]
    assert out["refreshed"] == 0
