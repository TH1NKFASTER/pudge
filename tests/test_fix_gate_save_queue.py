"""The review gate before an episode saves grades in the background (invented words).

Grades go to Jiten one after another while the next card can already be graded; a grade
Pudge's Jiten budget held back is sent again (it was dropped, and ⌘Z then undid the review
before it); an undone grade that never left is just dropped. Jiten's bracket readings are
not wrapped in a second ruby, and the sidebar card stays while the gate grades others."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge import jiten_client
from pudge.light_novels import JitenUnavailableError
from pudge.review_episode_due import _bracket_ruby, _ruby_kana, build_episode_due_review_cards
from pudge.review_providers import JitenReviewProvider, ReviewNotSent, ReviewProviderError
from pudge.web_app import WebAppApi
from study_page_support import CARD, GATE_VIEW
from test_iphone_media import _api, _episode, service  # noqa: F401 - service is a fixture
from test_review_gate_episode_due import _FakeDb, _FakeLightNovels

# Three cards; every review answer waits for the test: answer() confirms the oldest request,
# answer({...}) answers it with that payload. SENT lists [wordId, grade, attemptId] per request.
QUEUE_GATE = (
    CARD
    + r"""
window.EPISODE = '/lib/Show - 03.mkv';
window.GATE_CARDS = [reviewCard(1, '猫', 'ねこ'), reviewCard(2, '犬', 'いぬ'), reviewCard(3, '鳥', 'とり')];
window.REQUIRED = 3;
window.GATE_DONE = 0; window.SENT = []; window.ANSWERS = [];
const gateStatus = () => ({supported: true, blocking: window.GATE_DONE < window.REQUIRED, granted: window.GATE_DONE >= window.REQUIRED,
  required: window.REQUIRED, completed: window.GATE_DONE, remaining: window.REQUIRED - window.GATE_DONE, episode: 3});
__api.review_gate_status = async () => gateStatus();
__api.review_gate_begin = async () => ({...gateStatus(), cards: window.GATE_CARDS.slice(window.GATE_DONE)});
__api.review_gate_review = (path, wordId, readingIndex, grade, attempt) => new Promise(resolve => {
  window.SENT.push([wordId, grade, attempt]);
  window.ANSWERS.push(payload => {
    if (payload) { resolve({...gateStatus(), ...payload}); return; }
    window.GATE_DONE += 1;
    resolve({ok: true, outcome: 'confirmed', ...gateStatus()});
  });
});
__api.review_gate_undo = async () => { window.GATE_DONE -= 1; return {ok: true, outcome: 'undone', ...gateStatus()}; };
__api.play = async () => ({ok: true, status: 'starting'});
"""
)

QUEUE_VIEW = (
    GATE_VIEW
    + r"""
const answer = async payload => { window.ANSWERS.shift()(payload); await __tick(10); };
const gradeGood = async () => { await space(); await gradeKey(3); await space(); await __tick(10); };
const enabled = () => [...gate().querySelectorAll('[data-review-gate-grade]')].every(b => !b.disabled);
const status = () => __text('.pudge-review-gate-status');
const of = (completed, required) => PudgeI18n.t('gate.reviewedOf', {completed, required});
"""
)


def test_the_next_card_is_graded_while_earlier_grades_are_saved_one_by_one(page) -> None:
    result = page.run(
        QUEUE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20);
        await gradeGood();
        const out = {first: {word: frontWord().innerText, sent: SENT.length, enabled: enabled(), status: status()}};
        await gradeGood();                                   // card 2 while card 1 is still on its way
        out.second = {word: frontWord().innerText, sent: SENT.map(r => r[0]), status: status(), enabled: enabled()};
        await answer();
        out.afterFirstSave = SENT.map(r => r[0]);            // card 2 goes only now
        await answer();
        await gradeGood(); await answer(); await __tick(20);
        out.open = PudgeReviewGate.isOpen();
        out.of = [of(1, 3), of(2, 3)];
        return out;
        """,
        before=QUEUE_GATE,
    )
    out = result.value
    assert out["first"] == {"word": "犬", "sent": 1, "enabled": True, "status": out["of"][0]}
    assert out["second"] == {"word": "鳥", "sent": [1], "status": out["of"][1], "enabled": True}
    assert out["afterFirstSave"] == [1, 2]
    assert out["open"] is False and result.calls_of("play")  # the last save opened the episode


def test_once_the_quota_is_graded_the_gate_waits_for_the_saves(page) -> None:
    result = page.run(
        QUEUE_VIEW
        + r"""
        window.REQUIRED = 2;                                 // three cards issued, two required
        await startPlay(EPISODE); await __tick(20);
        await gradeGood(); await gradeGood();
        const out = {full: {word: frontWord().innerText, enabled: enabled(), status: status()},
                     texts: [of(2, 2) + PudgeI18n.t('gate.savingPrevious')]};
        await answer(); await answer(); await __tick(20);
        out.open = PudgeReviewGate.isOpen();
        return out;
        """,
        before=QUEUE_GATE,
    )
    out = result.value
    assert out["full"] == {"word": "鳥", "enabled": False, "status": out["texts"][0]}
    assert out["open"] is False and result.calls_of("play")
    assert [call[1] for call in result.calls_of("review_gate_review")] == [1, 2]


