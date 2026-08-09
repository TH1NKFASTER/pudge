from __future__ import annotations

import re
from pathlib import Path

try:
    from charset_normalizer import from_bytes
except ImportError:  # pragma: no cover
    from_bytes = None


TEXT_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt"}
IMAGE_SUBTITLE_EXTENSIONS = {".sup", ".pgs", ".idx"}
JAPANESE_LANGUAGE_CODES = {"ja", "jp", "jpn", "japanese"}

JA_MARKER_RE = re.compile(r"(?i)(?:^|[\s._\-\[\(])(ja|jp|jpn|japanese|日本語)(?:$|[\s._\-\[\]\)])")
NEGATIVE_MARKER_RE = re.compile(r"(?i)(?:^|[\s._\-\[\(])(en|eng|english|rus|ru|russian)(?:$|[\s._\-\[\]\)])")
ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
HTML_TAG_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{2,3}\s*--?>?\s*\d{1,2}:\d{2}:\d{2}[,.]\d{2,3}")


def normalize_language_code(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().casefold().replace("_", "-").split("-")[0]


def has_japanese_marker(value: str) -> bool:
    return bool(JA_MARKER_RE.search(value))


def has_negative_language_marker(value: str) -> bool:
    return bool(NEGATIVE_MARKER_RE.search(value))


def decode_subtitle_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "shift_jis", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    if from_bytes is not None:
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    return data.decode("utf-8", errors="replace")


def subtitle_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as fh:
            return decode_subtitle_bytes(fh.read(max_bytes))
    except OSError:
        return ""


def strip_subtitle_markup(text: str) -> str:
    text = ASS_TAG_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = TIMESTAMP_RE.sub(" ", text)
    text = re.sub(r"(?im)^\s*(?:dialogue|comment):[^,]*(?:,[^,]*){8},", " ", text)
    text = re.sub(r"(?m)^\s*\d+\s*$", " ", text)
    return text


def japanese_text_metrics(text: str) -> dict[str, float | int | bool]:
    text = strip_subtitle_markup(text)
    hiragana = sum("\u3040" <= ch <= "\u309f" for ch in text)
    katakana = sum("\u30a0" <= ch <= "\u30ff" for ch in text)
    kanji = sum(("\u3400" <= ch <= "\u4dbf") or ("\u4e00" <= ch <= "\u9fff") for ch in text)
    latin = sum(ch.isascii() and ch.isalpha() for ch in text)
    letters = hiragana + katakana + kanji + latin
    kana = hiragana + katakana
    japanese = kana + kanji
    kana_ratio = kana / max(letters, 1)
    japanese_ratio = japanese / max(letters, 1)

    # Kana is required to reject Chinese subtitles that contain only Han characters.
    detected = kana >= 10 and kana_ratio >= 0.015 and japanese_ratio >= 0.08
    return {
        "detected": detected,
        "hiragana": hiragana,
        "katakana": katakana,
        "kanji": kanji,
        "kana_ratio": kana_ratio,
        "japanese_ratio": japanese_ratio,
    }


def is_japanese_subtitle(path: Path) -> bool:
    if has_negative_language_marker(path.name) and not has_japanese_marker(path.name):
        return False
    if has_japanese_marker(path.name):
        return True
    if path.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS:
        return False
    return bool(japanese_text_metrics(subtitle_text(path))["detected"])
