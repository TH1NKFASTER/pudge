from __future__ import annotations

from pathlib import Path
import json
import time
from types import SimpleNamespace
import subprocess
import threading

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.review_gate import ReviewGateStore
from pudge.web_app import WebAppApi
from pudge import player
from css_support import css_rules
from tests.i18n_support import en, ru


class _OneCardService:
    def __init__(self) -> None:
        self.required_values: list[int] = []

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, _backend: str):
        return {
            "configured": True,
            "strict_gate_supported": True,
            "strict_gate_reason": "safe",
            "account_key": "jiten:test-account",
        }

    def strict_review_candidates(
        self,
        *,
        required: int,
        exclude_keys: set[str] | None = None,
        trusted_previous_keys: set[str] | None = None,
    ):
        self.required_values.append(required)
        return {
            "cards": [
                {
                    "wordId": 10,
                    "readingIndex": 0,
                    "wordText": "生活",
                    "wordTextPlain": "生活",
                    "readings": [{"readingIndex": 0, "text": "せいかつ", "rubyText": "生[せい]活[かつ]"}],
                    "definitions": [{"meanings": ["life", "living"]}],
                    "partsOfSpeech": ["noun"],
                    "pitchAccents": [0],
                    "frequencyRank": 123,
                    "intervalPreview": {"againSeconds": 60, "goodSeconds": 86400},
                    "sourceDeckName": "Example deck",
                    "exampleSentence": {"text": "生活を楽しむ。"},
                    "pudgeCardKey": "10:0",
                }
            ],
            "session_id": "s1",
        }


def _api(tmp_path: Path) -> tuple[WebAppApi, Path, _OneCardService]:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.ui.review_gate_enabled = True
    cfg.ui.review_gate_count = 8
    db = Database(cfg.library.database_path)
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_anime(LibraryAnime(media_id=777, title="Show", status="CURRENT", progress=0, episodes=12))
    db.upsert_episode(LibraryEpisode(media_id=777, title="Show", episode=1, video_path=video, state="ready"))

    service = _OneCardService()
    api = object.__new__(WebAppApi)
    api.config = cfg
    api.manager = SimpleNamespace(db=db)
    api.light_novels = service
    api._review_gate_lock = threading.RLock()
    api._review_gate_store = ReviewGateStore(db)
    api._review_gate_candidates = {}
    api._review_gate_pending = set()
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    return api, video, service


def test_review_gate_prefetch_warms_cards_before_open(tmp_path: Path) -> None:
    api, video, service = _api(tmp_path)
    scheduled = api.review_gate_prefetch()
    assert scheduled["started"] is True
    thread = api._review_gate_prefetch_thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert service.required_values == [8]

    result = api.review_gate_begin(str(video))
    assert service.required_values[0] == 8
    assert len(result["cards"]) == 1
    assert result["prefetched"] is True
    assert result["candidate_fetch_ms"] == 0.0
    assert result["begin_ms"] >= 0.0


# The review gate before an episode: review_gate_status blocks until window.__granted;
# review_gate_review answers are held in window.__reviews until the test resolves them.
GATE = r"""
const PATH = '/v/Show - 01.mkv';
const CARD = (wordId, word, extra = {}) => ({wordId, readingIndex: 0, wordText: word, wordTextPlain: word,
  readings: [{readingIndex: 0, text: 'よみ'}], definitions: [{meanings: ['meaning ' + wordId]}], pudgeCardKey: `${wordId}:0`, ...extra});
window.__granted = false; window.__reviews = [];
__api.review_gate_status = async () => window.__granted ? {blocking: false, granted: true}
  : {blocking: true, required: 2, completed: 0, remaining: 2, episode: 1};
__api.review_gate_review = (...args) => new Promise(resolve => window.__reviews.push({args, resolve}));
__api.play = async () => ({status: 'starting'});
const gate = '#pudgeReviewGate';
const view = () => ({word: __text(`${gate} .pudge-review-gate-word`), status: __text(`${gate} .pudge-review-gate-status`),
  loading: __text(`${gate} .pudge-review-gate-loading`), back: __visible(`${gate} .pudge-review-gate-back`),
  grades: [...document.querySelectorAll(`${gate} [data-review-gate-grade]`)].map(b => b.disabled),
  busy: document.querySelector(gate)?.classList.contains('pudge-review-gate-busy') ?? null});
const reveal = async () => { await __key(' ', {code: 'Space'}); await __tick(20); };
const grade = async id => { await __click(`${gate} [data-review-gate-grade="${id}"]`); await __tick(20); };
const events = name => __apiCalls.filter(c => c.name === 'review_gate_ui_event' && c.args[0].event === name).map(c => c.args[0]);
"""


