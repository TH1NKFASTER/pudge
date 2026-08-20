from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_v3_preserves_silence_and_poll_contracts() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "speechActive=options.speechActive!==false" in source
    assert "if(!speechActive)" in source
    assert "const previousWord=previous;" in source
    assert "word=previousWord" in source
    assert "lnPairedOffsetAtTime(state,position)" in source
    assert "{reason:'poll',speechActive:true,previewOffset}" in source
    assert "speech_active:speechActive" in source
