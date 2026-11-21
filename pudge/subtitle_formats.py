from __future__ import annotations

import bisect
import hashlib
import html
import math
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

from .i18n import err
from .language import decode_subtitle_bytes, read_subtitle_file
from .fsutil import atomic_write_text


# Hours may have more than two digits and a time may be negative (a file shifted before
# 0 s): with ``\d{1,2}`` and no sign, "-00:00:01,000" read as +1 s and "100:00:01,000" lost
# its first digit or the whole cue.
_TIME_RE = re.compile(r"(?P<sign>-?)(?P<h>\d{1,4}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})")
_SRT_TIME = r"-?\d{1,4}:\d{2}:\d{2}[,.]\d{1,3}"
_TIMING_RE = re.compile(rf"(?<![\d:])(?P<start>{_SRT_TIME})\s*-->\s*(?P<end>{_SRT_TIME})")
# The last time ``srt_timestamp`` writes: what ``_TIME_RE`` reads back.
_MAX_SRT_MILLISECONDS = 9999 * 3_600_000 + 59 * 60_000 + 59_999
_ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")
# Strip actual HTML tags, but do not eat Japanese dialogue merely wrapped in
# ASCII angle brackets, such as ``<いつも朝が苦手だった>``.
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>", re.IGNORECASE)
# HTML ruby: the reading (<rt>, and the <rp> brackets around it) is not spoken text, the
# base is. Readings are removed one by one, so the base after a second reading stays
# (<ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby> read 漢 with one pattern per <ruby> element); an
# <rt> may end at the next <rt>, at </ruby> or at the end of its line (HTML allows it).
_RUBY_READING_RE = re.compile(
    r"<rp(?:[.\s][^>]*)?>.*?</rp>|<rt(?:[.\s][^>]*)?>.*?(?:</rt>|(?=<rt[.\s>])|(?=</ruby>)|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RUBY_TAG_RE = re.compile(r"</?(?:ruby|rb|rtc|rt|rp)(?:[.\s][^>]*)?>", re.IGNORECASE)
_KANA = r"ぁ-ゖァ-ヺーゝゞヽヾ"
_KANJI = r"一-龯々〆ヵヶ"
_FURIGANA_BASE = rf"([{_KANJI}][{_KANJI}{_KANA}・]*)"
_FURIGANA_READING = rf"[{_KANA}\s・]{{1,40}}"
_FURIGANA_PATTERNS = (
    re.compile(rf"[｜|]?{_FURIGANA_BASE}《{_FURIGANA_READING}》"),
    re.compile(rf"{_FURIGANA_BASE}（{_FURIGANA_READING}）"),
    re.compile(rf"{_FURIGANA_BASE}\({_FURIGANA_READING}\)"),
    re.compile(rf"{_FURIGANA_BASE}［{_FURIGANA_READING}］"),
    re.compile(rf"{_FURIGANA_BASE}\[{_FURIGANA_READING}\]"),
)
_SPEAKER_LABEL_RE = re.compile(r"^\s*[（(]([^（）()\r\n]{1,24})[）)]\s*(.*)$")
_ANGLE_BRACKET_TRANSLATION = str.maketrans({"<": "", ">": "", "＜": "", "＞": ""})
_STRAY_HANGUL_BEFORE_JAPANESE_RE = re.compile(r"(?m)^[\uac00-\ud7af](?=[\u3040-\u30ff\u3400-\u9fff])")
_MIN_PLAYBACK_CUE_GAP_SECONDS = 0.100
_MIN_TRIMMED_CUE_DURATION_SECONDS = 0.300
_MIN_SUBTITLE_START_SECONDS = 0.100
_SIMPLIFIED_CHINESE_HINTS = set(
    "这们还没吗为与听见说来过时会里对从后发头尽将让个门开关间无气学书车东业乐边变长处点电动国话画华万网现线压应张总导叶台号爱带办报宝贝笔毕标别产场称迟冲出传达单当党灯敌尔儿饭飞风该赶广归汉号合欢击际价见讲较节进经举据绝开课块来类礼离两临马买卖门难脑闹内农盘齐钱亲轻请让认扫声师实试书术树双说虽岁孙体条听厅头图团万为卫问无务习系戏县写兴须选严验阳样药业页义鱼语远云杂脏早战张只钟种众总组"
)
# Characters that are strong evidence of Simplified Chinese rather than
# ordinary Japanese shinjitai/kanji.  The broader set above is useful for
# corpus-level language profiling, but it is intentionally too permissive for
# deleting a *single* Han-only line from an otherwise Japanese cue.
_STRONG_SIMPLIFIED_CHINESE_HINTS = set(
    "这们还没吗为与听说过对从尽将让门关间无车东业乐边变长处点电动话华网现线压应张总爱带办报贝笔毕标别产场迟冲传敌尔儿饭飞该赶广归汉欢击际价见讲较节进经举据绝课块类离两临马买卖难脑闹农盘齐钱亲轻请认扫师实试书术树双虽岁孙厅头图团问务习戏县兴须选严验阳样药页义鱼语远云杂脏战钟种众组译词继续"
)


# ``&`` and ``+`` separate languages in tags such as ``[CHS&JPN]``.
_FILENAME_JAPANESE_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,&+])"
    r"(?:ja(?:\[cc\])?|jp|jpn|japanese|日本語|jpsc|jptc|scjp|tcjp)"
    r"(?:$|[\s._\-\[\]\),&+])"
    # 简日双语 / 繁日雙語: Chinese + Japanese in one file.
    r"|[简繁簡]日|中日[双雙]语|中日[双雙]語"
)
_FILENAME_CHINESE_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,&+])"
    r"(?:chs|cht|chi|zho|zh(?:[-_](?:cn|tw|hans|hant))?|chinese|中文|jpsc|jptc|scjp|tcjp)"
    r"(?:$|[\s._\-\[\]\),&+])"
    r"|简中|繁中|簡中|简体|繁体|簡體|繁體|[简繁簡]日|中日[双雙]语|中日[双雙]語|[双雙]语|[双雙]語"
)
# SC / TC / GB / BIG5 are also group names ("[SC-Raws]") or other words: they mark
# Chinese only as a tag of their own in brackets ("[TC]", not GB alone, which is
# also a size or a country) or beside another language tag ("[SC&JPN]").
_FILENAME_BRACKET_GROUP_RE = re.compile(r"[\[\(]([^\[\]\(\)]*)[\]\)]")
_FILENAME_TAG_SPLIT_RE = re.compile(r"[\s&+,_\-/.]+")
_AMBIGUOUS_CHINESE_TAGS = frozenset({"sc", "tc", "gb", "big5"})
_LANGUAGE_TAGS = frozenset(
    {
        "ja",
        "jp",
        "jpn",
        "japanese",
        "chs",
        "cht",
        "chi",
        "zho",
        "zh",
        "chinese",
        "en",
        "eng",
        "english",
        "日本語",
        "中文",
    }
)


# Japanese + English in one file. Only a subtitle-language suffix right before the extension
# is trusted ("03.jp-en.ass", ".en.ja.srt"): elsewhere "JPN, ENG" / "JPN.ENG" / "en-dub" name a
# dual-audio release, not the subtitle text. A combined tag anywhere else counts only when the
# lines confirm it (``bilingual_english_profile``).
_JAPANESE_TAGS = frozenset({"ja", "jp", "jpn", "japanese"})
_ENGLISH_TAGS = frozenset({"en", "eng", "english"})
_FILENAME_JAPANESE_ENGLISH_SUFFIX_RE = re.compile(
    r"(?i)[._\-](?:(?:ja|jp|jpn)[._\-+&](?:en|eng)|(?:en|eng)[._\-+&](?:ja|jp|jpn))\.(?:ass|ssa|srt|vtt)$"
)
_FILENAME_JAPANESE_ENGLISH_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,&+])"
    r"(?:(?:ja|jp|jpn|japanese)[\s._\-&+,]+(?:en|eng|english)"
    r"|(?:en|eng|english)[\s._\-&+,]+(?:ja|jp|jpn|japanese))"
    r"(?:$|[\s._\-\[\]\),&+])"
)


def _japanese_english_suffix(name: str) -> bool:
    return bool(_FILENAME_JAPANESE_ENGLISH_SUFFIX_RE.search(name))


