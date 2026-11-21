from __future__ import annotations

import re
from pathlib import Path


def _charset_from_bytes():
    """charset_normalizer's detector, imported on first use (~20-35 ms of every start otherwise:
    the CLI, mpv helpers and the window all import this module, and it is a last resort)."""
    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover
        return None
    return from_bytes


TEXT_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt"}
IMAGE_SUBTITLE_EXTENSIONS = {".sup", ".pgs", ".idx"}
JAPANESE_LANGUAGE_CODES = {"ja", "jp", "jpn", "japanese"}

# "(Japanese, English)" / "[JPN+ENG]": a comma, "&" or "+" also ends a language tag.
JA_MARKER_RE = re.compile(r"(?i)(?:^|[\s._\-\[\(,&+])(ja|jp|jpn|japanese|日本語)(?:$|[\s._\-\[\]\),&+])")
NEGATIVE_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,&+])(en|eng|english|rus|ru|russian)(?:$|[\s._\-\[\]\),&+])"
)
ENGLISH_MARKER_RE = re.compile(r"(?i)(?:^|[\s._\-\[\(,&+])(en|eng|english)(?:$|[\s._\-\[\]\),&+])")
# A file named Japanese + English ("01.ja-en.ass"): its English lines, signs and romaji
# karaoke can outnumber the Japanese letters, so the share of Japanese text proves
# nothing; enough distinct Japanese lines do (bilingual files stay low-priority candidates).
_BILINGUAL_MIN_KANA_LINES = 20
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


def _utf16_without_bom(data: bytes) -> str:
    """The UTF-16 codec when every other byte is NUL, else an empty string."""
    # Subtitles are ASCII-heavy (cue numbers, timestamps, ASS fields), so UTF-16
    # shows NUL in every other byte; UTF-8 and Shift-JIS text never has NUL.
    sample = data[:8192]
    half = len(sample) // 2
    if half < 8:
        return ""
    even, odd = sample[0::2].count(0), sample[1::2].count(0)
    if odd >= half * 0.2 and even <= odd * 0.05:
        return "utf-16-le"
    if even >= half * 0.2 and odd <= even * 0.05:
        return "utf-16-be"
    return ""


_ISO2022_JP_ESCAPES_RE = re.compile(rb"\x1b\$[@B]|\x1b\$\(D")


def _strict_decode(data: bytes, encoding: str) -> str | None:
    """``data`` in ``encoding`` without replacement characters, or None.

    A read cut at max_bytes may split the last multibyte character; that tail
    alone does not disqualify the encoding.
    """
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        if (
            exc.reason == "incomplete multibyte sequence"
            and exc.end == len(data)
            and len(data) - exc.start < 8
        ):
            try:
                return data[: exc.start].decode(encoding)
            except UnicodeDecodeError:
                return None
        return None


def decode_subtitle_bytes(data: bytes) -> str:
    # UTF-16 only with a BOM or its NUL pattern: tried blindly first, any
    # even-length Shift-JIS file "decoded" as UTF-16 garbage.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    utf16 = _utf16_without_bom(data)
    if utf16:
        return data.decode(utf16, errors="replace")
    # ISO-2022-JP is 7-bit, so it "decodes" as UTF-8 with the escape
    # sequences left in the text; recognise its JIS X 0208 escapes first.
    if _ISO2022_JP_ESCAPES_RE.search(data):
        decoded = _strict_decode(data, "iso2022_jp_2")
        if decoded is not None:
            return decoded
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # A read cut at max_bytes may split the last UTF-8 character.
        if exc.reason == "unexpected end of data" and exc.end == len(data) and len(data) - exc.start < 4:
            return data[: exc.start].decode("utf-8-sig", errors="replace")
    # EUC-JP before Shift-JIS: Shift-JIS kana and punctuation (0x81-0x83 lead
    # bytes) are invalid EUC-JP, while EUC-JP text often passes as Shift-JIS
    # half-width katakana, and charset_normalizer reads it as EUC-KR Hangul.
    for encoding in ("euc_jp", "shift_jis", "cp932"):
        decoded = _strict_decode(data, encoding)
        if decoded is not None:
            return decoded
    from_bytes = _charset_from_bytes()
    if from_bytes is not None:
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    return data.decode("utf-8", errors="replace")


def read_subtitle_file(path: Path) -> str:
    """The whole text subtitle, decoded like ``decode_subtitle_bytes`` (raises OSError)."""
    return decode_subtitle_bytes(path.read_bytes())


