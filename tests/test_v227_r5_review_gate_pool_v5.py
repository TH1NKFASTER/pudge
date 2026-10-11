from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import subprocess

import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.review_gate import ReviewGateStore
from pudge.review_providers import JitenReviewProvider
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


class _PoolService:
    def __init__(self, available: int = 100) -> None:
        self.available = available
        self.calls: list[tuple[int, set[str]]] = []

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, _backend: str):
        return {
            "configured": True,
            "strict_gate_supported": True,
            "strict_gate_reason": "safe",
            "account_key": "jiten:pool-test",
        }

    def strict_review_candidates(
        self,
        *,
        required: int,
        exclude_keys: set[str] | None = None,
        trusted_previous_keys: set[str] | None = None,
    ):
        trusted = set(trusted_previous_keys or set())
        excluded = set(exclude_keys or set())
        self.calls.append((required, trusted))
        cards = []
        for word_id in range(1, self.available + 1):
            key = f"{word_id}:0"
            if key in excluded:
                continue
            cards.append(
                {
                    "wordId": word_id,
                    "readingIndex": 0,
                    "wordTextPlain": f"語{word_id}",
                    "pudgeCardKey": key,
                }
            )
            if len(cards) >= required:
                break
        return {
            "session_id": "pool-session",
            "cards": cards,
            "provider_available_estimate": self.available,
        }


def _api(tmp_path: Path, *, review_count: int, ready_anime: int, available: int = 100):
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.ui.review_gate_enabled = True
    cfg.ui.review_gate_count = review_count
    db = Database(cfg.library.database_path)
    first_video = None
    for index in range(ready_anime):
        media_id = 1000 + index
        db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=f"Show {index}",
                status="CURRENT",
                progress=0,
                episodes=12,
            )
        )
        video = tmp_path / f"Show {index} - 01.mkv"
        video.write_bytes(b"video")
        if first_video is None:
            first_video = video
        db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=f"Show {index}",
                episode=1,
                video_path=video,
                state="ready",
            )
        )

    service = _PoolService(available=available)
    api = object.__new__(WebAppApi)
    api.config = cfg
    api.manager = SimpleNamespace(db=db)
    api.light_novels = service
    api._review_gate_lock = threading.RLock()
    api._review_gate_store = ReviewGateStore(db)
    api._review_gate_candidates = {}
    api._review_gate_pending = set()
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    return api, first_video, service


def test_prefetch_target_is_reviews_per_episode_times_unique_ready_anime(tmp_path: Path) -> None:
    api, _video, service = _api(tmp_path, review_count=5, ready_anime=4, available=100)
    assert api._review_gate_prefetch_target() == 20

    started = api.review_gate_prefetch()
    assert started["started"] is True
    thread = api._review_gate_prefetch_thread
    assert thread is not None
    thread.join(timeout=3)
    assert not thread.is_alive()

    # Cold startup first publishes one episode quota, then fills the x*y pool.
    assert [required for required, _trusted in service.calls] == [5, 15]
    snapshot = api._review_gate_prefetch_snapshot("jiten:pool-test")
    assert snapshot["target"] == 20
    assert snapshot["ready_anime"] == 4
    assert snapshot["available"] == 20


def test_prefetch_pool_naturally_stops_at_provider_available_count(tmp_path: Path) -> None:
    api, _video, service = _api(tmp_path, review_count=5, ready_anime=4, available=13)
    api.review_gate_prefetch()
    api._review_gate_prefetch_thread.join(timeout=3)
    snapshot = api._review_gate_prefetch_snapshot("jiten:pool-test")
    assert snapshot["target"] == 20
    assert snapshot["available"] == 13

    # Provider reports only 13 reviewable cards, so effective target is
    # min(13, 5*4)=13. A minute tick must treat that pool as complete instead of
    # creating another provider burst.
    calls_before = len(service.calls)
    api._review_gate_prefetch_fetched_at -= 600
    settled = api.review_gate_prefetch()
    assert settled["started"] is False
    assert settled["reason"] == "satisfied"
    assert settled["effective_target"] == 13
    assert settled["provider_available"] == 13
    assert len(service.calls) == calls_before


