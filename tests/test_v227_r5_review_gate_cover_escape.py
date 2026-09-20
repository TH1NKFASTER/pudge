from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import json
import subprocess

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from pudge.review_providers import JitenReviewProvider, ReviewOutcomeUnknown, ReviewProviderError
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


class FakeStudyService:
    def __init__(self, *, backend: str = "jiten", account_key: str = "jiten:test-account") -> None:
        self.backend = backend
        self.account_key = account_key
        self.candidate_calls = 0
        self.submit_calls: list[tuple[int, int, str, str]] = []
        self.unknown_words: set[int] = set()
        self.cards = [
            {
                "wordId": 10,
                "readingIndex": 0,
                "wordText": "猫",
                "readings": [{"text": "ねこ"}],
                "definitions": [{"meanings": ["cat"]}],
                "pudgeCardKey": "10:0",
            },
            {
                "wordId": 20,
                "readingIndex": 1,
                "wordText": "犬",
                "readings": [{"text": "いぬ"}],
                "definitions": [{"meanings": ["dog"]}],
                "pudgeCardKey": "20:1",
            },
            {
                "wordId": 30,
                "readingIndex": 0,
                "wordText": "鳥",
                "readings": [{"text": "とり"}],
                "definitions": [{"meanings": ["bird"]}],
                "pudgeCardKey": "30:0",
            },
        ]

    def settings(self):
        return SimpleNamespace(study_backend=self.backend)

    def study_provider_capabilities(self, backend: str):
        if backend != "jiten":
            return {
                "configured": True,
                "strict_gate_supported": False,
                "strict_gate_reason": "unsupported provider",
                "account_key": "jpdb:test-account",
            }
        return {
            "configured": True,
            "strict_gate_supported": True,
            "strict_gate_reason": "safe",
            "account_key": self.account_key,
        }

    def strict_review_candidates(self, *, required: int, exclude_keys: set[str] | None = None):
        self.candidate_calls += 1
        excluded = set(exclude_keys or set())
        cards = [card for card in self.cards if card["pudgeCardKey"] not in excluded]
        return {"cards": cards[: max(1, required)], "session_id": "s1"}

    def strict_review_submit(
        self, word_id: int, reading_index: int, grade: str, *, attempt_id: str
    ):
        self.submit_calls.append((word_id, reading_index, grade, attempt_id))
        if int(word_id) in self.unknown_words:
            raise ReviewOutcomeUnknown("jiten", attempt_id, "lost response")
        return {"ok": True, "outcome": "confirmed", "attempt_id": attempt_id}


def _api(tmp_path: Path, *, count: int = 2, service: FakeStudyService | None = None) -> tuple[WebAppApi, Path, FakeStudyService]:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.ui.review_gate_enabled = True
    cfg.ui.review_gate_count = count
    db = Database(cfg.library.database_path)
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_anime(LibraryAnime(media_id=777, title="Show", status="CURRENT", progress=0, episodes=12))
    db.upsert_episode(LibraryEpisode(media_id=777, title="Show", episode=1, video_path=video, state="ready"))

    api = object.__new__(WebAppApi)
    api.config = cfg
    api.manager = SimpleNamespace(db=db)
    api.light_novels = service or FakeStudyService()
    api._review_gate_lock = threading.RLock()
    api._review_gate_store = ReviewGateStore(db)
    api._review_gate_candidates = {}
    api._review_gate_pending = set()
    api._play_lock = threading.Lock()
    api._play_state_locked = lambda _key: {"status": "idle"}
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    return api, video, api.light_novels


def test_review_gate_blocks_play_locally_before_launch(tmp_path: Path) -> None:
    api, video, service = _api(tmp_path)

    result = WebAppApi.play(api, str(video))

    assert result["review_gate_required"] is True
    assert result["review_gate"]["completed"] == 0
    assert result["review_gate"]["required"] == 2
    assert service.candidate_calls == 0  # play never does provider/network work


