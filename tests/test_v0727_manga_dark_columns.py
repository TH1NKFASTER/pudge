from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _infer_vertical_character_segments


def test_dark_narration_uses_real_variable_vertical_columns() -> None:
    image = Image.new("RGB", (240, 300), "white")
    draw = ImageDraw.Draw(image)
    # Irregular black narration panel with a white-text interior.
    draw.rectangle((12, 14, 226, 286), fill="black")
    # Main vertical columns: 6 / 6 / 7 / 7 glyphs, right-to-left.
    counts = [6, 6, 7, 7]
    xs = [184, 148, 82, 46]
    tops = [48, 48, 100, 100]
    for x, top, count in zip(xs, tops, counts):
        for row in range(count):
            y = top + row * 24
            draw.rectangle((x, y, x + 14, y + 15), fill="white")
    # Small furigana-like noise beside main columns must not become columns.
    for x, top in ((205, 54), (104, 110)):
        for row in range(4):
            y = top + row * 13
            draw.rectangle((x, y, x + 4, y + 6), fill="white")

    region = {
        "x": 0.05,
        "y": 0.04,
        "width": 0.90,
        "height": 0.91,
        "orientation": "vertical",
        "source": "dark-block-proposal",
    }
    text = "彼の死に際に放つだ一言は全世界の人々を海へ駆り立てた"
    segments = _infer_vertical_character_segments(image, region, text)

    assert "".join(str(segment["text"]) for segment in segments) == text
    assert len(segments) == 26
    assert {segment["source"] for segment in segments} == {"dark-columns-v1"}

    groups: list[list[str]] = []
    last_x: float | None = None
    for segment in segments:
        x = float(segment["x"])
        if last_x is None or abs(x - last_x) > 1e-6:
            groups.append([])
            last_x = x
        groups[-1].append(str(segment["text"]))
    assert ["".join(group) for group in groups] == [
        "彼の死に際に",
        "放つだ一言は",
        "全世界の人々を",
        "海へ駆り立てた",
    ]
