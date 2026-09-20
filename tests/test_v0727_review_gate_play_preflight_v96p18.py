from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def _start_play_source() -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return html.split(
        "async function startPlay(path,resume=false,allowImageSubtitles=false,queueContext=null,allowUnsyncedSubtitles=false){",
        1,
    )[1].split("\nwindow.startPlay=startPlay;", 1)[0]


def test_v96p18_review_gate_preflight_happens_before_starting_or_play_rpc() -> None:
    source = _start_play_source()
    preflight = source.index("await pywebview.api.review_gate_status(path)")
    gate_open = source.index("await window.PudgeReviewGate?.open?.(launch,gate)", preflight)
    starting = source.index("setPathPlayState(path,'starting')")
    play_rpc = source.index("await pywebview.api.play(")
    assert preflight < gate_open < starting < play_rpc


def test_v96p18_review_gate_preflight_has_non_visual_reentry_guard() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = _start_play_source()
    assert "reviewGateChecks:new Set()" in html
    assert "if(window.PudgeReviewGate?.isOpen?.())return;" in source
    assert "if(ui.reviewGateChecks.has(path))return;" in source
    add = source.index("ui.reviewGateChecks.add(path);")
    preflight = source.index("await pywebview.api.review_gate_status(path)")
    remove = source.index("finally{ui.reviewGateChecks.delete(path);}")
    starting = source.index("setPathPlayState(path,'starting')")
    assert add < preflight < remove < starting


def test_v96p18_backend_gate_fallback_and_original_launch_context_remain() -> None:
    source = _start_play_source()
    assert "if(result?.review_gate_required)" in source
    assert "await window.PudgeReviewGate?.open?.(launch,result.review_gate||{})" in source
    assert "allowUnsyncedSubtitles:!!allowUnsyncedSubtitles" in source
    gate = (WEB / "review_gate.js").read_text(encoding="utf-8")
    assert "await window.startPlay?.(" in gate
    assert "pending.queueContext || null" in gate
