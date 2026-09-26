from pathlib import Path

from PIL import Image


def test_macos_fallback_app_icon_keeps_standard_grid_breathing_room() -> None:
    icon = Path(__file__).parents[1] / "pudge" / "assets" / "app-icon.png"
    with Image.open(icon) as image:
        rgba = image.convert("RGBA")
        assert rgba.size == (1024, 1024)
        # Static PNG/ICNS fallback: keep the visible body inside the conventional
        # ~824 px macOS icon grid instead of using a nearly full-bleed 922 px body.
        assert rgba.getbbox() == (100, 100, 924, 924)
