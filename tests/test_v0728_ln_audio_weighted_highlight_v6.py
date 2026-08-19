from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_v6_only_switches_after_current_word_visually_finishes() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "progress=parseFloat(previous.style.getPropertyValue(" in source
    assert "until:performance.now()+34" not in source
    assert "previous_progress:progress" in source
    assert "renderLnPairedPosition(state,estimatedOffset,{speechActive:true,previewOffset})" in source


def test_v6_preserves_silence_freeze_contracts() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "speechActive=options.speechActive!==false" in source
    assert "if(!speechActive)" in source
    assert "word=previousWord" in source
    assert "word=null" in source
    assert "&&speechActive)" in source
    assert "ln-paired-word-finishing" not in source
