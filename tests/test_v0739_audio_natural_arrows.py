from pathlib import Path


ROOT = Path(__file__).parents[1]
NEW_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:-15,ArrowDown:15}"
OLD_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:15,ArrowDown:-15}"


def test_ln_and_audiobook_arrow_directions_match() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert f"const lnAudioShortcuts={NEW_MAP};" in html
    assert f"const audioShortcuts={NEW_MAP};" in media
    assert OLD_MAP not in html
    assert OLD_MAP not in media
