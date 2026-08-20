from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_renderer_keeps_explicit_pause_support_for_nonstandard_callers() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "speechActive=options.speechActive!==false" in source
    assert "if(!speechActive)" in source
    assert "frozen=true;word=previous||null" in source
    assert "Number.isFinite(offset)&&!frozen" in source
    assert "speech_active:speechActive" in source


def test_normal_playback_uses_linear_anchor_clock_without_fft_micro_freezes() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedSpeechActive(anchor,time)" in source
    assert "speechActive=lnPairedSpeechActive(anchor,estimatedTime)" not in source
    assert "speechActive=lnPairedSpeechActive(anchor,position)" not in source
    assert (
        "renderLnPairedPosition(renderState,estimatedOffset,{speechActive:true,previewOffset})"
        in source
    )
    assert (
        "renderLnPairedPosition(state,displayOffset,"
        "{reason:'poll',speechActive:true,previewOffset})"
        in source
    )