# Embedded fonts and pictures of an ASS script (uuencoded, often megabytes,
# usually before [Events]): no subtitle text.
_ASS_ATTACHMENT_SECTIONS = frozenset({"[fonts]", "[graphics]", "[aegisub extradata]"})
_ASS_ATTACHMENT_HEADER_RE = re.compile(rb"(?im)^[ \t]*\[(?:fonts|graphics|aegisub extradata)\][ \t]*\r?$")
_ASS_FULL_READ_LIMIT = 64 * 1024 * 1024
_ASS_DIALOGUE_RE = re.compile(r"(?im)^[ \t]*dialogue:(.*)$")


def strip_ass_attachments(text: str) -> str:
    """``text`` without the [Fonts], [Graphics] and [Aegisub Extradata] sections."""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped.casefold() in _ASS_ATTACHMENT_SECTIONS
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _ass_dialogue_text(text: str) -> str | None:
    """The Text field of every Dialogue event, one screen line per line (None: not ASS)."""
    rows = _ASS_DIALOGUE_RE.findall(text)
    if not rows:
        return None
    lines: list[str] = []
    for row in rows:
        parts = row.split(",", 9)
        if len(parts) == 10:
            lines.extend(parts[9].replace("\\N", "\n").replace("\\n", "\n").splitlines())
    return "\n".join(lines)


def subtitle_text(path: Path, max_bytes: int = 2_000_000) -> str:
    is_ass = path.suffix.casefold() in {".ass", ".ssa"}
    try:
        with path.open("rb") as fh:
            data = fh.read(max_bytes)
            if is_ass and len(data) >= max_bytes and _ASS_ATTACHMENT_HEADER_RE.search(data):
                # Fonts fill the first megabytes: read on to the dialogue.
                data += fh.read(max(0, _ASS_FULL_READ_LIMIT - len(data)))
    except OSError:
        return ""
    text = decode_subtitle_bytes(data)
    return strip_ass_attachments(text) if is_ass else text


def strip_subtitle_markup(text: str) -> str:
    text = ASS_TAG_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = TIMESTAMP_RE.sub(" ", text)
    text = re.sub(r"(?im)^\s*(?:dialogue|comment):[^,]*(?:,[^,]*){8},", " ", text)
    text = re.sub(r"(?m)^\s*\d+\s*$", " ", text)
    return text


_KANA_CHAR_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def is_macos_metadata_path(name: str) -> bool:
    """A Finder metadata entry (``__MACOSX/`` or an AppleDouble ``._name``) from a zip made on a Mac."""
    parts = [part for part in str(name).replace("\\", "/").split("/") if part]
    return bool(parts) and (parts[-1].startswith("._") or any(part == "__MACOSX" for part in parts))


def japanese_text_metrics(text: str, *, min_kana: int = 10) -> dict[str, float | int | bool]:
    # ASS: only what is shown (Dialogue text), not style names, script info or fonts.
    dialogue = _ass_dialogue_text(strip_ass_attachments(text))
    if dialogue is not None:
        text = dialogue
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

    # Distinct lines with CJK text that contain kana. A Chinese subtitle with
    # Japanese OP/ED lyrics has plenty of kana overall but on about a tenth of
    # its lines; Japanese (and Japanese + Chinese bilingual) files have it on
    # most. Distinct: a kanji-only sign drawn in ten layers is one line.
    cjk_lines = {line.strip() for line in text.splitlines() if _CJK_CHAR_RE.search(line)}
    kana_lines = sum(1 for line in cjk_lines if _KANA_CHAR_RE.search(line))
    kana_line_ratio = kana_lines / max(len(cjk_lines), 1)

    # Kana is required to reject Chinese subtitles that contain only Han characters.
    detected = kana >= min_kana and kana_ratio >= 0.015 and japanese_ratio >= 0.08 and kana_line_ratio >= 0.20
    return {
        "detected": detected,
        "hiragana": hiragana,
        "katakana": katakana,
        "kanji": kanji,
        "kana_ratio": kana_ratio,
        "japanese_ratio": japanese_ratio,
        "kana_line_ratio": kana_line_ratio,
        "kana_lines": kana_lines,
    }


def is_japanese_subtitle(path: Path) -> bool:
    marker = has_japanese_marker(path.name)
    if has_negative_language_marker(path.name) and not marker:
        return False
    if is_macos_metadata_path(path.name):
        return False
    if path.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS:
        return marker
    # A ``.ja`` name only lowers the amount of text needed (any kana): the
    # content is still read, so a mis-decoded, Chinese or empty "ja" file is
    # not trusted.
    metrics = japanese_text_metrics(subtitle_text(path), min_kana=1 if marker else 10)
    if metrics["detected"]:
        return True
    if marker and ENGLISH_MARKER_RE.search(path.name):
        return (
            int(metrics["kana_lines"]) >= _BILINGUAL_MIN_KANA_LINES
            and float(metrics["kana_line_ratio"]) >= 0.20
        )
    return False