def _japanese_english_tag_elsewhere(name: str) -> bool:
    """A combined tag outside the suffix: a hint the lines must confirm."""
    if _FILENAME_JAPANESE_ENGLISH_RE.search(name):
        return True
    for group in _FILENAME_BRACKET_GROUP_RE.findall(name):
        tokens = {token for token in _FILENAME_TAG_SPLIT_RE.split(group.casefold()) if token}
        if tokens & _JAPANESE_TAGS and tokens & _ENGLISH_TAGS:
            return True
    return False


def _ambiguous_chinese_tag(name: str, *, japanese_marker: bool) -> bool:
    for group in _FILENAME_BRACKET_GROUP_RE.findall(name):
        tokens = {token for token in _FILENAME_TAG_SPLIT_RE.split(group.casefold()) if token}
        ambiguous = tokens & _AMBIGUOUS_CHINESE_TAGS
        if not ambiguous:
            continue
        if tokens & _LANGUAGE_TAGS:
            return True
        if tokens == ambiguous and not japanese_marker and ambiguous != {"gb"}:
            return True
    return False


# Word edges are "not a letter or digit": ``\b`` takes "_" for a letter, so
# "Show_01_AI_Translated.srt" and "Show_MTL_01.srt" were not seen.
_MACHINE_TRANSLATED_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9])AI[\s_.-]*(?:translat[A-Za-z]*|TL)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])machine[\s_.-]*translat"
    r"|(?<![A-Za-z0-9])MTL(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])translated[\s_.-]+from[\s_.-]+(?:english|eng|chinese|chs|cht)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:google|deepl|chat[\s_.-]*gpt|gpt)[\s_.-]*(?:translat[A-Za-z]*|TL)(?![A-Za-z0-9])"
    r"|機械翻訳|AI翻訳|自動翻訳",
    re.IGNORECASE,
)


def subtitle_name_is_machine_translated(filename: str | Path) -> bool:
    """True when a subtitle's name says it was machine-translated into Japanese.

    "[Judas] Dorohedoro - S01E11 [JPN] (AI Translated From English).ass" is Japanese
    text, times perfectly, and reads like a translation, not the episode's lines.
    """
    return bool(_MACHINE_TRANSLATED_NAME_RE.search(Path(str(filename)).name))


def subtitle_filename_language_profile(filename: str | Path) -> dict[str, object]:
    """Classify explicit Japanese/Chinese/English language markers in a subtitle name."""
    name = Path(filename).name
    japanese = bool(_FILENAME_JAPANESE_MARKER_RE.search(name))
    chinese = bool(_FILENAME_CHINESE_MARKER_RE.search(name)) or _ambiguous_chinese_tag(
        name, japanese_marker=japanese
    )
    english = bool(japanese and not chinese and _japanese_english_suffix(name))
    english_hint = bool(japanese and not chinese and not english and _japanese_english_tag_elsewhere(name))
    if japanese and chinese:
        purity = "mixed_japanese_chinese"
        priority = 0
    elif english:
        # The viewer would read English beside the Japanese: a fallback like JP+CN files.
        purity = "mixed_japanese_english"
        priority = 0
    elif japanese:
        purity = "japanese_only"
        priority = 2
    elif chinese:
        purity = "chinese_only"
        priority = 0
    else:
        purity = "unknown"
        priority = 1
    return {
        "purity": purity,
        "priority": priority,
        "japanese_marker": japanese,
        "chinese_marker": chinese,
        "english_marker": english,
        "english_tag_unconfirmed": english_hint,
    }


def format_preference_bonus(filename: str | Path, prefer_srt: bool = True) -> float:
    """Format-quality prior: native SRT is safer than post-processed ASS/SSA."""
    if not prefer_srt:
        return 0.0
    suffix = Path(filename).suffix.casefold()
    return {
        ".srt": 16.0,
        ".ass": 6.0,
        ".ssa": 5.0,
        ".vtt": 3.0,
        ".sup": 0.0,
    }.get(suffix, 0.0)


def _timestamp_to_seconds(value: str, *, signed: bool = False) -> float:
    """Seconds of ``H:MM:SS,mmm``; a negative time only with ``signed`` (SRT), else invalid."""
    match = _TIME_RE.fullmatch(value.strip())
    if not match or (match.group("sign") and not signed):
        raise ValueError(err("subtitles.invalidTimestamp", value=value))
    milliseconds = match.group("ms").ljust(3, "0")[:3]
    seconds = (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(milliseconds) / 1000.0
    )
    return -seconds if match.group("sign") else seconds


def srt_timestamp(value: float) -> str:
    """``HH:MM:SS,mmm`` of a time in seconds (rounded to the millisecond, never negative,
    never past what ``parse_srt`` reads back: 9999 hours)."""
    total_ms = min(_MAX_SRT_MILLISECONDS, max(0, int(round(float(value) * 1000))))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


_seconds_to_timestamp = srt_timestamp


def _remove_embedded_furigana(value: str) -> str:
    if "<r" in value or "<R" in value:
        value = _RUBY_TAG_RE.sub("", _RUBY_READING_RE.sub("", value))
    for pattern in _FURIGANA_PATTERNS:
        value = pattern.sub(r"\1", value)
    return value


def _remove_leading_speaker_labels(value: str) -> str:
    """Remove parenthesized speaker metadata attached to actual dialogue.

    A standalone final cue such as ``（ドアが開く）`` is preserved. A label on
    its own line before dialogue, or a label directly followed by dialogue, is
    removed: ``（ミオ）青木さん`` -> ``青木さん``.
    """
    lines = value.splitlines()
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        match = _SPEAKER_LABEL_RE.match(line)
        if match is None:
            cleaned.append(line)
            continue

        remainder = match.group(2).strip()
        if remainder:
            cleaned.append(remainder)
            continue

        if any(next_line.strip() for next_line in lines[index + 1 :]):
            continue

        cleaned.append(line)
    return "\n".join(cleaned)


_DRAWING_COMMANDS = frozenset("mnlbspc")
_DRAWING_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_JAPANESE_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff66-\uff9f]")
# A line that is only a release/encode credit (``BDRip.....Group``), never dialogue.
_RELEASE_CREDIT_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:bd-?rip|bdmv|web-?rip|web-?dl|dvd-?rip|hdtv-?rip|vcb-?studio|x26[45]|hevc|hi10p)"
    r"(?![a-z0-9])|^\s*encod(?:ed|er|ing)\s*(?:by\b|:)"
)


def _is_ass_drawing_line(line: str) -> bool:
    """``m 0 0 l 670 0 l 670 60 l 0 60``: ASS vector drawing commands shown as text."""
    tokens = line.split()
    if len(tokens) < 5 or tokens[0] != "m":
        return False
    commands = numbers = 0
    for token in tokens:
        if token in _DRAWING_COMMANDS:
            commands += 1
        elif _DRAWING_NUMBER_RE.fullmatch(token):
            numbers += 1
        else:
            return False
    return commands >= 2 and numbers >= 4


