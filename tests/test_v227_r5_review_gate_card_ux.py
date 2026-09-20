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


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


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


def test_review_gate_front_back_contract_and_jiten_details() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    css = (WEB / "review_gate.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")

    # Front is recall-only except for Jiten confusable readings: no answer/translation/furigana.
    assert "card.wordTextPlain || card.wordText" in source
    assert "const frontConfusable = confusableReadings(card);" in source
    assert "pudge-review-gate-front-confusable" in source
    assert "Confusable readings" in source
    assert "Похожие чтения" in source
    assert ".pudge-review-gate-front-confusable" in css
    assert "data-review-gate-show-answer" in source
    assert "Показать ответ" in source
    assert "Show answer" in source
    assert "pudge-review-gate-back\"${revealed ? '' : ' hidden'}" in source
    assert ".pudge-review-gate-back[hidden]{display:none}" in css
    front_block = source.split("if (card) {", 1)[1].split("} else {", 1)[0]
    assert "frontConfusable.join" in front_block
    assert "definitions" not in front_block
    assert "rubyHtml(" not in front_block

    # Back can use Jiten study-batch details without extra provider requests.
    for field in (
        "rubyText",
        "definitions",
        "partsOfSpeech",
        "pitchAccents",
        "frequencyRank",
        "exampleSentence",
        "sourceDeckName",
        "deckOccurrences",
        "confusableReadings",
        "intervalPreview",
    ):
        assert field in source
    assert "<ruby>" in source and "<rt>" in source

    # Space reveals the answer; Escape only closes/cancels the gate.
    assert "if(window.PudgeReviewGate?.handleEscape?.())return;" in html
    assert "if(window.PudgeReviewGate?.handleKeydown?.(event))return;" in html
    assert "<kbd>Space</kbd>" in source
    assert "event?.code !== 'Space'" in source
    assert "function handleEscape()" in source
    escape_body = source.split("function handleEscape()", 1)[1].split("function handleKeydown", 1)[0]
    assert "showAnswer()" not in escape_body
    assert "close();" in escape_body

    # Prefetch starts before any series click and refreshes once per minute.
    assert "PREFETCH_INTERVAL_MS = 60_000" in source
    assert "review_gate_prefetch" in source
    assert "pywebviewready" in source
    assert "startPrefetchLoop" in source

    # Pitch is rendered graphically, not as a numeric 'Pitch: X' label.
    assert "study?.inlinePitch" in source
    assert "pudge-review-gate-pitch" in source
    assert "Pitch'}:" not in source


def test_review_gate_advances_to_prefetched_next_card_before_network_result() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    shift = source.index("cardQueue.shift();")
    submit = source.index("await window.pywebview.api.review_gate_review(", shift)
    render_next = source.index("render({loading:false}, {replaceCards:false});", shift)
    assert shift < render_next < submit
    assert "button.disabled = saving || !cardAuthorized" in source
    assert "authorizedCardKeys = new Set" in source
    assert "overlay?.classList.toggle('pudge-review-gate-busy', cardQueue.length === 0)" in source
    assert "Saving previous" in source


def test_warm_card_authorization_clears_busy_before_repaint() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    load = source.split("  async function loadBatch() {", 1)[1].split("\n  async function submitGrade", 1)[0]
    auth = load.index("authorizedCardKeys = new Set")
    clear_busy = load.index("busy = false;", auth)
    repaint = load.index("render(status || {}, {replaceCards:true});", clear_busy)
    assert auth < clear_busy < repaint
    assert "overlay?.classList.toggle('pudge-review-gate-busy', cardQueue.length === 0)" in load
    assert "button.disabled = saving || !cardAuthorized" in source

def test_review_grades_are_unavailable_until_answer_is_revealed_and_authorized() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    assert "if (grade) void submitGrade" in source
    assert "!revealed ? 'answer_hidden'" in source
    assert "!authorizedCardKeys.has(submittedKey)" in source
    assert "reason:'authorizing'" not in source  # reason is computed dynamically, not hard-coded branch UI
    assert "Authorizing card" in source
    assert "grade_blocked" in source


def test_numeric_jiten_bracket_ruby_never_leaks_literal_brackets() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    ruby_body = source.split("  function rubyHtml(value) {", 1)[1].split("\n  function meaningRows", 1)[0]
    ruby_function = "function rubyHtml(value) {" + ruby_body
    script = f"""
const esc = value => String(value ?? '');
{ruby_function}
process.stdout.write(rubyHtml('2[に]時[じ]'));
"""
    rendered = subprocess.check_output(["node", "-e", script], text=True)
    assert "2[に]" not in rendered
    assert "<ruby>2<rt>に</rt></ruby>" in rendered
    assert "<ruby>時<rt>じ</rt></ruby>" in rendered


def test_review_gate_ui_diagnostics_are_logged(tmp_path: Path) -> None:
    api, _video, _service = _api(tmp_path)
    lines: list[str] = []
    api.logger = SimpleNamespace(info=lambda fmt, *args: lines.append(fmt % args))
    result = api.review_gate_ui_event({
        "event": "grade_blocked",
        "card": "10:0",
        "reason": "authorizing",
        "enabled": False,
        "duration_ms": 17,
    })
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
    api._play_registry = {
        key: {"pid": launcher.pid, "started_at": started_at, "ack_file": str(ack_file)}
    }
    api.logger = _PlaybackLogger()
    return api, key, launcher


def test_review_count_is_optimistic_before_jiten_round_trip() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    submit = source.split("  async function submitGrade(grade) {", 1)[1].split("\n  async function open(", 1)[0]
    optimistic = submit.index("optimisticReviewed += 1;")
    repaint = submit.index("render({loading:false}, {replaceCards:false});", optimistic)
    network = submit.index("await window.pywebview.api.review_gate_review(", repaint)
    assert optimistic < repaint < network
    assert "review_optimistic" in submit
    assert "review_optimistic_confirm" in submit
    assert "review_optimistic_rollback" in submit
    assert "displayedCompleted(currentStatus" in submit


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
    fake.wait = lambda: 0  # type: ignore[attr-defined]
    monkeypatch.setattr(player.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(player, "_focus_mpv_process", lambda _pid: None)
    monkeypatch.setenv("PUDGE_MPV_START_ACK", str(ack))

    code = player.run_mpv(["mpv", "--", "video.mkv"], focus=True)

    assert code == 0
    payload = json.loads(ack.read_text(encoding="utf-8"))
    assert payload["pid"] == 7777
    assert float(payload["started_at"]) > 0


def test_play_monitor_surfaces_backend_startup_error() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    monitor = html.split("function monitorPlay(path,queueContext=null){", 1)[1].split("async function startPlay", 1)[0]
    assert "status.error||t('error.mpv'" in monitor


def test_player_focus_helper_has_timeout() -> None:
    source = (ROOT / "pudge" / "player.py").read_text(encoding="utf-8")
    focus = source.split("def _focus_mpv_process", 1)[1].split("def run_mpv", 1)[0]
    assert "timeout=1.5" in focus
    assert "subprocess.TimeoutExpired" in focus