def test_review_card_front_asks_for_recall_and_space_reveals_the_jiten_answer(page) -> None:
    assert (en("gate.confusableReadings"), ru("gate.confusableReadings")) == (
        "Confusable readings",
        "Похожие чтения",
    )
    assert (en("gate.showAnswer"), ru("gate.showAnswer")) == ("Show answer", "Показать ответ")
    assert en("gate.spaceKey") == "Space"
    result = page.run(
        GATE
        + """
        const full = CARD(10, '生活', {readings: [{readingIndex: 0, text: 'せいかつ', rubyText: '生[せい]活[かつ]'}],
          definitions: [{meanings: ['life', 'living']}], partsOfSpeech: ['noun'], pitchAccents: [0], frequencyRank: 123,
          intervalPreview: {againSeconds: 60, goodSeconds: 86400}, sourceDeckName: 'Example deck',
          exampleSentence: {text: '生活を楽しむ。'}, confusableReadings: ['しょうかつ']});
        __api.review_gate_begin = async () => ({blocking: true, granted: false, required: 2, completed: 0, remaining: 2, episode: 1,
          cards: [full, CARD(11, '犬')]});
        await __tick(100);
        void startPlay(PATH); await __tick(50);
        const out = {front: {...view(), text: __text(`${gate} .pudge-review-gate-front`),
                             ruby: document.querySelectorAll(`${gate} .pudge-review-gate-front ruby`).length}};
        await __key('a', {code: 'KeyA'}); await __tick(20);
        out.otherKey = view().back;
        await grade('good');                                     // the hidden button cannot grade either
        out.hiddenGrade = {sent: window.__reviews.length, blocked: events('grade_blocked').map(e => e.reason)};
        await reveal();
        const back = `${gate} .pudge-review-gate-back`;
        out.back = {...view(), text: __text(back),
          ruby: [...document.querySelectorAll(`${gate} .pudge-review-gate-word ruby`)].map(r => [r.firstChild.textContent, r.querySelector('rt').textContent]),
          wordShown: __visible(`${gate} .pudge-review-gate-word`), recallHidden: !__visible(`${gate} [data-review-gate-show-answer]`) && !__visible(`${gate} .pudge-review-gate-front-confusable`),
          pitch: document.querySelectorAll(`${gate} .pudge-review-gate-pitch .pudge-inline-pitch`).length,
          meanings: [...document.querySelectorAll(`${gate} .pudge-review-gate-meanings li`)].map(li => li.textContent),
          grades: [...document.querySelectorAll(`${gate} [data-review-gate-grade]`)].map(b => b.textContent)};
        await __key('Escape'); await __tick(20);
        out.closed = !PudgeReviewGate.isOpen();
        out.texts = {confusable: t('gate.confusableReadings'), show: `${t('gate.showAnswer')} ${t('gate.spaceKey')}`,
                     again: t('gate.intervalMinutes', {value: 1}), good: t('gate.intervalDays', {value: 1})};
        return out;
        """,
    )
    out = result.value
    texts = out["texts"]
    # Front: the word only (plus Jiten's confusable readings) and "Show answer Space".
    assert out["front"]["word"] == "生活" and out["front"]["back"] is False and out["front"]["ruby"] == 0
    assert out["front"]["text"].splitlines() == ["生活", texts["confusable"], "しょうかつ", texts["show"]]
    # Other keys and grades do nothing before the answer is shown.
    assert out["otherKey"] is False
    assert out["hiddenGrade"] == {"sent": 0, "blocked": ["answer_hidden"]}
    # Space shows the back with Jiten's details, from the card itself.
    back = out["back"]
    assert back["back"] is True
    assert back["wordShown"] is True and back["ruby"] == [
        ["生", "せい"],
        ["活", "かつ"],
    ]  # furigana over the word
    assert back["recallHidden"] is True
    assert back["pitch"] == 1 and "Pitch" not in back["text"]  # drawn, not "Pitch: 0"
    for detail in ("せいかつ", "#123", "noun", "生活を楽しむ。", "Example deck"):
        assert detail in back["text"], detail
    assert back["meanings"] == ["life", "living"]
    assert back["grades"][0].endswith(texts["again"]) and back["grades"][2].endswith(texts["good"])
    # Escape cancels the gate (it never reveals), and the episode does not start.
    assert out["closed"] is True and not result.called("play")
    # The back stays hidden in WebKit too, whatever display rules it gets.
    assert css_rules("review_gate.css").get(".pudge-review-gate-back[hidden]", "display") == "none"


