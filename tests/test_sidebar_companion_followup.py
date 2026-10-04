from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import time
import subprocess

from pudge.web_app import WebAppApi

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"
SIDEBAR = (WEB / "sidebar_companion.js").read_text(encoding="utf-8")
CSS = (WEB / "sidebar_companion.css").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
WEB_APP = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")


def test_sidebar_review_card_has_full_review_ux_and_shortcuts() -> None:
    assert "wordLines(word)" in SIDEBAR
    assert "chars.length <= 8" in SIDEBAR
    assert "sidebar-due-word" in SIDEBAR
    assert "rubyHtml" in SIDEBAR
    assert "pitchAccents" in SIDEBAR
    assert "inlinePitch" in SIDEBAR
    assert "sidebar-due-meta" not in SIDEBAR
    assert "exampleSentence" in SIDEBAR
    assert "sourceDeckName" in SIDEBAR
    assert "previousReviewHtml" in SIDEBAR
    assert "matchAction" in SIDEBAR
    assert "code === 'Space'" in SIDEBAR
    assert "UNDO_WINDOW_MS = 10_000" in SIDEBAR
    assert "sidebar_due_review_undo" in SIDEBAR
    assert "font-size:42px" in CSS


def test_sidebar_review_prefetches_and_advanced_setting_is_static() -> None:
    assert "PREFETCH_TARGET = 12" in SIDEBAR
    assert "PREFETCH_LOW_WATER = 7" in SIDEBAR
    assert "prefetchPromise" in SIDEBAR
    assert "prefetchDueQueue" in SIDEBAR
    assert "sidebar_due_review_cards" in SIDEBAR
    assert "pudgeSidebarAnimeSource" in WEB_APP
    assert 'id="s_sidebar_review_interval"' in INDEX
    setting_at = INDEX.index('id="s_sidebar_review_interval"')
    category_at = INDEX.rfind('data-settings-category="advanced"', 0, setting_at)
    assert category_at >= 0
    assert 'option value="continuous"' in INDEX[setting_at:setting_at + 1200]
    assert "block.dataset.settingsCategory='essential'" not in SIDEBAR


def test_sidebar_audiobook_uses_stable_shell_and_compact_controls() -> None:
    # Position polling updates the existing DOM instead of replacing the whole
    # mini-player; this is the important anti-flicker invariant.
    render = SIDEBAR.split("function renderAudio()", 1)[1].split("async function pollAudio", 1)[0]
    assert "host.innerHTML" in render  # only clear when no active book
    update = SIDEBAR.split("function updateAudio(book)", 1)[1].split("function renderAudio", 1)[0]
    assert "innerHTML" not in update
    assert "sidebar-audio-cover" in SIDEBAR
    assert 'data-sc-audio="speed"' in SIDEBAR
    assert "AUDIO_SPEEDS" in SIDEBAR
    assert "book.title" not in SIDEBAR
    assert 'data-delta="-15"' in SIDEBAR and '>↶<' in SIDEBAR
    assert 'data-delta="15"' in SIDEBAR and '>↷<' in SIDEBAR
    assert "sidebar-audio-timeline" in SIDEBAR
    assert "audiobook_seek_to" in SIDEBAR
    assert "liveTextSignature" in SIDEBAR
    assert "chapter_char_offset_exact" in SIDEBAR
    assert "audioTextLength" in SIDEBAR
    assert "rawIndexAtAudioOffset" in SIDEBAR
    assert "paragraphAudioLength" in SIDEBAR
    assert "PUDGE_LN_IMAGE_URL" in SIDEBAR
    assert "updateLiveHighlight" in SIDEBAR
    assert "ln-paired-word-current" in SIDEBAR
    assert "requestAnimationFrame(frame)" in SIDEBAR
    assert "live?.playing ? 500 : 1400" in SIDEBAR
    assert "cropStart" in SIDEBAR and "cropStart + 512" in SIDEBAR
    assert "liveFollowFrozen = true" in SIDEBAR
    assert "height:7.75em" in CSS
    assert "width:92px;height:126px" in CSS
    assert "#app>.sidebar{min-height:0;overflow:hidden}" in CSS
    assert ".sidebar>.sidebar-bottom{flex:0 0 auto;margin-top:8px}" in CSS


