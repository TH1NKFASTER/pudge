from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_v4_keeps_all_speech_pause_contracts_without_changing_behavior() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "frozen=true;word=previous||null" in source
    assert "word=previousWord" in source
    assert "word=null" in source
    assert "candidate=word" in source
    assert "speechActive=options.speechActive!==false" in source
    assert "lnPairedOffsetAtTime(state,Number(state.position))" in source
