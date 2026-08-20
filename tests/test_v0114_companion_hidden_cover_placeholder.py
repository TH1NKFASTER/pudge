from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_companion_hidden_cover_placeholder_cannot_override_hidden_attribute() -> None:
    css = (ROOT / "pudge" / "web" / "companion" / "styles.css").read_text(encoding="utf-8")
    compact = css.replace(" ", "").replace("\n", "")
    assert "[hidden]{display:none!important}" in compact