def is_non_dialogue_line(line: str) -> bool:
    """A subtitle line that is a vector drawing or a release credit, not dialogue."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("m ") and _is_ass_drawing_line(stripped):
        return True
    return not _JAPANESE_CHAR_RE.search(stripped) and bool(_RELEASE_CREDIT_RE.search(stripped))


def plain_subtitle_text(value: str) -> str:
    """Normalize subtitle payload for stable plain-text rendering in mpv."""
    value = value.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    value = html.unescape(value)
    value = _ASS_OVERRIDE_RE.sub("", value)
    value = _remove_embedded_furigana(value)
    value = _HTML_TAG_RE.sub("", value)
    value = value.translate(_ANGLE_BRACKET_TRANSLATION)
    value = _remove_leading_speaker_labels(value)
    value = value.replace("\ufeff", "")
    # Some Japanese broadcast captions contain a single corrupted Hangul glyph
    # immediately before otherwise valid Japanese text (for example
    # ``모大きな船``). Remove only that narrow artefact and leave legitimate
    # Korean lines untouched.
    value = _STRAY_HANGUL_BEFORE_JAPANESE_RE.sub("", value)
    lines = [line.strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line and not is_non_dialogue_line(line)).strip()


def merge_layered_cues(
    cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], int]:
    """(cues, events merged): overlapping events with one visible text become one cue.

    Karaoke and lyric-box layers draw the same line several times at once (glow,
    border, highlight); as plain text each layer became its own stacked cue."""
    open_by_text: dict[str, int] = {}
    out: list[list[object]] = []
    merged = 0
    for start, end, text in sorted(cues, key=lambda cue: (float(cue[0]), float(cue[1]))):
        visible = re.sub(r"\s+", "", re.sub(r"<[^>]*>|\{[^}]*\}", "", str(text or "")))
        # A line, not a letter or a syllable of a sign (those are fragments, kept apart).
        visible = visible if len(visible) >= 4 else ""
        index = open_by_text.get(visible) if visible else None
        if index is not None and float(start) < float(out[index][1]):
            out[index][1] = max(float(out[index][1]), float(end))
            merged += 1
            continue
        if visible:
            open_by_text[visible] = len(out)
        out.append([start, end, text])
    if not merged:
        return cues, 0
    return [(row[0], row[1], row[2]) for row in out], merged  # type: ignore[misc]


# Subtitle preparation parses the same few files dozens of times per episode.
# Cues are a pure function of the text, so cache by the text itself (never
# stale, unlike a path/mtime key) and hand every caller its own list.
_SRT_CUE_CACHE: OrderedDict[str, tuple[tuple[float, float, str], ...]] = OrderedDict()
_SRT_CUE_CACHE_LIMIT = 32
_SRT_CUE_CACHE_LOCK = threading.Lock()


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    text = read_subtitle_file(path)
    with _SRT_CUE_CACHE_LOCK:
        cached = _SRT_CUE_CACHE.get(text)
        if cached is not None:
            _SRT_CUE_CACHE.move_to_end(text)
            return list(cached)
    cues = _parse_srt_text(text)
    with _SRT_CUE_CACHE_LOCK:
        _SRT_CUE_CACHE[text] = tuple(cues)
        while len(_SRT_CUE_CACHE) > _SRT_CUE_CACHE_LIMIT:
            _SRT_CUE_CACHE.popitem(last=False)
    return cues


def _parse_srt_text(text: str) -> list[tuple[float, float, str]]:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues: list[tuple[float, float, str]] = []
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = _TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        # A missing blank line merged the next cue into this block (and put its
        # timing line on screen): every later timing line starts a new cue.
        starts = [timing_index] + [
            index for index in range(timing_index + 1, len(lines)) if _TIMING_RE.match(lines[index].strip())
        ]
        for position, line_index in enumerate(starts):
            match = _TIMING_RE.search(lines[line_index])
            stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
            # The next cue's bare number sits right before its timing line.
            if stop < len(lines) and stop - 1 > line_index and lines[stop - 1].strip().isdigit():
                stop -= 1
            try:
                start = _timestamp_to_seconds(match.group("start"), signed=True)
                end = _timestamp_to_seconds(match.group("end"), signed=True)
            except ValueError:
                continue
            # A line shifted before the start of the video begins there; one that also
            # ends before it is not in the video.
            start = max(0.0, start)
            if end <= start:
                continue
            payload = plain_subtitle_text("\n".join(lines[line_index + 1 : stop]))
            if payload:
                cues.append((start, end, payload))
    return cues


def _cue_script_kind(text: str) -> str:
    has_kana = any("\u3040" <= ch <= "\u30ff" for ch in text)
    has_han = any(("\u3400" <= ch <= "\u4dbf") or ("\u4e00" <= ch <= "\u9fff") for ch in text)
    if has_kana:
        return "japanese"
    if has_han:
        return "han_only"
    return "other"


def _parallel_han_cue_indices(
    cues: list[tuple[float, float, str]],
    kinds: list[str] | None = None,
    *,
    timestamp_tolerance: float = 0.180,
) -> set[int]:
    """Find Han-only cues sharing essentially the same interval with Japanese cues."""
    resolved_kinds = kinds or [_cue_script_kind(text) for _start, _end, text in cues]
    japanese = sorted(
        [
            (index, start, end)
            for index, ((start, end, _text), kind) in enumerate(zip(cues, resolved_kinds))
            if kind == "japanese"
        ],
        key=lambda item: (item[1], item[2]),
    )
    japanese_starts = [item[1] for item in japanese]
    longest = max((item[2] - item[1] for item in japanese), default=0.0)
    paired: set[int] = set()
    for index, ((start, end, _text), kind) in enumerate(zip(cues, resolved_kinds)):
        if kind != "han_only":
            continue
        duration = max(end - start, 0.001)
        # Japanese cues that start this early end before ``start - tolerance``: the loop
        # below skips them anyway (1 s of margin), so a long file is not scanned per cue.
        first = bisect.bisect_left(japanese_starts, start - timestamp_tolerance - max(longest, 0.0) - 1.0)
        for _japanese_index, japanese_start, japanese_end in japanese[first:]:
            if japanese_start > end + timestamp_tolerance:
                break
            if japanese_end < start - timestamp_tolerance:
                continue
            same_interval = (
                abs(start - japanese_start) <= timestamp_tolerance
                and abs(end - japanese_end) <= timestamp_tolerance
            )
            japanese_duration = max(japanese_end - japanese_start, 0.001)
            overlap = max(0.0, min(end, japanese_end) - max(start, japanese_start))
            overlap_ratio = overlap / max(min(duration, japanese_duration), 0.001)
            near_parallel = bool(
                abs(start - japanese_start) <= 0.350
                and abs(end - japanese_end) <= 0.350
                and overlap_ratio >= 0.90
            )
            if same_interval or near_parallel:
                paired.add(index)
                break
    return paired


def bilingual_cjk_profile(
    cues: list[tuple[float, float, str]],
) -> dict[str, object]:
    kinds = [_cue_script_kind(text) for _start, _end, text in cues]
    japanese = sum(kind == "japanese" for kind in kinds)
    han_only = sum(kind == "han_only" for kind in kinds)
    short_han = sum(
        kind == "han_only" and end - start <= 0.45 for (start, end, _text), kind in zip(cues, kinds)
    )
    transitions = sum(
        left != right and {left, right} == {"japanese", "han_only"} for left, right in zip(kinds, kinds[1:])
    )
    parallel_han = len(_parallel_han_cue_indices(cues, kinds))
    relevant = japanese + han_only
    transition_ratio = transitions / max(relevant - 1, 1)
    short_han_ratio = short_han / max(han_only, 1)
    parallel_han_ratio = parallel_han / max(han_only, 1)
    suspected = bool(
        japanese >= 20
        and han_only >= 20
        and ((short_han_ratio >= 0.50 and transition_ratio >= 0.35) or parallel_han_ratio >= 0.50)
    )
    return {
        "suspected_bilingual_cjk": suspected,
        "japanese_cues": japanese,
        "han_only_cues": han_only,
        "short_han_cues": short_han,
        "short_han_ratio": round(short_han_ratio, 4),
        "parallel_han_cues": parallel_han,
        "parallel_han_ratio": round(parallel_han_ratio, 4),
        "alternating_ratio": round(transition_ratio, 4),
    }


_LATIN_WORD_RE = re.compile(r"[A-Za-z']{2,}")
# Words an English line nearly always has and romaji lyrics do not ("to", "no", "a", "de",
# "he", "me", "so" are left out: they are romaji too).
_ENGLISH_FUNCTION_WORDS = frozenset(
    {
        "the", "you", "is", "are", "was", "were", "what", "this", "that", "it's", "i'm", "don't",
        "can't", "your", "we", "they", "she", "and", "of", "for", "with", "have", "not", "it",
        "just", "but", "there", "here", "why", "how", "who", "will", "would", "be", "i",
    }
)  # fmt: skip
_SONG_STYLE_RE = re.compile(
    r"(?i)(?:^|[^a-z])(?:op|ed|song|songs|kara|karaoke|lyric|lyrics|romaji|insert|kfx)(?:[^a-z]|$)"
)
_KARAOKE_TAG_RE = re.compile(r"(?i)\\k[fo]?\d")


def _english_line(text: str) -> bool:
    if _cue_script_kind(text) != "other":
        return False
    words = [word.casefold() for word in _LATIN_WORD_RE.findall(text)] + [
        word.casefold() for word in re.findall(r"\b[Ii]\b", text)
    ]
    return len(words) >= 2 and any(word in _ENGLISH_FUNCTION_WORDS for word in words)


def bilingual_english_profile(
    cues: list[tuple[float, float, str]] | list[tuple[float, float, str, bool]],
) -> dict[str, object]:
    """Japanese and English lines shown together (a JP+EN fansub file under any name).

    An English line has no kana or kanji, two Latin words and an English function word;
    it is parallel when it shares at least half of its time with a Japanese line. Song
    lines (``\\k`` karaoke, ``♪``, OP/ED/song/karaoke styles) never count: a short file of
    40 Japanese lines and 20 romaji lyric lines is not bilingual. At least 40 parallel
    English lines, and half as many as Japanese ones, are needed.
    """
    rows = [(cue[0], cue[1], cue[2], bool(cue[3]) if len(cue) > 3 else False) for cue in cues]
    japanese = sorted(
        (start, end) for start, end, text, _song in rows if _cue_script_kind(text) == "japanese"
    )
    english = [
        (start, end)
        for start, end, text, song in rows
        if not song and "♪" not in text and end > start and _english_line(text)
    ]
    starts = [start for start, _end in japanese]
    parallel = 0
    for start, end in english:
        position = bisect.bisect_right(starts, end)
        for japanese_start, japanese_end in japanese[max(0, position - 8) : position]:
            overlap = min(end, japanese_end) - max(start, japanese_start)
            if overlap >= 0.5 * min(end - start, max(japanese_end - japanese_start, 0.001)):
                parallel += 1
                break
    suspected = bool(len(japanese) >= 20 and parallel >= 40 and parallel >= 0.5 * len(japanese))
    return {
        "suspected_bilingual_english": suspected,
        "japanese_cues": len(japanese),
        "english_cues": len(english),
        "parallel_english_cues": parallel,
    }


def english_lines_removed(
    cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], int]:
    """The cues without their English lines (``_english_line``; lines inside one cue too)."""
    kept: list[tuple[float, float, str]] = []
    removed = 0
    for start, end, text in cues:
        lines = [line for line in str(text).splitlines() if line.strip()]
        japanese = [line for line in lines if not _english_line(line)]
        removed += len(lines) - len(japanese)
        if japanese:
            kept.append((start, end, "\n".join(japanese)))
    return kept, removed


def _profile_cues(path: Path) -> list[tuple[float, float, str, bool]] | None:
    """(start, end, plain text, song line) of a text subtitle; None when unreadable."""
    suffix = path.suffix.casefold()
    if suffix == ".srt":
        try:
            return [(start, end, text, "♪" in text) for start, end, text in parse_srt(path)]
        except OSError:
            return None
    if suffix not in {".ass", ".ssa"}:
        return None
    try:
        text = read_subtitle_file(path)
    except OSError:
        return None
    cues: list[tuple[float, float, str, bool]] = []
    for line in text.splitlines():
        if not line.casefold().startswith("dialogue:"):
            continue
        parts = line.split(":", 1)[1].lstrip().split(",", 9)
        if len(parts) < 10:
            continue
        try:
            start = _timestamp_to_seconds(parts[1])
            end = _timestamp_to_seconds(parts[2])
        except ValueError:
            continue
        payload = plain_subtitle_text(parts[9])
        song = bool(
            _KARAOKE_TAG_RE.search(parts[9])
            or _SONG_STYLE_RE.search(parts[3])
            or "karaoke" in parts[8].casefold()
            or "♪" in payload
        )
        if payload and end > start:
            cues.append((start, end, payload, song))
    return cues


def subtitle_language_profiles(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    """(bilingual CJK profile, bilingual English profile) from one read of the file."""
    rows = _profile_cues(path)
    if rows is None:
        return {"suspected_bilingual_cjk": False}, {"suspected_bilingual_english": False}
    return bilingual_cjk_profile([(start, end, text) for start, end, text, _song in rows]), (
        bilingual_english_profile(rows)
    )


def subtitle_bilingual_cjk_profile(path: Path) -> dict[str, object]:
    return subtitle_language_profiles(path)[0]


def subtitle_bilingual_english_profile(path: Path) -> dict[str, object]:
    return subtitle_language_profiles(path)[1]


def _filter_inline_chinese_lines(
    cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], int]:
    """Drop a Chinese translation line embedded in the same subtitle cue.

    Some JP+CN releases store Japanese and Chinese as two lines inside one ASS
    Dialogue/SRT cue. Cue-level script classification then sees kana and treats
    the whole cue as Japanese, so the old parallel-cue filter cannot remove the
    Chinese half. Keep short kanji-only Japanese fragments conservatively.
    """
    # A normal Japanese subtitle can legitimately contain a kanji-only line
    # next to a kana-containing line (e.g. 出発前 / 大佐殿が…).  Do not
    # infer "Chinese translation" from line length alone.  Traditional JP+CN
    # files without simplified-character hints are still detectable when this
    # mixed-line shape is a consistent file-wide pattern rather than an
    # occasional Japanese construction.
    multiline_cues = 0
    mixed_line_cues = 0
    for _start, _end, text in cues:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        multiline_cues += 1
        kinds = [_cue_script_kind(line) for line in lines]
        if "japanese" in kinds and "han_only" in kinds:
            mixed_line_cues += 1
    consistent_inline_bilingual = bool(mixed_line_cues >= 2 and mixed_line_cues / max(len(cues), 1) >= 0.25)

    filtered: list[tuple[float, float, str]] = []
    removed = 0
    for start, end, text in cues:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if len(lines) < 2:
            filtered.append((start, end, text))
            continue
        kinds = [_cue_script_kind(line) for line in lines]
        has_japanese = any(kind == "japanese" for kind in kinds)
        if not has_japanese or not any(kind == "han_only" for kind in kinds):
            filtered.append((start, end, text))
            continue
        kept: list[str] = []
        for line, kind in zip(lines, kinds):
            if kind != "han_only":
                kept.append(line)
                continue
            compact = re.sub(r"[^\u3400-\u4dbf\u4e00-\u9fff]", "", line)
            strong_simplified_hints = sum(ch in _STRONG_SIMPLIFIED_CHINESE_HINTS for ch in compact)
            likely_translation = bool(consistent_inline_bilingual or strong_simplified_hints >= 2)
            if likely_translation:
                removed += 1
                continue
            kept.append(line)
        cleaned = "\n".join(kept).strip()
        if cleaned:
            filtered.append((start, end, cleaned))
    return filtered, removed


def _filter_parallel_chinese_cues(
    cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], dict[str, object]]:
    cues, inline_removed = _filter_inline_chinese_lines(cues)
    kinds = [_cue_script_kind(text) for _start, _end, text in cues]
    paired_han_indices = _parallel_han_cue_indices(cues, kinds)
    profile = bilingual_cjk_profile(cues)
    if not profile.get("suspected_bilingual_cjk"):
        profile = dict(profile)
        profile["removed_inline_chinese_lines"] = inline_removed
        profile["suspected_bilingual_cjk"] = bool(inline_removed)
        return cues, profile

    filtered: list[tuple[float, float, str]] = []
    removed = 0
    for index, ((start, end, text), kind) in enumerate(zip(cues, kinds)):
        if kind != "han_only":
            filtered.append((start, end, text))
            continue
        compact = re.sub(r"[^\u3400-\u4dbf\u4e00-\u9fff]", "", text)
        duration = end - start
        has_simplified_hint = any(ch in _SIMPLIFIED_CHINESE_HINTS for ch in compact)
        # A Han-only line sharing its interval with a kana-containing line is the
        # parallel Chinese track. Remove it regardless of duration, while keeping
        # unpaired short kanji-only Japanese cues such as 私, 本当 or 大丈夫.
        translated = bool(
            index in paired_han_indices or duration <= 0.45 or len(compact) > 4 or has_simplified_hint
        )
        if translated:
            removed += 1
            continue
        filtered.append((start, end, text))

    profile = dict(profile)
    profile["removed_han_only_cues"] = removed
    profile["removed_inline_chinese_lines"] = inline_removed
    profile["remaining_cues"] = len(filtered)
    profile["suspected_bilingual_cjk"] = bool(profile.get("suspected_bilingual_cjk") or inline_removed)
    return filtered, profile


def _merge_parallel_cues(
    cues: list[tuple[float, float, str]],
    *,
    timestamp_tolerance: float = 0.120,
) -> tuple[list[tuple[float, float, str]], int]:
    """Merge ASS lines that were meant to be displayed at the same time.

    Broadcast-caption ASS files often encode one visible subtitle as several
    positioned ``Dialogue`` events with identical timestamps. Plain SRT cannot
    preserve those positions. Serialising the events makes the second and later
    lines appear one or two seconds late, so keep the shared interval and join
    the text into one multiline cue instead. Genuine partial overlaps, where
    either boundary differs materially, remain separate.
    """
    if len(cues) < 2:
        return list(cues), 0

    merged: list[tuple[float, float, str]] = []
    merged_count = 0
    for raw_start, raw_end, raw_text in cues:
        start = float(raw_start)
        end = float(raw_end)
        text = plain_subtitle_text(raw_text)
        if not text or end <= start:
            continue

        if merged:
            previous_start, previous_end, previous_text = merged[-1]
            same_interval = (
                abs(start - previous_start) <= timestamp_tolerance
                and abs(end - previous_end) <= timestamp_tolerance
            )
            if same_interval:
                lines = [line for line in previous_text.splitlines() if line.strip()]
                known = {line.strip() for line in lines}
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and stripped not in known:
                        lines.append(stripped)
                        known.add(stripped)
                merged[-1] = (
                    min(previous_start, start),
                    max(previous_end, end),
                    "\n".join(lines),
                )
                merged_count += 1
                continue

        merged.append((start, end, text))

    return merged, merged_count


def _separate_touching_cues(
    cues: list[tuple[float, float, str]],
    *,
    minimum_gap: float = _MIN_PLAYBACK_CUE_GAP_SECONDS,
    minimum_trimmed_duration: float = _MIN_TRIMMED_CUE_DURATION_SECONDS,
    minimum_start: float = _MIN_SUBTITLE_START_SECONDS,
    preserve_order: bool = False,
    preserve_overlaps: bool = False,
) -> list[tuple[float, float, str]]:
    """Normalize cue timing while keeping intentional simultaneous dialogue.

    By default cues are sorted for malformed third-party files. Piecewise
    retiming passes ``preserve_order=True``: dialogue order is then sacred and
    must never be changed merely to make timestamps look chronological.

    Playback/alignment writers may pass ``preserve_overlaps=True`` so genuine
    overlaps remain simultaneous. Exact/near-exact hand-offs are still separated
    by the small safety gap to prevent a previous line lingering for one frame.

    Every emitted cue starts after zero and lasts at least 300 ms.
    """
    ordered = list(cues) if preserve_order else sorted(cues, key=lambda cue: (cue[0], cue[1]))
    separated: list[list[float | str]] = []
    for raw_start, raw_end, cue_text in ordered:
        original_duration = max(0.0, float(raw_end) - float(raw_start))
        start = max(float(minimum_start), float(raw_start))
        end = max(float(raw_end), start + max(minimum_trimmed_duration, original_duration))

        if separated:
            previous_start = float(separated[-1][0])
            previous_end = float(separated[-1][1])
            # Default writers sort cues by source start. If collision handling
            # moved the previous cue forward, never let a later sorted cue
            # fall back behind it: that would serialize non-monotonic SRT
            # starts even though the input starts were monotonic. Preserve-order
            # retiming is exempt because caller order is intentionally sacred.
            if not preserve_order and start < previous_start:
                shift = previous_start - start
                start = previous_start
                end = max(end + shift, start + minimum_trimmed_duration)
            gap = start - previous_end
            genuine_overlap = gap < -0.001
            if gap < minimum_gap and not (preserve_overlaps and genuine_overlap):
                adjusted_previous_end = start - minimum_gap
                if adjusted_previous_end - previous_start >= minimum_trimmed_duration:
                    separated[-1][1] = adjusted_previous_end
                else:
                    start = previous_end + minimum_gap
                    end = max(end, start + minimum_trimmed_duration)

        if end - start < minimum_trimmed_duration:
            end = start + minimum_trimmed_duration
        separated.append([start, end, cue_text])

    return [(float(start), float(end), str(cue_text)) for start, end, cue_text in separated]


def write_srt(
    cues: list[tuple[float, float, str]],
    path: Path,
    *,
    preserve_order: bool = False,
    preserve_overlaps: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    normalized = _separate_touching_cues(
        cues, preserve_order=preserve_order, preserve_overlaps=preserve_overlaps
    )
    for start, end, text in normalized:
        payload = plain_subtitle_text(text)
        if not payload or end <= start:
            continue
        # Numbered as written: a left-out cue (no text after the cleanup) left a gap.
        blocks.append(
            f"{len(blocks) + 1}\n{_seconds_to_timestamp(start)} --> {_seconds_to_timestamp(end)}\n{payload}"
        )
    # Atomic: callers treat an existing output as a ready cache, so an interrupted
    # write must never leave a truncated file at *path*. The usual file mode (the
    # umask): the file can sit next to the video, where a media server running as
    # another account reads it.
    atomic_write_text(path, "\n\n".join(blocks) + ("\n" if blocks else ""))
    return path


def write_exact_srt(cues: Iterable[tuple[float, float, str]], path: Path) -> Path:
    """Write cues exactly as given: no gap or overlap normalization, no text cleanup
    (timing references, repaired alignments, OCR results). A cue without text or
    without duration is left out; numbering counts every cue. Atomic, like write_srt."""
    blocks = [
        f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(cues, start=1)
        if text and end > start
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "\n\n".join(blocks) + ("\n" if blocks else ""))
    return path


def _cached_srt_ready(path: Path) -> bool:
    """A cached SRT is reusable only when it still parses to real cues."""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        return bool(parse_srt(path))
    except (OSError, ValueError):
        return False


def srt_has_non_dialogue_lines(subtitle: Path) -> bool:
    """Whether an SRT shows a vector drawing or a release credit as text (prepared
    before those were removed); False when unreadable."""
    try:
        text = read_subtitle_file(subtitle)
    except OSError:
        return False
    for raw_line in text.splitlines():
        line = _HTML_TAG_RE.sub("", _ASS_OVERRIDE_RE.sub("", raw_line)).strip()
        if not line or "-->" in line or line.isdigit():
            continue
        if is_non_dialogue_line(line):
            return True
    return False


def _without_non_dialogue_lines(subtitle: Path, output_dir: Path) -> Path | None:
    """A copy of an already clean SRT without drawing/credit lines (timing kept
    exactly; layered duplicates the drawing hid are merged); None when ``subtitle``
    has none or nothing would be left."""
    if not srt_has_non_dialogue_lines(subtitle):
        return None
    try:
        stat = subtitle.stat()
    except OSError:
        return None
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-v16-nondialogue-v1".encode()
    ).hexdigest()[:20]
    output = output_dir / f"v16-{digest}.srt"
    if _cached_srt_ready(output):
        return output
    cues, _merged = merge_layered_cues(parse_srt(subtitle))
    if not cues:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    write_exact_srt(cues, output)
    return output


def _playback_srt_output(subtitle: Path, output_dir: Path) -> Path:
    """Where clean_srt_for_playback puts the cleaned copy of ``subtitle`` (named by its stat)."""
    stat = subtitle.stat()
    generation = "playback-srt-v16"
    try:
        subtitle.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        # Cleaned before as mojibake: a Shift-JIS/UTF-16 source gets a new key
        # (UTF-8 keeps its key, which the library rebuilds for old OCR rows).
        generation += "-decoded"
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{generation}".encode()
    ).hexdigest()[:20]
    return output_dir / f"v16-{digest}.srt"


def is_playback_copy_of(played: Path | str, source: Path | str, cache_dir: Path) -> bool:
    """``played`` is ``source`` itself or its cleaned playback copy (clean_srt_for_playback)."""
    try:
        played_path = Path(played).expanduser().resolve()
        source_path = Path(source).expanduser()
        if played_path == source_path.resolve():
            return True
        if source_path.suffix.casefold() != ".srt" or not source_path.is_file():
            return False
        output_dir = (Path(cache_dir) / "playback-srt").expanduser()
        return played_path == _playback_srt_output(source_path, output_dir).resolve()
    except OSError:
        return False


def clean_srt_for_playback(
    subtitle: Path,
    cache_dir: Path,
    *,
    force: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Create a cached, normalized SRT used by mpv without touching source."""
    if subtitle.suffix.casefold() != ".srt":
        return subtitle, {"reason": "not_srt", "cleaned": False}
    if not subtitle.is_file():
        return subtitle, {"reason": "missing", "cleaned": False}

    output_dir = (cache_dir / "playback-srt").expanduser()
    try:
        subtitle.resolve().relative_to(output_dir.resolve())
        if subtitle.name.startswith("v16-"):
            stripped = _without_non_dialogue_lines(subtitle, output_dir)
            if stripped is not None:
                return stripped, {"reason": "non_dialogue_removed", "cleaned": True, "output": str(stripped)}
            return subtitle, {"reason": "already_clean", "cleaned": False}
    except ValueError:
        pass

    output = _playback_srt_output(subtitle, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if force:
        output.unlink(missing_ok=True)
    if _cached_srt_ready(output):
        stripped = _without_non_dialogue_lines(output, output_dir)
        if stripped is not None:
            return stripped, {"reason": "non_dialogue_removed", "cleaned": True, "output": str(stripped)}
        return output, {"reason": "cached", "cleaned": True, "output": str(output)}
    output.unlink(missing_ok=True)

    cues = parse_srt(subtitle)
    if not cues:
        return subtitle, {"reason": "no_valid_cues", "cleaned": False}

    cues, bilingual_profile = _filter_parallel_chinese_cues(cues)
    if not cues:
        return subtitle, {"reason": "no_japanese_cues_after_bilingual_filter", "cleaned": False}
    if srt_has_non_dialogue_lines(subtitle):
        # The drawing hid a lyric that another layer shows too (box + text layers).
        cues, _merged = merge_layered_cues(cues)

    conflict_count = sum(
        1
        for previous, current in zip(cues, cues[1:])
        if current[0] - previous[1] < _MIN_PLAYBACK_CUE_GAP_SECONDS
    )
    write_srt(cues, output, preserve_overlaps=True)
    return output, {
        "reason": "cleaned",
        "cleaned": True,
        "output": str(output),
        "cue_count": len(cues),
        "conflict_count": conflict_count,
        "bilingual_cjk": bool(bilingual_profile.get("suspected_bilingual_cjk")),
        "bilingual_removed": int(bilingual_profile.get("removed_han_only_cues") or 0),
        "bilingual_profile": bilingual_profile,
    }


def subtitle_has_genuine_overlaps(
    subtitle: Path,
    *,
    minimum_overlap_seconds: float = 0.050,
) -> bool:
    """Return whether a text subtitle intentionally has simultaneous cues."""
    suffix = subtitle.suffix.casefold()
    cues: list[tuple[float, float, str]] = []
    try:
        if suffix == ".srt":
            cues = parse_srt(subtitle)
        elif suffix in {".ass", ".ssa"}:
            text = read_subtitle_file(subtitle)
            fields: list[str] = []
            in_events = False
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_events = line.casefold() == "[events]"
                    continue
                if not in_events:
                    continue
                if line.casefold().startswith("format:"):
                    fields = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
                    continue
                if not line.casefold().startswith("dialogue:") or not fields:
                    continue
                parts = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
                if len(parts) != len(fields):
                    continue
                row = dict(zip(fields, parts))
                try:
                    start = _timestamp_to_seconds(row.get("start", ""))
                    end = _timestamp_to_seconds(row.get("end", ""))
                except ValueError:
                    continue
                payload = plain_subtitle_text(row.get("text", ""))
                if payload and end > start:
                    cues.append((start, end, payload))
        else:
            return False
    except OSError:
        return False

    if len(cues) < 2:
        return False
    ordered = sorted(cues, key=lambda cue: (cue[0], cue[1]))
    furthest_end = ordered[0][1]
    threshold = max(0.001, float(minimum_overlap_seconds))
    for start, end, _text in ordered[1:]:
        if start < furthest_end - threshold:
            return True
        furthest_end = max(furthest_end, end)
    return False


_ASS_DRAWING_TAG_RE = re.compile(r"\\p(\d+)", re.IGNORECASE)
# Aegisub's karaoke templater (and Kara Effector) mark generated lines "fx".
_ASS_FX_EFFECT_RE = re.compile(r"(?i)(?:^|[^a-z])fx(?:$|[^a-z])")


def _strip_ass_drawings(raw_text: str) -> str:
    """``raw_text`` without vector drawings (text between ``{\\p1}`` and ``{\\p0}``)."""
    parts: list[str] = []
    drawing = False
    position = 0
    for block in _ASS_OVERRIDE_RE.finditer(raw_text):
        if not drawing:
            parts.append(raw_text[position : block.start()])
        modes = _ASS_DRAWING_TAG_RE.findall(block.group(0))
        if modes:
            drawing = int(modes[-1]) > 0
        parts.append(_ASS_DRAWING_TAG_RE.sub("", block.group(0)))
        position = block.end()
    if not drawing:
        parts.append(raw_text[position:])
    return "".join(parts)


# Styles/actors of staff rolls and release credits: never dialogue.
_ASS_CREDIT_STYLE_RE = re.compile(
    r"(?i)(?:^|[^a-z])(?:credits?|staff|fansub|encoder?|encoded|logo|watermark)(?:[^a-z]|$)"
)
# Styles whose Latin-only lines in a Japanese file are romaji lyrics or sign/title text.
_ASS_SIGN_STYLE_RE = re.compile(r"(?i)(?:^|[^a-z])(?:title|sign|signs)(?:[^a-z]|$)")


def _sanitize_ass_events(text: str) -> tuple[str, dict[str, int]]:
    """Drop karaoke-effect rows, vector drawings and credits from an ASS script.

    A karaoke template emits one positioned event per syllable (Effect ``fx``);
    converted to SRT every syllable became its own overlapping cue. Drawings
    (``{\\p1}m 0 0 l 100 0 ...``) came through ffmpeg as subtitle text. Staff and
    release-credit styles are not dialogue. In a Japanese script (most lines carry
    kana or kanji) a Latin-only line in a song/karaoke/title/sign style or with
    ``\\k`` timing is romaji or a sign, not Japanese dialogue. An event that ends at or
    before its start is never shown: ffmpeg wrote one with End before Start as a cue
    lasting until the next event (the geometry path always left it out).
    """
    fields: list[str] = []
    section = ""
    lines = text.splitlines()
    # (line index, prefix, parts, row) of every Dialogue event
    events: list[tuple[int, str, list[str], dict[str, str]]] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.casefold()
        elif section == "[events]" and line.casefold().startswith("format:"):
            fields = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
        elif section == "[events]" and fields and line.casefold().startswith("dialogue:"):
            prefix, body = raw_line.split(":", 1)
            parts = body.split(",", len(fields) - 1)
            if len(parts) == len(fields):
                events.append((index, prefix, parts, dict(zip(fields, parts))))
    with_text = [plain_subtitle_text(_strip_ass_drawings(row.get("text", ""))) for *_rest, row in events]
    nonempty = [payload for payload in with_text if payload]
    japanese_script = len(nonempty) >= 5 and sum(
        bool(_JAPANESE_CHAR_RE.search(payload)) for payload in nonempty
    ) >= 0.5 * len(nonempty)
    fx_removed = drawings_removed = credits_removed = romaji_removed = untimed_removed = 0
    dropped: set[int] = set()
    replaced: dict[int, str] = {}
    for (index, prefix, parts, row), payload in zip(events, with_text):
        raw_text = row.get("text", "")
        try:
            untimed = _timestamp_to_seconds(row.get("end", "")) <= _timestamp_to_seconds(row.get("start", ""))
        except ValueError:
            untimed = False  # an unreadable time is left to the converter, as before
        if untimed:
            untimed_removed += 1
            dropped.add(index)
            continue
        if _ASS_FX_EFFECT_RE.search(row.get("effect", "")):
            fx_removed += 1
            dropped.add(index)
            continue
        if _ASS_CREDIT_STYLE_RE.search(row.get("style", "")) or _ASS_CREDIT_STYLE_RE.search(
            row.get("name", "")
        ):
            credits_removed += 1
            dropped.add(index)
            continue
        if (
            japanese_script
            and payload
            and not _JAPANESE_CHAR_RE.search(payload)
            and (
                _SONG_STYLE_RE.search(row.get("style", ""))
                or _ASS_SIGN_STYLE_RE.search(row.get("style", ""))
                or _KARAOKE_TAG_RE.search(raw_text)
            )
        ):
            romaji_removed += 1
            dropped.add(index)
            continue
        if "text" in row and re.search(r"\\p[1-9]", raw_text, re.IGNORECASE):
            drawings_removed += 1
            if not payload:
                dropped.add(index)
                continue
            parts = list(parts)
            parts[fields.index("text")] = _strip_ass_drawings(raw_text)
            replaced[index] = f"{prefix}:{','.join(parts)}"
        elif not payload and _ASS_OVERRIDE_RE.sub("", raw_text).replace(r"\N", "").strip():
            # Drawing commands whose ``{\\p1}`` an earlier converter removed, or a release credit.
            drawings_removed += 1
            dropped.add(index)
    kept = [replaced.get(index, line) for index, line in enumerate(lines) if index not in dropped]
    removed = {"fx_events_removed": fx_removed, "drawings_removed": drawings_removed}
    if credits_removed:
        removed["credits_removed"] = credits_removed
    if romaji_removed:
        removed["romaji_removed"] = romaji_removed
    if untimed_removed:
        removed["untimed_removed"] = untimed_removed
    return "\n".join(kept) + "\n", removed


def _ass_geometry_cues(
    source: Path,
    sanitized: str | None = None,
) -> tuple[list[tuple[float, float, str]], dict[str, int]]:
    """Assemble positioned caption fragments before text/language cleanup.

    Ruby is only discarded when a smaller kana fragment sits above a matching
    kanji-containing base in the same interval. Small standalone dialogue and
    unpositioned events are retained. Moving/drawing events are not assembled.
    ``sanitized``: ``_sanitize_ass_events`` of the file's text, when the caller has it.
    """
    text = sanitized if sanitized is not None else _sanitize_ass_events(read_subtitle_file(source))[0]
    fields: list[str] = []
    style_fields: list[str] = []
    styles: dict[str, dict[str, str]] = {}
    events: list[dict[str, object]] = []
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.casefold()
            continue
        if line.casefold().startswith("format:"):
            names = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
            if section == "[events]":
                fields = names
            elif section in {"[v4+ styles]", "[v4 styles]"}:
                style_fields = names
            continue
        if line.casefold().startswith("style:") and style_fields:
            parts = line.split(":", 1)[1].lstrip().split(",", len(style_fields) - 1)
            row = dict(zip(style_fields, parts))
            styles[row.get("name", "")] = row
            continue
        if section != "[events]" or not line.casefold().startswith("dialogue:") or not fields:
            continue
        parts = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts))
        raw_text = row.get("text", "")
        if re.search(r"\\p[1-9]", raw_text, re.IGNORECASE):
            continue
        try:
            start = _timestamp_to_seconds(row.get("start", ""))
            end = _timestamp_to_seconds(row.get("end", ""))
        except ValueError:
            continue
        # Speaker-label removal must wait until fragments have been joined.
        payload = (
            html.unescape(_ASS_OVERRIDE_RE.sub("", raw_text))
            .replace(r"\N", "\n")
            .replace(r"\n", "\n")
            .replace(r"\h", " ")
            .strip()
        )
        if not payload or end <= start:
            continue
        style = styles.get(row.get("style", ""), {})

        def number(name: str, default: float, style: dict[str, str] = style) -> float:
            try:
                value = float(style.get(name, default))
            except (ValueError, TypeError):
                return default
            # "nan" or "inf" in a damaged style line: int(alignment) raised out of the whole
            # conversion, and the file was no candidate at all.
            return value if math.isfinite(value) else default

        def override(tag: str, default: float, raw_text: str = raw_text) -> float:
            matches = re.findall(r"\\" + tag + r"(-?\d+(?:\.\d+)?)", raw_text)
            return float(matches[-1]) if matches else default

        positions = re.findall(r"\\pos\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", raw_text)
        position = (
            positions[0] if len(positions) == 1 and "\n" not in payload and r"\move" not in raw_text else None
        )
        font = override("fs", number("fontsize", 40.0))
        height = font * override("fscy", number("scaley", 100.0)) / 100.0
        # Inline scale changes often narrow only the closing punctuation.
        # Its final override is not the width of the entire base sentence.
        horizontal_scales = [float(v) for v in re.findall(r"\\fscx(-?\d+(?:\.\d+)?)", raw_text)]
        scale_x = max(horizontal_scales) if horizontal_scales else number("scalex", 100.0)
        width = font * scale_x / 100.0 * sum(1.0 if ord(ch) >= 0x2E80 else 0.55 for ch in payload)
        alignment = int(override("an", number("alignment", 2)))
        x, y = (float(position[0]), float(position[1])) if position else (0.0, 0.0)
        if alignment % 3 == 2:
            x -= width / 2
        elif alignment % 3 == 0:
            x -= width
        events.append(
            {
                "start": start,
                "end": end,
                "text": payload,
                "positioned": bool(position),
                "x": x,
                "y": y,
                "height": height,
                "width": width,
            }
        )
    groups: dict[tuple[float, float], list[dict[str, object]]] = {}
    for event in events:
        groups.setdefault((float(event["start"]), float(event["end"])), []).append(event)
    cues: list[tuple[float, float, str]] = []
    ruby_removed = assembled = 0
    for (start, end), group in groups.items():
        # Outline/blur layers render the same caption at the same position.
        # Deduplicate before joining fragments, while retaining repeated words
        # at different positions (for example two speakers saying "はい").
        seen_positions: set[tuple[str, float, float]] = set()
        positioned = []
        for event in group:
            if not event["positioned"]:
                continue
            position_key = (str(event["text"]), float(event["x"]), float(event["y"]))
            if position_key not in seen_positions:
                seen_positions.add(position_key)
                positioned.append(event)
        ruby: set[int] = set()
        for index, reading in enumerate(positioned):
            if not re.fullmatch(r"[ぁ-ゖァ-ヺーゝゞヽヾ・\s]+", str(reading["text"])):
                continue
            for base in positioned:
                if not re.search(r"[一-龯々0-9０-９]", str(base["text"])):
                    continue
                h = float(base["height"])
                dy = float(base["y"]) - float(reading["y"])
                overlap = min(
                    float(reading["x"]) + float(reading["width"]),
                    float(base["x"]) + float(base["width"]),
                ) - max(float(reading["x"]), float(base["x"]))
                if (
                    h > 0
                    and float(reading["height"]) <= 0.70 * h
                    and 0.45 * h <= dy <= 1.75 * h
                    and overlap > 0
                ):
                    ruby.add(index)
                    break
        ruby_removed += len(ruby)
        rows: list[list[dict[str, object]]] = []
        for index, event in sorted(
            enumerate(positioned),
            key=lambda pair: (float(pair[1]["y"]), float(pair[1]["x"])),
        ):
            if index in ruby:
                continue
            if rows and abs(float(event["y"]) - float(rows[-1][0]["y"])) <= max(
                3.0, 0.12 * float(rows[-1][0]["height"])
            ):
                rows[-1].append(event)
            else:
                rows.append([event])
        if len(positioned) >= 2:
            lines = [
                "".join(str(event["text"]) for event in sorted(row, key=lambda event: float(event["x"])))
                for row in rows
            ]
            payload = plain_subtitle_text("\n".join(lines))
            if payload:
                cues.append((start, end, payload))
            assembled += max(0, len(positioned) - len(ruby) - 1)
        else:
            for event in positioned:
                payload = plain_subtitle_text(str(event["text"]))
                if payload:
                    cues.append((start, end, payload))
        for event in group:
            if not event["positioned"]:
                payload = plain_subtitle_text(str(event["text"]))
                if payload:
                    cues.append((start, end, payload))
    return cues, {
        "positioned_events": sum(bool(event["positioned"]) for event in events),
        "ruby_removed": ruby_removed,
        "fragments_joined": assembled,
    }