def test_next_card_shows_at_once_while_the_previous_review_is_saved(page) -> None:
    assert en("gate.savingPrevious").startswith("Saving previous")
    result = page.run(
        GATE
        + """
        __api.review_gate_begin = async () => ({blocking: true, granted: false, required: 2, completed: 0, remaining: 2, episode: 1,
          cards: [CARD(10, '一'), CARD(11, '2時', {readings: [{readingIndex: 0, text: 'にじ', rubyText: '2[に]時[じ]'}], deckOccurrences: [{originalTitle: 'Show', occurrences: 3}]}), CARD(12, '三')]});
        await __tick(100);
        void startPlay(PATH); await __tick(50);
        const out = {first: view()};
        await reveal(); await grade('good');
        out.saving = view();                                     // card 2 at once, card 1 still on its way to Jiten
        out.sent = window.__reviews.map(r => r.args.slice(0, 4));
        out.attempt = window.__reviews[0].args[4];
        window.__reviews[0].resolve({ok: true, outcome: 'unknown', completed: 0, remaining: 2, required: 2, blocking: true});
        await __tick(20);
        out.unknown = {...view(), toasts: __toasts()};
        await reveal();
        out.ruby = [...document.querySelectorAll(`${gate} .pudge-review-gate-word ruby`)].map(r => [r.firstChild.textContent, r.querySelector('rt').textContent]);
        out.answer = __text(`${gate} .pudge-review-gate-word`);
        out.source = __text(`${gate} .pudge-review-gate-source`);
        await grade('good');
        window.__reviews[1].resolve({ok: true, outcome: 'confirmed', completed: 1, remaining: 1, required: 2, blocking: true});
        await __tick(20);
        out.confirmed = view();
        await reveal(); await grade('easy');
        window.__granted = true;
        window.__reviews[2].resolve({ok: true, outcome: 'confirmed', completed: 2, remaining: 0, required: 2, granted: true, blocking: false});
        await __tick(50);
        out.open = PudgeReviewGate.isOpen();
        out.texts = {saving: t('gate.savingPrevious'), unknown: t('gate.reviewOutcomeIsUnknownIt'),
                     of: [0, 1].map(n => t('gate.reviewedOf', {completed: n, required: 2}))};
        out.events = ['review_optimistic', 'review_optimistic_confirm', 'review_optimistic_rollback'].map(name => events(name).length);
        return out;
        """,
    )
    out = result.value
    texts = out["texts"]
    assert out["first"]["word"] == "一" and out["first"]["status"] == texts["of"][0]
    # The next card is drawn before Jiten answers; the count already includes the review.
    assert out["saving"]["word"] == "2時"
    assert out["saving"]["status"] == texts["of"][1] + texts["saving"]
    assert out["saving"]["grades"] == [True] * 4 and out["saving"]["busy"] is False
    assert out["sent"] == [["/v/Show - 01.mkv", 10, 0, "good"]] and out["attempt"]
    # A lost answer takes the optimistic count back and says so.
    assert out["unknown"]["status"] == texts["of"][0] and out["unknown"]["grades"] == [False] * 4
    assert out["unknown"]["toasts"] == [texts["unknown"]]
    # Jiten's bracket furigana over digits never shows the brackets.
    assert out["ruby"] == [["2", "に"], ["時", "じ"]] and "[" not in out["answer"]
    assert out["source"].endswith("Show ×3")  # where the word occurs, without a source deck
    assert out["confirmed"]["status"] == texts["of"][1]
    # The last review grants the episode: the gate closes and the episode starts.
    assert out["open"] is False
    assert result.calls_of("play") == [["/v/Show - 01.mkv", False, False, False]]
    assert out["events"] == [3, 2, 1]


