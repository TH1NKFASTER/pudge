"""Fix OCR misreads of katakana words with the series' own vocabulary.

Names and recurring katakana words repeat across a series. When OCR reads one
occurrence with a single look-alike character (ソ for ゾ, フ for プ, 口 for ロ ...)
the misread spelling is rare while the real one is frequent, so the rare
spelling is replaced by the frequent one. Only same-length, one-character
substitutions between known look-alikes are made, so segment geometry stays
valid and ordinary rare words are left alone.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

# Characters OCR confuses through (han)dakuten, small kana or the matching kanji.
_SOUND_GROUPS = (
    "ソゾ",
    "シジ",
    "ツヅ",
    "ハバパ",
    "ヒビピ",
    "フブプ",
    "ヘベペ",
    "ホボポ",
    "カガ",
    "キギ",
    "クグ",
    "ケゲ",
    "コゴ",
    "サザ",
    "スズ",
    "セゼ",
    "タダ",
    "チヂ",
    "テデ",
    "トド",
    "ウヴ",
    "ッツ",
    "ャヤ",
    "ュユ",
    "ョヨ",
    "ァア",
    "ィイ",
    "ゥウ",
    "ェエ",
    "ォオ",
    "ヮワ",
    "ロ口",
    "カ力",
    "エ工",
    "タ夕",
    "ニ二",
    "ハ八",
    "ト卜",
    "ー一",
    "ヘへ",
    "ベべ",
    "ペぺ",
    "リり",
)
# Different letters with a similar shape: replaced only for very frequent words.
_SHAPE_GROUPS = ("ソン", "シツ", "ワウ", "ノソ", "クタ", "ルレ", "ラヲ", "メナ", "マア", "チテ", "ユコ")
SOUND_FLOOR = 30
SHAPE_FLOOR = 100
RATIO = 15
MIN_LENGTH = 3

_KATAKANA = re.compile(r"[ァ-ヺー]")
_LOOKALIKES = frozenset("口力工夕二八卜一へべぺり")


def _table(groups: Iterable[str]) -> dict[str, frozenset[str]]:
    table: dict[str, set[str]] = {}
    for group in groups:
        for character in group:
            table.setdefault(character, set()).update(set(group) - {character})
    return {key: frozenset(value) for key, value in table.items()}


_SOUND = _table(_SOUND_GROUPS)
_SHAPE = _table(_SHAPE_GROUPS)


def term_spans(text: str) -> list[tuple[int, int, str]]:
    """Katakana words of ``text`` (look-alike kanji/hiragana inside them included)."""
    spans: list[tuple[int, int, str]] = []
    start = -1
    for index, character in enumerate(text + "\0"):
        if _KATAKANA.match(character) or (character in _LOOKALIKES and start >= 0):
            if start < 0:
                start = index
            continue
        if start >= 0:
            end = index
            while end > start and text[end - 1] in _LOOKALIKES:
                end -= 1
            word = text[start:end]
            katakana = sum(1 for item in word if _KATAKANA.match(item))
            if len(word) >= MIN_LENGTH and katakana * 2 > len(word):
                spans.append((start, end, word))
            start = -1
    return spans


def count_terms(texts: Iterable[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(word for _start, _end, word in term_spans(str(text or "")))
    return counts


def build_fixes(counts: Mapping[str, int]) -> dict[str, str]:
    """Rare spelling -> frequent spelling one look-alike character away."""
    fixes: dict[str, str] = {}
    for word, count in counts.items():
        if len(word) < MIN_LENGTH:
            continue  # two-letter words are too ambiguous to correct
        best: tuple[int, str] | None = None
        for index, character in enumerate(word):
            last = index == len(word) - 1
            for table, floor in ((_SOUND, SOUND_FLOOR), (_SHAPE, SHAPE_FLOOR)):
                for alternative in table.get(character, ()):
                    if last and {character, alternative} <= {"ッ", "ツ"}:
                        continue  # a final small ッ is a sound effect, not a misread ツ
                    candidate = word[:index] + alternative + word[index + 1 :]
                    frequency = int(counts.get(candidate, 0))
                    if (
                        frequency >= floor
                        and frequency >= RATIO * int(count)
                        and (best is None or frequency > best[0])
                    ):
                        best = (frequency, candidate)
        if best is not None:
            fixes[word] = best[1]
    return fixes


def fix_text(text: str, fixes: Mapping[str, str]) -> tuple[str, list[dict[str, str]]]:
    if not fixes or not text:
        return text, []
    changed: list[dict[str, str]] = []
    result = text
    for start, end, word in term_spans(text):
        replacement = fixes.get(word)
        if replacement and len(replacement) == end - start:
            result = result[:start] + replacement + result[end:]
            changed.append({"from": word, "to": replacement})
    return result, changed


def fix_region(region: dict[str, Any], fixes: Mapping[str, str]) -> dict[str, Any]:
    """Region with series fixes applied."""
    if not fixes:
        return region
    text = str(region.get("text") or "")
    fixed, changed = fix_text(text, fixes)
    if not changed:
        return region
    segments = region.get("segments")
    if isinstance(segments, list) and segments:
        compact = [character for character in text if not character.isspace()]
        if [str(item.get("text") or "") for item in segments if isinstance(item, dict)] != compact:
            return region  # geometry is not one box per character: keep text and boxes in step
        fixed_compact = [character for character in fixed if not character.isspace()]
        segments = [{**item, "text": character} for item, character in zip(segments, fixed_compact)]
    result = {**region, "text": fixed, "lexicon_fixes": changed}
    if isinstance(region.get("segments"), list) and region.get("segments"):
        result["segments"] = segments
    result.setdefault("raw_text", text)
    return result