def test_a_grade_held_back_by_the_budget_is_sent_again_and_undo_goes_to_it(page) -> None:
    result = page.run(
        QUEUE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20);
        await gradeGood();
        await answer({ok: false, outcome: 'retry', retry_after: 2, message: 'busy'});
        const out = {held: {status: status(), word: frontWord().innerText, sent: SENT.length},
                     later: of(1, 3) + PudgeI18n.t('gate.savingLater')};
        await __tick(2100);
        out.resent = {sent: SENT.length, same: SENT[0][2] === SENT[1][2] && SENT[1][0] === 1};
        await answer();
        out.saved = status();
        await gradeGood();
        await answer({ok: false, outcome: 'retry', retry_after: 5, message: 'busy'});
        await __key('z', {code: 'KeyZ', metaKey: true}, 'body'); await __tick(20);
        out.undone = {word: frontWord().innerText, status: status()};
        await __tick(6000);
        out.after = {sent: SENT.length, word: frontWord().innerText};
        out.of = of(1, 3);
        return out;
        """,
        before=QUEUE_GATE,
    )
    out = result.value
    assert out["held"] == {"status": out["later"], "word": "犬", "sent": 1}  # still counted, not dropped
    assert out["resent"] == {"sent": 2, "same": True}
    assert out["saved"] == out["of"]
    # ⌘Z goes to the grade that was held back (犬), not to the saved one before it.
    assert out["undone"] == {"word": "犬", "status": out["of"]}
    assert out["after"] == {"sent": 3, "word": "犬"}  # never sent again
    assert not result.called("review_gate_undo")  # nothing reached Jiten, nothing to undo there


def test_undo_of_a_grade_still_in_the_queue_drops_it_unsent(page) -> None:
    result = page.run(
        QUEUE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20);
        await gradeGood(); await gradeGood();                // 犬 waits behind 猫
        await __key('z', {code: 'KeyZ', metaKey: true}, 'body'); await __tick(20);
        const out = {word: frontWord().innerText, enabled: enabled()};
        await answer(); await __tick(20);
        out.sent = SENT.map(r => r[0]);
        out.status = status(); out.of = of(1, 3);
        return out;
        """,
        before=QUEUE_GATE,
    )
    out = result.value
    assert out["word"] == "犬" and out["enabled"] is True
    assert out["sent"] == [1] and out["status"] == out["of"]
    assert not result.called("review_gate_undo")


# --- the server side of a held-back grade ------------------------------------------------


def test_a_refused_request_is_a_not_sent_provider_error(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise jiten_client.JitenRefused("busy", reason="rate_limited", retry_after=17.0)

    monkeypatch.setattr(jiten_client, "request", refuse)
    provider = JitenReviewProvider("synthetic-key")
    with pytest.raises(ReviewNotSent) as submit:
        provider.submit_review(1, 0, "good", attempt_id="a1")
    with pytest.raises(ReviewNotSent) as lookup:
        provider.lookup_state(1, 0)
    assert submit.value.retry_after == 17.0 and lookup.value.retry_after == 17.0
    assert isinstance(submit.value, ReviewProviderError)  # older callers still catch it


def test_strict_submit_turns_a_held_back_grade_into_jiten_unavailable(monkeypatch) -> None:
    from pudge import light_novels

    class Provider:
        def __init__(self, _key) -> None:
            pass

        def validate_strict_review_candidate(self, *_args):
            raise ReviewNotSent("busy", retry_after=9.0)

    monkeypatch.setattr(light_novels, "JitenReviewProvider", Provider)
    service = light_novels.LightNovelService.__new__(light_novels.LightNovelService)
    service.settings = lambda: SimpleNamespace(study_backend="jiten", jiten_api_key="synthetic-key")
    with pytest.raises(JitenUnavailableError) as raised:
        service.strict_review_submit(1, 0, "good", attempt_id="a1")
    assert raised.value.retry_after == 9.0


def _due_api(tmp_path: Path, monkeypatch, light_novels) -> tuple[WebAppApi, Path]:
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True, review_gate_count=1, review_gate_all_due_episode_words=True
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=_FakeDb(video, subtitle))
    api.light_novels = light_novels
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_app_module,
        "build_episode_due_review_cards",
        lambda *args, **kwargs: {
            "cards": [
                {"wordId": 1, "readingIndex": 0, "pudgeCardKey": "1:0"},
                {"wordId": 2, "readingIndex": 0, "pudgeCardKey": "2:0"},
            ],
            "episode_pairs": 2,
            "due_pairs": 2,
        },
    )
    return api, video