def test_minute_refresh_reuses_proven_history_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    history_calls: list[str] = []

    class _Response:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, *, headers, params=None, timeout):
        if url.endswith("/srs/study-batch"):
            return _Response(
                {
                    "sessionId": "s1",
                    "cards": [
                        {
                            "wordId": 11,
                            "readingIndex": 0,
                            "isNewCard": False,
                            "wordTextPlain": "猫",
                        },
                        {
                            "wordId": 12,
                            "readingIndex": 0,
                            "isNewCard": False,
                            "wordTextPlain": "犬",
                        },
                    ],
                }
            )
        history_calls.append(url)
        return _Response(
            {
                "card": {"state": 2, "due": "2026-09-15T00:00:00Z"},
                "reviews": [{"rating": 3, "reviewDateTime": "2026-09-13T00:00:00Z"}],
            }
        )

    monkeypatch.setattr("pudge.review_providers.httpx.get", fake_get)
    provider = JitenReviewProvider("token")
    first = provider.list_strict_review_candidates(required=2)
    assert len(first["cards"]) == 2
    assert len(history_calls) == 2

    history_calls.clear()
    second = provider.list_strict_review_candidates(
        required=2,
        trusted_previous_keys={"11:0", "12:0"},
    )
    assert len(second["cards"]) == 2
    assert history_calls == []


# review_gate_prefetch: no cards until window.__warmAt ms after start (null: never), a target
# of window.__target cards.
WARM = r"""
window.__warmAt = 1000; window.__target = 4;
__api.review_gate_prefetch = async () => ({target: window.__target,
  cards: window.__warmAt !== null && Date.now() - window.__start >= window.__warmAt
    ? [1, 2, 3].map(id => ({wordId: id, readingIndex: 0, wordText: '一二三'[id - 1], wordTextPlain: '一二三'[id - 1], pudgeCardKey: `${id}:0`}))
    : []});
window.__start = Date.now();
"""
PREFETCHES = "__apiCalls.filter(c => c.name === 'review_gate_prefetch').map(c => c.at)"


def test_webview_polls_fast_only_until_backend_warm_cache_arrives(page) -> None:
    def run(code: str, setup: str = ""):
        return page.run(code, before=WARM + setup).value

    # The warm cards arrive after 1 s: polled every 250 ms until then, afterwards once a
    # minute, and not while the window is in the background.
    timeline = run(f"""
        await __tick(5000); const fast = {PREFETCHES};
        await __tick(56000); const minute = {PREFETCHES};
        await __setHidden(true); await __tick(120000); const hidden = {PREFETCHES};
        await __setHidden(false); await __tick(10);
        return {{fast, minute, hidden, back: {PREFETCHES}}};
        """)
    assert timeline["fast"] == [0, 250, 500, 750, 1000]
    assert timeline["minute"] == timeline["fast"] + [60000]
    assert timeline["hidden"] == timeline["minute"]
    assert len(timeline["back"]) == len(timeline["hidden"]) + 1  # coming back refreshes at once
    # Never warm: the fast polling gives up after 15 s.
    never = run(f"await __tick(30000); return {PREFETCHES};", "window.__warmAt = null;")
    assert never[-1] == 15000 and len(never) == 61
    # Nothing to warm (no ready episode): no fast polling at all.
    assert run(f"await __tick(30000); return {PREFETCHES};", "window.__target = 0;") == [0]


def test_gate_opens_with_the_warm_cards_not_yet_counted(page) -> None:
    result = page.run(
        """
        __api.review_gate_status = async () => ({blocking: true, required: 3, completed: 1, remaining: 2, episode: 4,
                                                 confirmed_card_keys: ['1:0']});
        __api.review_gate_begin = () => new Promise(() => {});        // still authorizing
        await __tick(2000);
        void startPlay('/v/Show - 04.mkv'); await __tick(20);
        return {word: __text('#pudgeReviewGate .pudge-review-gate-word'),
                grades: [...document.querySelectorAll('#pudgeReviewGate [data-review-gate-grade]')].map(b => b.disabled)};
        """,
        before=WARM + "window.__warmAt = 0;",
    )
    # Card 1 was already reviewed for this episode: the gate starts at card 2, at once.
    assert result.value["word"] == "二"
    assert result.value["grades"] == [True] * 4