def test_review_gate_persists_unique_progress_and_permanent_episode_grant(tmp_path: Path) -> None:
    api, video, service = _api(tmp_path, count=2)

    batch = api.review_gate_begin(str(video))
    assert batch["blocking"] is True
    # The already-prefetched quota is issued at once so the frontend can move
    # to card N+1 immediately while card N is being committed to Jiten.
    assert [card["pudgeCardKey"] for card in batch["cards"]] == ["10:0", "20:1"]
    assert batch["issued"] == 2

    first = api.review_gate_review(str(video), 10, 0, "good", "attempt-1")
    assert first["completed"] == 1
    assert first["remaining"] == 1
    assert first["granted"] is False

    # Reopening the gate after closing the UI keeps durable progress and does not
    # offer the already-counted card again.
    reopened = api.review_gate_begin(str(video))
    assert reopened["completed"] == 1
    assert all(card["pudgeCardKey"] != "10:0" for card in reopened["cards"])

    second = api.review_gate_review(str(video), 20, 1, "easy", "attempt-2")
    assert second["completed"] == 2
    assert second["granted"] is True
    calls_before = service.candidate_calls

    # Existing grants are local/permanent and require no provider candidate fetch.
    granted = api.review_gate_begin(str(video))
    assert granted["granted"] is True
    assert granted["blocking"] is False
    assert service.candidate_calls == calls_before

    # Raising the configured quota later does not revoke the earned episode grant.
    api.config.ui.review_gate_count = 10
    assert api.review_gate_status(str(video))["granted"] is True


def test_review_gate_ambiguous_outcome_never_counts_or_retries_same_card(tmp_path: Path) -> None:
    service = FakeStudyService()
    service.unknown_words.add(10)
    api, video, _ = _api(tmp_path, count=1, service=service)
    api.review_gate_begin(str(video))

    result = api.review_gate_review(str(video), 10, 0, "good", "unknown-attempt")

    assert result["outcome"] == "unknown"
    assert result["completed"] == 0
    assert result["granted"] is False
    assert "10:0" in result["unknown_card_keys"]
    again = api.review_gate_review(str(video), 10, 0, "good", "must-not-send")
    assert again["outcome"] == "unknown"
    assert len(service.submit_calls) == 1


def test_review_gate_is_account_scoped(tmp_path: Path) -> None:
    api, video, service = _api(tmp_path, count=1)
    api.review_gate_begin(str(video))
    api.review_gate_review(str(video), 10, 0, "good", "attempt-1")
    assert api.review_gate_status(str(video))["granted"] is True

    service.account_key = "jiten:other-account"
    status = api.review_gate_status(str(video))
    assert status["granted"] is False
    assert status["completed"] == 0


def test_unsupported_provider_never_pretends_strict_gate_is_active(tmp_path: Path) -> None:
    service = FakeStudyService(backend="jpdb")
    api, video, _ = _api(tmp_path, count=3, service=service)
    status = api.review_gate_status(str(video))
    assert status["supported"] is False
    assert status["blocking"] is False
    assert status["granted"] is True
    assert "unsupported" in status["reason"]


