from __future__ import annotations

import json
from pathlib import Path

import pudge.review_providers as review_providers
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.values[key] = value


class _Response:
    status_code = 200
    content = b'{"success":true,"cardDeleted":false}'

    def json(self):
        return json.loads(self.content)


def test_review_gate_store_can_unconfirm_after_provider_undo() -> None:
    state = _State()
    store = ReviewGateStore(state, prefix="undo-test")
    identity = EpisodeReviewIdentity(10, 2)
    store.mark_confirmed("account", identity, required=2, card_key="1:0")
    second = store.mark_confirmed("account", identity, required=2, card_key="2:0")
    assert second.granted
    undone = store.unmark_confirmed("account", identity, required=2, card_key="2:0")
    assert undone.confirmed_card_keys == ("1:0",)
    assert undone.completed == 1
    assert undone.remaining == 1
    assert not undone.granted


def test_jiten_provider_uses_native_undo_endpoint(monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(review_providers.httpx, "post", fake_post)
    result = review_providers.JitenReviewProvider("token").undo_review(123, 4)
    assert result["outcome"] == "undone"
    assert calls[0][0].endswith("/api/srs/undo-review")
    assert calls[0][1]["json"] == {"wordId": 123, "readingIndex": 4}


def test_review_gate_uses_one_stable_word_slot_and_ruby_after_reveal() -> None:
    js = (WEB / "review_gate.js").read_text(encoding="utf-8")
    css = (WEB / "review_gate.css").read_text(encoding="utf-8")
    assert "function frontWordHtml(card, showReading)" in js
    assert "return rubyHtml(frontRuby);" in js
    assert "const frontMarkup = frontWordHtml(card, revealed);" in js
    front_block = js.split("if (card) {", 1)[1].split("} else {", 1)[0]
    assert "rubyHtml(" not in front_block
    assert "${frontMarkup}" in js
    assert "G10 stable word slot + optimistic previous-review undo" in css
    assert ".pudge-review-gate-card.answer-shown .pudge-review-gate-front{display:flex!important}" in css
    assert 'font-family:"Hiragino Mincho ProN","Yu Mincho",serif' in css


def test_previous_review_strip_and_command_z_are_wired() -> None:
    js = (WEB / "review_gate.js").read_text(encoding="utf-8")
    assert "function previousReviewHtml()" in js
    assert "data-review-gate-undo" in js
    assert "async function undoLastReview()" in js
    assert "event?.metaKey" in js
    assert "key.toLowerCase() === 'z'" in js
    assert "window.pywebview.api.review_gate_undo(" in js
    assert "reviewHistory.push(historyEntry);" in js
    assert "cardQueue.unshift({...entry.card});" in js


def test_undo_repaints_before_waiting_for_server() -> None:
    js = (WEB / "review_gate.js").read_text(encoding="utf-8")
    start = js.index("async function undoLastReview()")
    end = js.index("async function open(", start)
    undo = js[start:end]
    assert undo.index("cardQueue.unshift({...entry.card});") < undo.index("render({loading:false}, {replaceCards:false});")
    assert undo.index("render({loading:false}, {replaceCards:false});") < undo.index("await entry.submitPromise")
    assert undo.index("await entry.submitPromise") < undo.index("await window.pywebview.api.review_gate_undo(")