def test_normal_jiten_card_reuses_cached_deck_metadata(page) -> None:
    result = page.run(
        """
        const decks = () => __apiCalls.filter(c => c.name === 'study_decks').length;
        const box = document.createElement('div');
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['猫犬'],
          tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 1}, {wordId: 2, readingIndex: 0, start: 1, end: 2}]],
          vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', states: ['young']}, {wordId: 2, readingIndex: 0, spelling: '犬', states: ['young']}]},
          {backend: 'jiten'});
        document.body.appendChild(box);
        const open = async i => { await __click(`[data-pudge-study-token="${box.querySelectorAll('[data-pudge-study-token]')[i].dataset.pudgeStudyToken}"]`); await __tick(20); };
        const options = () => [...document.querySelectorAll('#pudgeStudyDeck option')].map(o => o.value);
        const out = {startup: decks()};
        await open(0);                                   // the start-up request is still on its way: shared
        out.pending = {calls: decks(), options: options()};
        window.__deckAnswers.splice(0).forEach(answer => answer()); await __tick(20);
        out.answered = options();
        await __click('[data-pudge-study-close]');
        await open(1);
        out.reopened = {calls: decks(), options: options()};
        await __click('[data-pudge-study-close]');
        await __tick(5 * 60_000 + 1000);                 // five minutes later the decks are asked again
        await open(0);
        out.later = decks();
        return out;
        """,
        before="""
        window.__deckAnswers = [];
        __api.study_decks = () => new Promise(resolve => window.__deckAnswers.push(() => resolve([{id: 12, name: 'Deck A'}])));
        """,
    )
    out = result.value
    assert out["startup"] == 1  # fetched when the window starts
    assert out["pending"] == {"calls": 1, "options": [""]}
    assert out["answered"] == ["", "12"]
    assert out["reopened"] == {"calls": 1, "options": ["", "12"]}
    assert out["later"] == 2


def test_cold_click_fetches_only_current_episode_quota_then_warms_full_pool(tmp_path: Path) -> None:
    api, video, service = _api(tmp_path, review_count=5, ready_anime=4, available=100)
    assert video is not None

    started = api.review_gate_begin(str(video))
    assert started["blocking"] is True
    assert started["issued"] == 5
    # The synchronous click path is bounded by X, not X*Y.
    assert service.calls[0][0] == 5

    thread = api._review_gate_prefetch_thread
    if thread is not None:
        thread.join(timeout=3)
        assert not thread.is_alive()
    # The full 20-card account pool is filled only in the background.
    assert any(required == 15 for required, _trusted in service.calls[1:])


def test_refill_does_not_compete_with_every_grade(tmp_path: Path) -> None:
    api, _video, _service = _api(tmp_path, review_count=5, ready_anime=4, available=100)
    api._ensure_review_gate_runtime()
    api._review_gate_prefetch_account_key = "jiten:pool-test"
    api._review_gate_prefetch_cards = [{"pudgeCardKey": f"{i}:0"} for i in range(19)]
    calls: list[bool] = []
    api.review_gate_prefetch = lambda force=False: calls.append(bool(force)) or {"started": True}

    # 20-card target, 5-card episode quota: losing one card must not create
    # provider traffic that can contend with the next interactive card.
    api._review_gate_refill_if_needed("jiten:pool-test")
    assert calls == []

    # Refill after an episode quota has actually been consumed...
    api._review_gate_prefetch_cards = api._review_gate_prefetch_cards[:14]
    api._review_gate_refill_if_needed("jiten:pool-test")
    assert calls == [True]

    # ...or exactly once when the episode gate is completed.
    calls.clear()
    api._review_gate_prefetch_cards = [{"pudgeCardKey": f"{i}:0"} for i in range(19)]
    api._review_gate_refill_if_needed("jiten:pool-test", episode_completed=True)
    assert calls == [True]