def test_warm_cards_show_at_once_and_grade_after_authorization(page) -> None:
    assert en("gate.authorizingCard").startswith("Authorizing card")
    warm = [
        {"wordId": 1, "readingIndex": 0, "wordText": "一", "wordTextPlain": "一", "pudgeCardKey": "1:0"},
        {"wordId": 2, "readingIndex": 0, "wordText": "二", "wordTextPlain": "二", "pudgeCardKey": "2:0"},
    ]
    code = (
        GATE
        + """
        window.__begin = [];
        __api.review_gate_begin = () => new Promise(resolve => window.__begin.push(resolve));
        await __tick(100);
        void startPlay(PATH); await __tick(20);
        const out = {opened: view()};
        await reveal();
        out.revealed = view();
        window.__begin[0]({blocking: true, granted: false, required: 2, completed: 0, remaining: 2, episode: 1, cards: [CARD(1, '一'), CARD(2, '二')]});
        await __tick(20);
        out.authorized = view();
        out.texts = {authorizing: t('gate.authorizingCard'), loading: t('gate.loadingJitenReviewCard'), of: t('gate.reviewedOf', {completed: 0, required: 2})};
        return out;
        """
    )
    # Cards warmed by the background prefetch: painted at once, grades wait for review_gate_begin.
    with_warm = page.run(code, api={"review_gate_prefetch": {"cards": warm, "target": 2}}).value
    texts = with_warm["texts"]
    assert with_warm["opened"]["word"] == "一" and with_warm["opened"]["busy"] is False
    assert with_warm["opened"]["status"] == texts["of"] + texts["authorizing"]
    assert with_warm["revealed"]["back"] is True and with_warm["revealed"]["grades"] == [True] * 4
    assert (
        with_warm["authorized"]["status"] == texts["of"] and with_warm["authorized"]["grades"] == [False] * 4
    )
    assert with_warm["authorized"]["busy"] is False
    # Nothing warmed: a loading card (the backdrop stays clickable to cancel) until the cards arrive.
    cold = page.run(code).value
    assert cold["opened"]["word"] is None and cold["opened"]["busy"] is True
    assert cold["opened"]["loading"] == texts["loading"]
    assert cold["authorized"]["word"] == "一" and cold["authorized"]["busy"] is False


def test_review_gate_ui_diagnostics_are_logged(tmp_path: Path) -> None:
    api, _video, _service = _api(tmp_path)
    lines: list[str] = []
    api.logger = SimpleNamespace(info=lambda fmt, *args: lines.append(fmt % args))
    result = api.review_gate_ui_event(
        {
            "event": "grade_blocked",
            "card": "10:0",
            "reason": "authorizing",
            "enabled": False,
            "duration_ms": 17,
        }
    )
    assert result == {"ok": True}
    assert any("EVENT review_gate.ui event=grade_blocked" in line for line in lines)
    assert any("card=10:0" in line and "reason=authorizing" in line for line in lines)


# R5 v5.3 optimistic review count + playback-start diagnostics/hardening.
class _PlaybackLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, fmt: str, *args: object, **_kwargs: object) -> None:
        self.lines.append(fmt % args if args else fmt)

    def error(self, fmt: str, *args: object, **_kwargs: object) -> None:
        self.lines.append(fmt % args if args else fmt)


class _PlaybackLauncher:
    def __init__(self, pid: int = 4321, code: int | None = None) -> None:
        self.pid = pid
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        self.code = 0
        return self.code


