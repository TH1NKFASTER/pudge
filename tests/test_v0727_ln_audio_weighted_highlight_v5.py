from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_v5_progress_requires_active_speech_explicitly() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "if(active&&Number.isFinite(offset)&&!frozen&&speechActive)" in source
    assert "&&speechActive)" in source
