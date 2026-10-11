from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.review_gate import ReviewGateStore
from pudge.review_providers import JitenReviewProvider, ReviewOutcomeUnknown, ReviewProviderError
from pudge.web_app import WebAppApi
from css_support import css_rules


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

    def strict_review_submit(self, word_id: int, reading_index: int, grade: str, *, attempt_id: str):
        self.submit_calls.append((word_id, reading_index, grade, attempt_id))
        if int(word_id) in self.unknown_words:
            raise ReviewOutcomeUnknown("jiten", attempt_id, "lost response")
        return {"ok": True, "outcome": "confirmed", "attempt_id": attempt_id}


def _api(
    tmp_path: Path, *, count: int = 2, service: FakeStudyService | None = None
) -> tuple[WebAppApi, Path, FakeStudyService]:
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


def test_escape_closes_the_word_card_first_then_the_modal_then_fullscreen(page) -> None:
    result = page.run(
        """
        await __tick(100);
        void showLnAudiobookNyaa({id: 5});                       // a modal is open...
        await __tick(20);
        const box = document.createElement('div');               // ...and a Jiten word card above it
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['猫'], tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 1}]],
          vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', states: ['young']}]}, {backend: 'jiten'});
        document.body.appendChild(box);
        await __click('[data-pudge-study-token]'); await __tick(20);
        const surfaces = () => ({card: PudgeReadingTools.study.isOpen(), modal: __visible('#modalBackdrop'),
                                 fullscreen: __apiCalls.filter(c => c.name === 'exit_fullscreen').length});
        const out = {start: surfaces()};
        const first = await __key('Escape'); await __tick(20);
        out.first = {...surfaces(), prevented: first.defaultPrevented};
        await __key('Escape'); await __tick(20);
        out.second = surfaces();
        await __key('Escape'); await __tick(20);
        out.third = surfaces();
        return out;
        """,
        state={"settings": {"escape_exits_fullscreen": True}},
    )
    out = result.value
    assert out["start"] == {"card": True, "modal": True, "fullscreen": 0}
    # One Escape closes one surface, the word card first.
    assert out["first"] == {"card": False, "modal": True, "fullscreen": 0, "prevented": True}
    assert out["second"] == {"card": False, "modal": False, "fullscreen": 0}
    assert out["third"] == {"card": False, "modal": False, "fullscreen": 1}


def test_main_anime_covers_keep_placeholder_until_real_image_loads(page) -> None:
    show = {
        "media_id": 7,
        "title": "Show",
        "cover": "https://img/cover.jpg",
        "next_episode": 3,
        "site_url": "https://anilist.co/anime/7",
    }
    result = page.run(
        """
        await __tick(100);
        const img = document.querySelector('.cover-shell img[data-pudge-cover-image]');
        // The page's inline onload/onerror handlers, run as WebKit runs them.
        const fire = type => new Function('event', img.getAttribute('on' + type)).call(img, new Event(type));
        const look = () => ({placeholder: __visible('.cover-shell [data-pudge-cover-placeholder]'), ready: img.dataset.pudgeCoverReady ?? null});
        const out = {loading: look()};
        fire('load');                                            // "loaded" but nothing decoded
        out.empty = look();
        img.__naturalWidth = 230; img.__naturalHeight = 345;
        fire('load');
        out.loaded = look();
        fire('error');
        out.failed = look();
        return out;
        """,
        state={"home": {"waiting": [show]}},
    )
    out = result.value
    assert out["loading"] == {"placeholder": True, "ready": None}
    assert out["empty"] == {"placeholder": True, "ready": None}
    assert out["loaded"] == {"placeholder": False, "ready": "1"}
    assert out["failed"] == {"placeholder": True, "ready": None}
    # Until it is ready the image is transparent over the placeholder (no broken-image frame).
    rules = css_rules("index.html")
    assert rules.get(".cover-shell img[data-pudge-cover-image]", "opacity") == "0"
    assert rules.get('.cover-shell img[data-pudge-cover-image][data-pudge-cover-ready="1"]', "opacity") == "1"


# Manga covers decode by file name: ok-* decodes, broken-* fails, zero-* decodes to nothing,
# slow-* waits for window.__decoded(name), hang-* never settles the first time.
MANGA_COVERS = r"""
const MakeImage = window.Image;
const attempts = new Map();
window.__waiting = new Map();
window.__decoded = name => { window.__waiting.get(name)?.(); };
window.Image = function Image() {
  const img = MakeImage();
  img.decode = () => {
    const src = String(img.src), name = src.split('/').pop();
    attempts.set(name, (attempts.get(name) || 0) + 1);
    const size = () => { img.__naturalWidth = 200; img.__naturalHeight = 300; };
    if (src.includes('ok-') || (src.includes('hang-') && attempts.get(name) > 1)) { size(); return Promise.resolve(); }
    if (src.includes('broken-')) return Promise.reject(new Error('decode failed'));
    if (src.includes('zero-')) return Promise.resolve();
    if (src.includes('hang-')) return new Promise(() => {});
    return new Promise(resolve => window.__waiting.set(name, () => { size(); resolve(); }));
  };
  return img;
};
window.__books = ['ok', 'broken', 'slow', 'zero', 'hang'].map((kind, i) => ({id: i + 1, title: `Vol ${i + 1}`, series_title: `Series ${i + 1}`,
  volume: 1, page_count: 10, read_pages: 0, cover_url: `covers/${kind}-${i + 1}.jpg`}));
__api.manga_state = async () => ({books: window.__books, ocr_available: false});
"""


def test_manga_cover_placeholder_waits_for_successful_decode(page) -> None:
    result = page.run(
        """
        const cover = id => ({image: __visible(`img[data-cover-book="${id}"]`), src: document.querySelector(`img[data-cover-book="${id}"]`)?.getAttribute('src') ?? null,
          placeholder: __visible(`[data-cover-placeholder="${id}"]`)});
        const redraw = async (id, changes) => {
          window.__books = window.__books.map(b => b.id === id ? {...b, ...changes} : b);
          await __settle(PudgeMangaReaderV2.renderLibrary(true)); await __tick(50);
        };
        setPage('manga');
        await __tick(100);
        const out = {first: [1, 2, 3, 4, 5].map(cover)};
        // Volume 3 gets a new cover while its old one is still decoding: the late old one is not shown.
        await redraw(3, {title: 'Vol 3 (new)', cover_url: 'covers/ok-new-3.jpg'});
        out.redrawn = cover(3);
        window.__decoded('slow-3.jpg'); await __tick(50);
        out.lateOld = cover(3);
        // A load that hangs is given up after a while, so the next redraw loads the cover again.
        await __tick(9000);
        out.hung = cover(5);
        await redraw(5, {title: 'Vol 5 (new)'});
        out.retried = cover(5);
        return out;
        """,
        before=MANGA_COVERS,
    )
    out = result.value
    hidden = {"image": False, "src": None, "placeholder": True}
    assert out["first"] == [
        {"image": True, "src": "covers/ok-1.jpg", "placeholder": False},
        hidden,
        hidden,
        hidden,
        hidden,
    ]
    assert out["redrawn"] == {"image": True, "src": "covers/ok-new-3.jpg", "placeholder": False}
    assert out["lateOld"] == out["redrawn"]
    assert out["hung"] == hidden
    assert out["retried"] == {"image": True, "src": "covers/hang-5.jpg", "placeholder": False}