def test_jiten_strict_candidates_require_real_previous_review(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.content = b"{}"

        def json(self):
            return self._payload

    def fake_get(url, *, headers, params=None, timeout=30):
        if url.endswith("/srs/study-batch"):
            return Response(
                {
                    "sessionId": "s1",
                    "cards": [
                        {"wordId": 1, "readingIndex": 0, "isNewCard": False},
                        {"wordId": 2, "readingIndex": 0, "isNewCard": False},
                        {"wordId": 3, "readingIndex": 0, "isNewCard": True},
                    ],
                }
            )
        word_id = int(url.rstrip("/").split("/")[-2])
        if word_id == 1:
            return Response({"card": {"state": 2}, "reviews": [{"rating": 3}]})
        return Response({"card": {"state": 0}, "reviews": []})

    monkeypatch.setattr("pudge.review_providers.httpx.get", fake_get)
    provider = JitenReviewProvider("token")
    batch = provider.list_strict_review_candidates(required=5)
    assert [card["wordId"] for card in batch["cards"]] == [1]
    assert batch["cards"][0]["pudgePreviouslyReviewed"] is True

    with pytest.raises(ReviewProviderError, match="previous review history"):
        provider.validate_strict_review_candidate(2, 0)


def test_review_gate_config_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.ui.review_gate_enabled = True
    cfg.ui.review_gate_count = 7
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.ui.review_gate_enabled is True
    assert loaded.ui.review_gate_count == 7



def test_jiten_card_escape_behavior_closes_card_before_modal_or_fullscreen() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    dispatcher = "function isEscapeKey" + html.split("function isEscapeKey", 1)[1].split(
        "// pudge-r1-escape-dispatch-end", 1
    )[0]
    script = f"""
const vm=require('vm');
const dispatcher={dispatcher!r};
const state={{study:true,modal:true,fullscreen:0,closes:[]}};
const windowTarget=new EventTarget();global.window=windowTarget;
global.ui={{page:'lightnovels',lnSelection:new Set(),lnStudyTriggerCapturing:false,shortcutCaptureTarget:null,onboardingForced:false,state:{{settings:{{escape_exits_fullscreen:true}}}}}};
global.$=id=>({{hidden:id==='globalSearchOverlay',classList:{{contains:name=>id==='modalBackdrop'&&name==='open'?state.modal:false,remove(){{}}}},click(){{state.closes.push(id+':click')}}}});
global.finishLnStudyTriggerCapture=()=>{{}};global.stopShortcutCapture=()=>{{}};global.closeLnFind=()=>{{}};
global.closeGlobalSearch=()=>{{}};global.closeOnboarding=()=>{{}};global.skipOnboarding=async()=>{{}};
global.closeModal=()=>{{state.modal=false;state.closes.push('modal')}};global.hideContextMenu=()=>{{}};
global.closeLnChapterPicker=()=>{{}};global.hideLnStudyStateMenu=()=>{{}};global.hideLnTranslation=()=>{{}};
global.clearLnSelection=()=>{{}};global.pywebview={{api:{{exit_fullscreen:async()=>{{state.fullscreen+=1}}}}}};
window.PudgeSelect={{closeIfOpen:()=>false}};window.PudgeConfirm={{closeIfOpen:()=>false}};window.PudgeCoverPreview={{closeIfOpen:()=>false}};
window.PudgeReadingTools={{study:{{closeIfOpen:()=>{{if(!state.study)return false;state.study=false;state.closes.push('study');return true;}}}},closeIfOpen:()=>false}};
window.PudgeReviewGate={{handleEscape:()=>false}};window.PudgeDebug={{close:()=>{{}}}};window.PudgeMangaReaderV2={{selectedBookIds:()=>[],closeEscapeSurface:()=>false}};window.PudgeAudiobookSelection={{selectedBookIds:()=>[]}};
vm.runInThisContext(dispatcher);
const event=new Event('keydown',{{cancelable:true}});Object.defineProperties(event,{{key:{{value:'Escape'}},code:{{value:'Escape'}},repeat:{{value:false}},isComposing:{{value:false}}}});
window.dispatchEvent(event);
setImmediate(()=>process.stdout.write(JSON.stringify({{study:state.study,modal:state.modal,fullscreen:state.fullscreen,closes:state.closes,prevented:event.defaultPrevented}})));
"""
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result == {
        "study": False,
        "modal": True,
        "fullscreen": 0,
        "closes": ["study"],
        "prevented": True,
    }


def test_main_anime_covers_keep_placeholder_until_real_image_loads() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "pudge-cover-load-placeholder" in html
    assert "data-pudge-cover-placeholder" in html
    assert "data-pudge-cover-image" in html
    assert "onload=\"window.pudgeCoverImageState(this,true)\"" in html
    assert "onerror=\"window.pudgeCoverImageState(this,false)\"" in html
    assert "img[data-pudge-cover-image] { position:relative; z-index:1; opacity:0; }" in html
    assert "img[data-pudge-cover-image][data-pudge-cover-ready=\"1\"] { opacity:1; }" in html
    assert "if(placeholder)placeholder.hidden=true" in html
    assert "if(placeholder)placeholder.hidden=false" in html


def test_jiten_card_escape_precedes_generic_modal_escape() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    reading = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    study_escape = "if(window.PudgeReadingTools?.study?.closeIfOpen?.())return;"
    modal_escape = "if($('modalBackdrop').classList.contains('open')){closeModal();return;}"
    assert study_escape in html
    assert html.index(study_escape) < html.index(modal_escape)
    assert "closeIfOpen()" in reading
    assert "document.getElementById('pudgeStudyCard')?.classList.contains('open')" in reading


def test_manga_cover_placeholder_waits_for_successful_decode() -> None:
    source = (WEB / "manga_reader_v2.js").read_text(encoding="utf-8")
    decode = source.index("const ready = await decodeCover(url);")
    reveal = source.index("img.hidden = false;", decode)
    hide_placeholder = source.index("placeholder.hidden = true;", reveal)
    assert decode < reveal < hide_placeholder
    assert "coverLoadInflight" in source
    assert "COVER_DECODE_TIMEOUT_MS = 8000" in source
    assert "Promise.race([load, timeout])" in source
    assert "if (!ready || expectedSignature !== libraryRenderSignature) return;" in source