def _play_api(tmp_path: Path, *, age: float, ack_file: Path) -> tuple[WebAppApi, str, _PlaybackLauncher]:
    api = object.__new__(WebAppApi)
    key = str((tmp_path / "Youjo Senki S2 - 10.mkv").resolve())
    launcher = _PlaybackLauncher()
    started_at = time.time() - age
    api._play_processes = {key: launcher}
    api._play_started_at = {key: started_at}
    api._play_exit_codes = {}
    api._play_last_logged_status = {}
    api._play_registry_path = tmp_path / "active-playbacks.json"
    api._play_registry = {key: {"pid": launcher.pid, "started_at": started_at, "ack_file": str(ack_file)}}
    api.logger = _PlaybackLogger()
    return api, key, launcher


def test_play_state_becomes_running_only_after_real_mpv_spawn_ack(tmp_path: Path) -> None:
    ack = tmp_path / "ack.json"
    ack.write_text(json.dumps({"pid": 9001, "started_at": time.time()}), encoding="utf-8")
    api, key, launcher = _play_api(tmp_path, age=0.2, ack_file=ack)
    api._pid_is_alive = lambda pid: pid in {launcher.pid, 9001}  # type: ignore[method-assign]

    state = api._play_state_locked(key)

    assert state["status"] == "running"
    assert state["pid"] == launcher.pid
    assert state["mpv_pid"] == 9001
    assert launcher.terminated is False


def test_play_starting_times_out_instead_of_sticking_forever(tmp_path: Path) -> None:
    ack = tmp_path / "missing.json"
    api, key, launcher = _play_api(
        tmp_path,
        age=WebAppApi.PLAY_STARTUP_TIMEOUT_SECONDS + 1.0,
        ack_file=ack,
    )
    api._pid_is_alive = lambda _pid: True  # type: ignore[method-assign]

    state = api._play_state_locked(key)

    assert state["status"] == "failed"
    assert state["reason"] == "startup_timeout"
    assert state["exit_code"] == -1
    assert launcher.terminated is True
    assert key not in api._play_processes
    assert key not in api._play_registry
    assert any("EVENT play.startup_timeout" in line for line in api.logger.lines)


def test_run_mpv_writes_spawn_ack_and_logs_lifecycle(tmp_path: Path, monkeypatch) -> None:
    ack = tmp_path / "spawn.json"
    fake = _PlaybackLauncher(pid=7777, code=0)
    waited = []
    fake.wait = lambda: waited.append(fake.pid) or 0  # type: ignore[attr-defined]
    # Isolate fake playback PIDs from macOS's ps and real host processes.
    monkeypatch.setattr("pudge.process_cleanup.process_snapshot", lambda: {})
    monkeypatch.setattr(player.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(player, "_focus_mpv_process", lambda _pid: None)
    monkeypatch.setenv("PUDGE_MPV_START_ACK", str(ack))

    code = player.run_mpv(["mpv", "--", "video.mkv"], focus=True)

    assert code == 0
    assert waited == [7777]
    payload = json.loads(ack.read_text(encoding="utf-8"))
    assert payload["pid"] == 7777
    assert float(payload["started_at"]) > 0


def test_play_monitor_surfaces_backend_startup_error(page) -> None:
    result = page.run(
        """
        window.__status = {status: 'failed', error: 'mpv could not open the display', exit_code: -1};
        __api.review_gate_status = async () => ({blocking: false, granted: true});
        __api.play = async () => ({status: 'starting'});
        __api.play_status = async () => window.__status;
        await __tick(100);
        await startPlay('/v/a.mkv'); await __tick(2000);
        const backend = __toasts();
        window.__status = {status: 'failed', exit_code: 2};
        await startPlay('/v/b.mkv'); await __tick(2000);
        return {backend, generic: __toasts(), mpv: t('error.mpv', {code: 2})};
        """,
    )
    out = result.value
    assert "mpv could not open the display" in out["backend"]
    assert out["mpv"] in out["generic"]


def test_player_focus_helper_gives_up_instead_of_hanging(monkeypatch) -> None:
    calls: list[dict] = []

    def hanging(*_args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("osascript", kwargs.get("timeout"))

    monkeypatch.setattr(player.sys, "platform", "darwin")
    monkeypatch.setattr(player.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(player.subprocess, "run", hanging)
    assert player._focus_mpv_process(4321) is None
    # One bounded attempt: System Events never blocks playback for long.
    assert len(calls) == 1 and 0 < calls[0]["timeout"] <= 2