def test_a_held_back_grade_keeps_the_card_on_offer(tmp_path: Path, monkeypatch) -> None:
    class Service(_FakeLightNovels):
        refusals = 1

        def strict_review_submit(self, word_id, reading_index, grade, *, attempt_id):
            if self.refusals:
                self.refusals -= 1
                raise JitenUnavailableError("busy", reason="rate_limited", retry_after=17.0)
            return super().strict_review_submit(word_id, reading_index, grade, attempt_id=attempt_id)

    api, video = _due_api(tmp_path, monkeypatch, Service())
    api.review_gate_begin(str(video))
    held = api.review_gate_review(str(video), 1, 0, "good", "a1")
    assert (held["ok"], held["outcome"], held["retry_after"], held["completed"]) == (False, "retry", 17.0, 0)
    again = api.review_gate_review(str(video), 1, 0, "good", "a1")  # the same card, not "stale"
    assert again["outcome"] == "confirmed" and again["completed"] == 1


def test_the_reading_gate_answers_retry_for_a_held_back_grade(tmp_path: Path, monkeypatch) -> None:
    from test_v0730_content_power_lifecycle import _gate

    gate, _revision, _removed = _gate(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise JitenUnavailableError("busy", reason="rate_limited", retry_after=4.0)

    status = gate.begin("ln", 1, 0)
    gate.api.light_novels.strict_review_submit = refuse
    held = gate.review(status["token"], 3, 0, "good", "attempt-a")
    assert (held["ok"], held["outcome"], held["retry_after"], held["completed"]) == (False, "retry", 4.0, 0)
    gate.api.light_novels.strict_review_submit = lambda *a, **k: {"ok": True, "outcome": "saved"}
    assert gate.review(status["token"], 3, 0, "good", "attempt-a")["completed"] == 1


# --- Jiten's bracket reading -------------------------------------------------------------


def test_a_bracket_reading_from_jiten_is_not_wrapped_twice(tmp_path: Path) -> None:
    assert _bracket_ruby("促す", "促[うなが]す") == "促[うなが]す"  # was 促[促[うなが]]す
    assert _ruby_kana("促[うなが]す") == "うながす" and _ruby_kana("取[と]り 扱[あつか]う") == "とりあつかう"
    assert _bracket_ruby("選ぶ", "えらぶ") == "選[えら]ぶ" and _ruby_kana("えらぶ") == "えらぶ"

    class Jiten:
        def jiten_preparse(self, text, digest=None):
            return {
                "tokens": [[{"wordId": 10, "readingIndex": 0}]],
                "vocabulary": [
                    {"wordId": 10, "readingIndex": 0, "spelling": "促す", "reading": "促[うなが]す"}
                ],
            }

        def jiten_live_states(self, pairs, force=False):
            return [{"wordId": 10, "readingIndex": 0, "normalizedState": "due", "states": ["due"]}]

    subtitle = tmp_path / "episode.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:03,000\n促す。\n", encoding="utf-8")
    card = build_episode_due_review_cards(Jiten(), subtitle, tmp_path / "cache")["cards"][0]
    assert card["reading"] == "うながす"
    assert card["readings"][0]["text"] == "うながす" and card["readings"][0]["rubyText"] == "促[うなが]す"


def test_the_gate_answer_shows_one_ruby_for_a_bracket_reading(page) -> None:
    result = page.run(
        QUEUE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20);
        await space();
        const word = gate().querySelector('.pudge-review-gate-answer-word');
        return {ruby: [...word.querySelectorAll('ruby')].map(r => [r.firstChild.textContent, r.querySelector('rt').textContent]),
                text: word.innerText};
        """,
        before=QUEUE_GATE
        + "window.GATE_CARDS[0] = reviewCard(1, '促す', 'うながす', "
        + "{readings: [{readingIndex: 0, text: 'うながす', rubyText: '促[うなが]す'}]});",
    )
    assert result.value["ruby"] == [["促", "うなが"]] and "[" not in result.value["text"]


def test_the_phone_gate_sends_a_held_back_grade_again(service, tmp_path, monkeypatch):
    from pudge import iphone_media

    episode = _episode(service, tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nうん\n")
    status = {
        "enabled": True,
        "blocking": True,
        "granted": False,
        "required": 2,
        "completed": 0,
        "remaining": 2,
    }
    answers = [
        {**status, "ok": False, "outcome": "retry", "retry_after": 3.0},
        {**status, "outcome": "confirmed", "completed": 1},
    ]
    calls, waits = [], []
    service.api = _api(review_gate_review=lambda *args: calls.append(args) or answers.pop(0))
    monkeypatch.setattr(iphone_media.time, "sleep", waits.append)
    view = service.media.gate_review(
        episode, {"wordId": 9, "readingIndex": 0, "grade": "good", "attemptId": "c" * 32}
    )
    assert view["outcome"] == "confirmed" and view["completed"] == 1
    assert waits == [3.0] and len(calls) == 2 and calls[0] == calls[1]  # the same attempt again