def _manual_ass_to_srt(
    source: Path, output: Path, geometry_cues: list[tuple[float, float, str]] | None = None
) -> bool:
    """``geometry_cues``: ``_ass_geometry_cues(source)`` already parsed by the caller."""
    cues = list(geometry_cues) if geometry_cues is not None else _ass_geometry_cues(source)[0]
    if not cues:
        return False
    cues, _bilingual_profile = _filter_parallel_chinese_cues(cues)
    if not cues:
        return False
    cues, _layers_merged = merge_layered_cues(cues)
    cues, _parallel_merged = _merge_parallel_cues(cues)
    write_srt(cues, output, preserve_overlaps=True)
    return True


_VTT_TIME = r"(?:(\d+):)?([0-5]?\d):([0-5]?\d)[.,](\d{1,3})"
_VTT_TIMING_RE = re.compile(r"^\s*" + _VTT_TIME + r"\s+-->\s+" + _VTT_TIME + r"(?:\s+.*)?$")
# Voice/class/language/ruby spans and inline timestamps: <v Name>, <c.yellow>, </c>, <00:01.000>.
_VTT_TAG_RE = re.compile(r"</?(?:v|c|lang|ruby|rt|rp|b|i|u)(?:[.\s][^>]*)?>|<\d[\d:.]*>", re.IGNORECASE)
# Furigana (<ruby>漢字<rt>かんじ</rt></ruby>) is a reading, not spoken text: with only the
# tags removed the line read 漢字かんじ (wrong words in the popup, in sync and in read-along).
_VTT_RUBY_READING_RE = _RUBY_READING_RE
# Direction marks streaming services put before a line (&lrm;): never text.
_VTT_DIRECTION_MARK_RE = re.compile(r"&(?:lrm|rlm);|[\u200e\u200f\u202a-\u202c]", re.IGNORECASE)


