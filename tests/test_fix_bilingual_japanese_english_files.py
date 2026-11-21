"""A Japanese + English subtitle ("KonoSuba 01.ja-en.ass") whose English lines, signs and
romaji karaoke outnumber the Japanese letters is still Japanese text: it stays a
(low-priority) candidate instead of being dropped as "not Japanese".  English-only and
Chinese files are still rejected.  Invented subtitle text."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.language import has_japanese_marker, is_japanese_subtitle
from pudge.subtitle_formats import subtitle_filename_language_profile

HEAD = (
    "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)
JP = ["カズマ", "めぐみん、爆裂魔法", "アクア様！", "行くぞ", "ダクネス？"]
EN = [
    "Kazuma, what on earth are you doing over there right now?",
    "Megumin's explosion magic is amazing, isn't it, everyone?",
    "Worship the great goddess Aqua, you lowly mortals!",
    "We're going on another quest again today, like it or not.",
    "Darkness, are you okay? You look a little flushed again.",
]


def _event(i: int, text: str, style: str) -> str:
    return f"Dialogue: 0,0:{i // 60 % 60:02d}:{i % 60:02d}.00,0:{i // 60 % 60:02d}:{i % 60:02d}.90,{style},,0,0,0,,{text}\n"


def _heavy_bilingual(japanese: bool = True, chinese: bool = False) -> str:
    body = []
    for i in range(300):
        if japanese:
            body.append(_event(i, f"{JP[i % 5]}{i}", "JP"))
        if chinese:
            body.append(_event(i, f"我们走吧第{i}次", "CN"))
        body.append(_event(i, f"{EN[i % 5]} ({i})", "EN"))
    body += [_event(i, f"Adventurers Guild notice board number {i}", "Sign") for i in range(600)]
    body += [_event(i, "{\\k20}tokai no hito no furi wo shite mo fx" + str(i), "OP") for i in range(3000)]
    return HEAD + "".join(body)


@pytest.mark.parametrize(
    "name",
    [
        "KonoSuba 01.ja-en.ass",
        "Kono Subarashii Sekai ni Shukufuku wo! - 01 [ja-en].ass",
        "KonoSuba S01E01 (Japanese, English).ass",
        "KonoSuba S01E01 [JPN,ENG].ass",
    ],
)
def test_bilingual_file_with_real_japanese_lines_is_japanese(tmp_path: Path, name: str):
    path = tmp_path / name
    path.write_text(_heavy_bilingual(), encoding="utf-8")
    assert is_japanese_subtitle(path)


def test_bilingual_name_stays_low_priority():
    assert subtitle_filename_language_profile("KonoSuba 01.ja-en.ass")["purity"] == "mixed_japanese_english"


def test_comma_separated_language_tag_is_a_marker():
    assert has_japanese_marker("Show 01 (Japanese, English).ass")
    assert has_japanese_marker("Show 01 [JPN,ENG].ass")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("KonoSuba 01.ja-en.ass", _heavy_bilingual(japanese=False)),  # the label lies: English only
        ("KonoSuba 01.ja-en.ass", _heavy_bilingual(japanese=False, chinese=True)),  # Chinese + English
        ("KonoSuba 01.en.ass", _heavy_bilingual()),  # no Japanese label: the old rule
        ("KonoSuba 01 (English, Russian).ass", _heavy_bilingual()),
    ],
)
def test_without_japanese_lines_or_label_it_is_still_rejected(tmp_path: Path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    assert not is_japanese_subtitle(path)