def test_provider_collects_multiple_jiten_batches_beyond_user_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_calls = 0
    history_calls: list[str] = []

    class _Response:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    batches = [
        [11, 12, 13],
        [12, 13, 14],
        [13, 14, 15],
    ]

    def fake_get(url, *, headers, params=None, timeout):
        nonlocal batch_calls
        if url.endswith("/srs/study-batch"):
            ids = batches[min(batch_calls, len(batches) - 1)]
            batch_calls += 1
            return _Response(
                {
                    "sessionId": "s1",
                    "reviewsRemaining": 20,
                    "cards": [
                        {
                            "wordId": word_id,
                            "readingIndex": 0,
                            "isNewCard": False,
                            "wordTextPlain": f"語{word_id}",
                        }
                        for word_id in ids
                    ],
                }
            )
        history_calls.append(url)
        return _Response(
            {
                "card": {"state": 2, "due": "2026-09-15T00:00:00Z"},
                "reviews": [{"rating": 3, "reviewDateTime": "2026-09-13T00:00:00Z"}],
            }
        )

    monkeypatch.setattr("pudge.review_providers.httpx.get", fake_get)
    provider = JitenReviewProvider("token")
    result = provider.list_strict_review_candidates(required=5)
    assert [card["wordId"] for card in result["cards"]] == [11, 12, 13, 14, 15]
    assert batch_calls == 3
    assert len(history_calls) == 5


def test_minute_tick_does_not_refetch_when_x_times_ready_pool_is_full(tmp_path: Path) -> None:
    api, _video, service = _api(tmp_path, review_count=5, ready_anime=4, available=100)
    api.review_gate_prefetch()
    api._review_gate_prefetch_thread.join(timeout=3)
    assert len(api._review_gate_prefetch_cards) == 20
    calls_before = len(service.calls)

    # Even if the provider timestamp is old, the minute tick only recomputes the
    # local X*ready target and leaves a full pool alone.
    api._review_gate_prefetch_fetched_at -= 600
    result = api.review_gate_prefetch()
    assert result["started"] is False
    assert result["reason"] == "satisfied"
    assert len(service.calls) == calls_before


def test_provider_uses_reviews_remaining_as_available_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class _Response:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, *, headers, params=None, timeout):
        nonlocal calls
        if url.endswith("/srs/study-batch"):
            calls += 1
            return _Response(
                {
                    "sessionId": "s1",
                    "reviewsRemaining": 1,
                    "cards": [
                        {"wordId": 31, "readingIndex": 0, "isNewCard": False, "wordTextPlain": "一"},
                        {"wordId": 32, "readingIndex": 0, "isNewCard": False, "wordTextPlain": "二"},
                    ],
                }
            )
        return _Response(
            {
                "card": {"state": 2, "due": "2026-09-15T00:00:00Z"},
                "reviews": [{"rating": 3, "reviewDateTime": "2026-09-13T00:00:00Z"}],
            }
        )

    monkeypatch.setattr("pudge.review_providers.httpx.get", fake_get)
    result = JitenReviewProvider("token").list_strict_review_candidates(required=20)
    assert result["provider_available_estimate"] == 3
    assert result["requested"] == 20
    # One repeated batch cannot grow beyond the same two IDs, and the adapter
    # terminates instead of chasing the unreachable X*Y target forever.
    assert len(result["cards"]) == 2
    assert calls <= 3


def test_reading_tools_headless_import_does_not_require_window_event_target() -> None:
    script = f"""
    global.window=global;global.document={{addEventListener(){{}}}};
    require({str(WEB / "reading_tools.js")!r});
    const yes=PudgeReadingTools.study.inlinePitchOnSurface('かな',{{reading:'カナ',pitchAccents:[1]}});
    process.stdout.write(JSON.stringify({{ok:!!yes}}));
    """
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert '"ok":true' in result.stdout