class _Service:
    def __init__(self):
        self.undone = []
        self.submitted = []

    def strict_review_candidates(self, *, required, **_kwargs):
        assert required >= 4
        return {
            "cards": [
                {"wordId": 1, "readingIndex": 0, "wordTextPlain": "外", "sourceDeckName": "Other"},
                {"wordId": 2, "readingIndex": 0, "wordTextPlain": "古", "sourceDeckName": "My Anime"},
                {"wordId": 3, "readingIndex": 0, "wordTextPlain": "新", "sourceDeckName": "My Anime"},
            ]
        }

    def jiten_live_states(self, pairs, *, force=False):
        assert force is False
        return [
            {"wordId": 1, "readingIndex": 0, "states": ["young", "due"]},
            {"wordId": 2, "readingIndex": 0, "states": ["mature", "due"]},
            {"wordId": 3, "readingIndex": 0, "states": ["young", "due"]},
        ]

    def settings(self):
        return SimpleNamespace(review_mode="native",jiten_api_key="test-key",study_backend="jiten")

    def strict_review_submit(self, word_id, reading_index, grade, *, attempt_id):
        self.submitted.append((word_id, reading_index, grade, attempt_id))
        return {"saved": True}

    def strict_review_undo(self, word_id, reading_index):
        self.undone.append((word_id, reading_index))
        return {"undone": True}


class _DB:
    def anime_list(self):
        return [SimpleNamespace(title="My Anime", titles=["マイアニメ"], synonyms=[])]


def _api() -> tuple[WebAppApi, _Service]:
    service = _Service()
    obj = object.__new__(WebAppApi)
    obj.light_novels = service
    obj.manager = SimpleNamespace(db=_DB())
    obj.logger = SimpleNamespace(info=lambda *_a, **_kw: None)
    return obj, service


def test_sidebar_queue_prioritizes_anime_then_younger_due() -> None:
    api, _service = _api()
    result = api.sidebar_due_review_cards(3)
    assert [card["wordId"] for card in result["cards"]] == [3, 2, 1]
    assert result["cards"][0]["pudgeSidebarAnimeSource"] is True
    assert result["cards"][0]["pudgeSidebarAge"] == "newer"
    assert result["cards"][1]["pudgeSidebarAge"] == "older"


def test_sidebar_undo_is_server_side_limited_to_ten_seconds() -> None:
    api, service = _api()
    submitted = api.sidebar_due_review_submit(3, 0, "good", "attempt-1")
    assert submitted["outcome"] == "confirmed"
    undone = api.sidebar_due_review_undo(3, 0)
    assert undone["outcome"] == "undone"
    assert service.undone == [(3, 0)]

    api._sidebar_due_last_review = (3, 0, time.monotonic() - 11.0)
    expired = api.sidebar_due_review_undo(3, 0)
    assert expired["outcome"] == "expired"
    assert service.undone == [(3, 0)]


def test_sidebar_review_reveal_keeps_front_geometry_and_pitch_uses_kana() -> None:
    assert "frontWordHtml(dueCard, dueRevealed)" in SIDEBAR
    assert "sidebar-due-front-ruby" in SIDEBAR
    assert "hide-reading" in SIDEBAR and "show-reading" in SIDEBAR
    assert "readingFromRuby" in SIDEBAR
    assert "const reading = kanaReading(card);" in SIDEBAR
    answer = SIDEBAR.split("function answerDetails", 1)[1].split("function gradeButtonsHtml", 1)[0]
    assert "sidebar-due-answer-word" not in answer


def test_sidebar_previous_is_word_and_undo_only() -> None:
    previous = SIDEBAR.split("function previousReviewHtml()", 1)[1].split("function renderDue()", 1)[0]
    assert "frontWordHtml(previousReview.card, true)" in previous
    assert "data-sc-undo" in previous
    assert "answerDetails" not in previous
    assert "sidebar-due-previous-head" not in previous


def test_sidebar_activity_is_exclusive_and_contained() -> None:
    render_due = SIDEBAR.split("function renderDue()", 1)[1].split("function schedulePreviousExpiry", 1)[0]
    assert "if (activeBook() && !audioState?.float_open) { host.hidden = true; return; }" in render_due
    active = SIDEBAR.split("function activeBook()", 1)[1].split("function coverMarkup", 1)[0]
    assert "lastAudioId" not in active
    assert "overflow-y:auto" in CSS
    assert "max-width:100%" in CSS


def test_continuous_review_advances_from_prefetched_queue_before_submit_finishes() -> None:
    submit = SIDEBAR.split("async function submitDue", 1)[1].split("async function undoLastSidebarReview", 1)[0]
    take = submit.index("const optimisticNext = continuous ? takePrefetchedCard() : null;")
    render = submit.index("renderDue();", take)
    call = submit.index("await api().sidebar_due_review_submit", take)
    assert take < render < call


def test_sidebar_javascript_behaviors() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/sidebar_companion.cjs"), str(WEB)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "behavioral scenarios: PASS" in result.stdout


def test_sidebar_prefetch_excludes_visible_and_pending_cards() -> None:
    api, service = _api()
    seen = []
    original = service.strict_review_candidates

    def candidates(*, required, exclude_keys):
        seen.append(exclude_keys)
        return original(required=required)

    service.strict_review_candidates = candidates
    result = api.sidebar_due_review_cards(3, ["3:0", "1:0", "bad"])
    assert seen == [{"3:0", "1:0"}]
    assert [card["wordId"] for card in result["cards"]] == [2]