def _vtt_text(line: str) -> str:
    """One cue line without readings, tags and direction marks (``&amp;`` and the other
    entities are read where every subtitle text is: ``plain_subtitle_text``)."""
    return _VTT_DIRECTION_MARK_RE.sub("", _VTT_TAG_RE.sub("", _VTT_RUBY_READING_RE.sub("", line)))


def _vtt_seconds(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def parse_webvtt(text: str) -> list[tuple[float, float, str]]:
    """Cues of a WebVTT file: the WEBVTT header, NOTE, STYLE and REGION blocks are skipped,
    cue identifiers and settings (``line:``, ``align:`` …) dropped, voice/class tags removed;
    timestamps with or without hours, with ``.`` (or ``,``) before the milliseconds."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.strip("\n").split("\n")
        # The timing line is the first one, or the second after a cue identifier.
        index = next((i for i, line in enumerate(lines[:2]) if "-->" in line), None)
        if index is None:
            continue  # WEBVTT header, NOTE, STYLE, REGION
        match = _VTT_TIMING_RE.match(lines[index])
        if match is None:
            continue
        start = _vtt_seconds(*match.groups()[:4])
        end = _vtt_seconds(*match.groups()[4:])
        payload = "\n".join(
            text for text in (_vtt_text(line).strip() for line in lines[index + 1 :]) if text
        ).strip()
        if payload and end > start:
            cues.append((start, end, payload))
    return cues


def _webvtt_to_srt(subtitle: Path, cache_dir: Path, *, force: bool) -> tuple[Path, dict[str, object]]:
    stat = subtitle.stat()
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:plain-srt-webvtt-v2".encode()
    ).hexdigest()[:20]
    output = cache_dir / "converted" / f"vtt2-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if _cached_srt_ready(output):
        return output, {"reason": "cached", "converted": True, "output": str(output)}
    cues = parse_webvtt(decode_subtitle_bytes(subtitle.read_bytes()))
    if not cues:
        return subtitle, {"reason": "conversion_failed", "converted": False, "method": "webvtt"}
    write_srt(cues, output, preserve_overlaps=True)
    if not _cached_srt_ready(output):
        output.unlink(missing_ok=True)
        return subtitle, {"reason": "conversion_failed", "converted": False, "method": "webvtt"}
    return output, {"reason": "converted", "converted": True, "method": "webvtt", "output": str(output)}


def convert_to_plain_srt(
    subtitle: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Convert ASS/SSA/WebVTT to styling-free SRT. Existing SRT is returned unchanged."""
    suffix = subtitle.suffix.casefold()
    if suffix == ".srt":
        return subtitle, {"reason": "already_srt", "converted": False}
    if suffix == ".vtt":
        # Counted as a text subtitle, but sent unconverted the sync engines refused it.
        return _webvtt_to_srt(subtitle, cache_dir, force=force)
    if suffix not in {".ass", ".ssa"}:
        return subtitle, {"reason": "unsupported_format", "converted": False}

    stat = subtitle.stat()
    # "-decoded": Shift-JIS/UTF-16 sources were converted to mojibake before.
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:plain-srt-v11-geometry-layers-decoded-nofx-nondialogue".encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "converted"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v20-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if _cached_srt_ready(output):
        return output, {"reason": "cached", "converted": True, "output": str(output)}
    output.unlink(missing_ok=True)

    # Read, decoded and cleaned once: the geometry parser and the copy ffmpeg converts
    # both use this text (the file was read and cleaned a second time for ffmpeg).
    raw = subtitle.read_bytes()
    sanitized, removed = _sanitize_ass_events(decode_subtitle_bytes(raw))
    # FFmpeg's plain-text encoder drops layout before our language filter can
    # distinguish ruby/base fragments. Positioned captions use the ASS parser.
    geometry_cues, geometry = _ass_geometry_cues(subtitle, sanitized)
    # The Python fallback below converts these same events: parsed once, not again.
    parsed_geometry_cues = list(geometry_cues)
    if geometry["positioned_events"] >= 2:
        geometry_cues, bilingual_profile = _filter_parallel_chinese_cues(geometry_cues)
        geometry_cues, _layers_merged = merge_layered_cues(geometry_cues)
        geometry_cues, parallel_merged = _merge_parallel_cues(geometry_cues)
        if geometry_cues:
            write_srt(geometry_cues, output, preserve_overlaps=True)
            return output, {
                "reason": "converted",
                "converted": True,
                "method": "ass-geometry",
                "output": str(output),
                "geometry": geometry,
                "parallel_merged": parallel_merged,
                "bilingual_cjk": bool(bilingual_profile.get("suspected_bilingual_cjk")),
                "bilingual_removed": int(bilingual_profile.get("removed_han_only_cues") or 0),
                "bilingual_profile": bilingual_profile,
            }

    filename_language = subtitle_filename_language_profile(subtitle.name)
    explicit_mixed_cjk = filename_language.get("purity") == "mixed_japanese_chinese"
    # ffmpeg may merge two ASS tracks into one multiline cue before we can
    # identify the Chinese half. Explicit mixed files use the manual parser,
    # which keeps parallel Dialogue events separate until filtering.
    resolved = (
        None if explicit_mixed_cjk else (shutil.which(ffmpeg_path) if "/" not in ffmpeg_path else ffmpeg_path)
    )
    ffmpeg_error = ""
    # Names of this conversion only: two processes converting the same file (the window
    # and the agent) overwrote and deleted each other's copies.
    run = f"{os.getpid()}-{threading.get_ident()}"
    ffmpeg_output = output.with_name(f".{output.stem}.{run}.ffmpeg.srt")
    # ffmpeg drops every non-UTF-8 line ("missing -sub_charenc"), so a
    # Shift-JIS/UTF-16 file goes in as a UTF-8 copy; karaoke-effect rows and
    # drawings are removed from that copy as they are in the geometry path.
    ffmpeg_input = output.with_name(f".{output.stem}.{run}.utf8{suffix}")
    if resolved:
        ffmpeg_output.unlink(missing_ok=True)
        try:
            try:
                raw.decode("utf-8")
                plain_utf8 = True
            except UnicodeDecodeError:
                plain_utf8 = False
            if plain_utf8 and not any(removed.values()):
                source_for_ffmpeg = subtitle
            else:
                ffmpeg_input.write_text(sanitized, encoding="utf-8")
                source_for_ffmpeg = ffmpeg_input
        except OSError:
            source_for_ffmpeg = subtitle
        command = [
            resolved,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source_for_ffmpeg),
            str(ffmpeg_output),
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode == 0 and ffmpeg_output.exists() and ffmpeg_output.stat().st_size > 0:
                # Rewrite through our parser to strip any style tags retained by ffmpeg.
                cues = parse_srt(ffmpeg_output)
                ffmpeg_output.unlink(missing_ok=True)
                if cues:
                    cues, bilingual_profile = _filter_parallel_chinese_cues(cues)
                    if not cues:
                        output.unlink(missing_ok=True)
                    else:
                        cues, _layers_merged = merge_layered_cues(cues)
                        cues, parallel_merged = _merge_parallel_cues(cues)
                        write_srt(cues, output, preserve_overlaps=True)
                        return output, {
                            "reason": "converted",
                            "converted": True,
                            "method": "ffmpeg",
                            "output": str(output),
                            "parallel_merged": parallel_merged,
                            "bilingual_cjk": bool(bilingual_profile.get("suspected_bilingual_cjk")),
                            "bilingual_removed": int(bilingual_profile.get("removed_han_only_cues") or 0),
                            "bilingual_profile": bilingual_profile,
                        }
            ffmpeg_error = completed.stdout[-1000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            ffmpeg_error = str(exc)
        finally:
            ffmpeg_output.unlink(missing_ok=True)
            ffmpeg_input.unlink(missing_ok=True)

    output.unlink(missing_ok=True)
    try:
        if _manual_ass_to_srt(subtitle, output, parsed_geometry_cues):
            return output, {
                "reason": "converted",
                "converted": True,
                "method": "python",
                "output": str(output),
            }
    except OSError as exc:
        ffmpeg_error = ffmpeg_error or str(exc)

    output.unlink(missing_ok=True)
    return subtitle, {
        "reason": "conversion_failed",
        "converted": False,
        "error": ffmpeg_error if verbose else err("subtitles.assConversionFailed"),
    }
