from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


_TIME_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)
_TIMING_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)
_ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")
# Strip actual HTML tags, but do not eat Japanese dialogue merely wrapped in
# ASCII angle brackets, such as ``<ずっと嫌いだった>``.
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>", re.IGNORECASE)
_RUBY_RP_RE = re.compile(r"<rp[^>]*>.*?</rp>", re.IGNORECASE | re.DOTALL)
_RUBY_WITH_RB_RE = re.compile(
    r"<ruby[^>]*>\s*<rb[^>]*>(.*?)</rb>.*?<rt[^>]*>.*?</rt>.*?</ruby>",
    re.IGNORECASE | re.DOTALL,
)
_RUBY_SIMPLE_RE = re.compile(
    r"<ruby[^>]*>(.*?)<rt[^>]*>.*?</rt>.*?</ruby>",
    re.IGNORECASE | re.DOTALL,
)
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


_FILENAME_JAPANESE_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,])"
    r"(?:ja(?:\[cc\])?|jp|jpn|japanese|日本語)"
    r"(?:$|[\s._\-\[\]\),])"
)
_FILENAME_CHINESE_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(,])"
    r"(?:chs|cht|chi|zho|zh(?:[-_](?:cn|tw|hans|hant))?|chinese|中文|简中|繁中|簡中)"
    r"(?:$|[\s._\-\[\]\),])"
)


def subtitle_filename_language_profile(filename: str | Path) -> dict[str, object]:
    """Classify explicit Japanese/Chinese language markers in a subtitle name."""
    name = Path(filename).name
    japanese = bool(_FILENAME_JAPANESE_MARKER_RE.search(name))
    chinese = bool(_FILENAME_CHINESE_MARKER_RE.search(name))
    if japanese and chinese:
        purity = "mixed_japanese_chinese"
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


def _timestamp_to_seconds(value: str) -> float:
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Некорректный timestamp субтитров: {value}")
    milliseconds = match.group("ms").ljust(3, "0")[:3]
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(milliseconds) / 1000.0
    )


def _seconds_to_timestamp(value: float) -> str:
    total_ms = max(0, int(round(value * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _remove_embedded_furigana(value: str) -> str:
    value = _RUBY_RP_RE.sub("", value)
    value = _RUBY_WITH_RB_RE.sub(lambda match: match.group(1), value)
    value = _RUBY_SIMPLE_RE.sub(lambda match: match.group(1), value)
    for pattern in _FURIGANA_PATTERNS:
        value = pattern.sub(r"\1", value)
    return value


def _remove_leading_speaker_labels(value: str) -> str:
    """Remove parenthesized speaker metadata attached to actual dialogue.

    A standalone final cue such as ``（ドアが開く）`` is preserved. A label on
    its own line before dialogue, or a label directly followed by dialogue, is
    removed: ``（南）赤石さん`` -> ``赤石さん``.
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
    # ``모巨大生物``). Remove only that narrow artefact and leave legitimate
    # Korean lines untouched.
    value = _STRAY_HANGUL_BEFORE_JAPANESE_RE.sub("", value)
    lines = [line.strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
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
        try:
            start = _timestamp_to_seconds(match.group("start"))
            end = _timestamp_to_seconds(match.group("end"))
        except ValueError:
            continue
        if end <= start:
            continue
        payload = plain_subtitle_text("\n".join(lines[timing_index + 1 :]))
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
    paired: set[int] = set()
    for index, ((start, end, _text), kind) in enumerate(zip(cues, resolved_kinds)):
        if kind != "han_only":
            continue
        duration = max(end - start, 0.001)
        for _japanese_index, japanese_start, japanese_end in japanese:
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
        kind == "han_only" and end - start <= 0.45
        for (start, end, _text), kind in zip(cues, kinds)
    )
    transitions = sum(
        left != right and {left, right} == {"japanese", "han_only"}
        for left, right in zip(kinds, kinds[1:])
    )
    parallel_han = len(_parallel_han_cue_indices(cues, kinds))
    relevant = japanese + han_only
    transition_ratio = transitions / max(relevant - 1, 1)
    short_han_ratio = short_han / max(han_only, 1)
    parallel_han_ratio = parallel_han / max(han_only, 1)
    suspected = bool(
        japanese >= 20
        and han_only >= 20
        and (
            (short_han_ratio >= 0.50 and transition_ratio >= 0.35)
            or parallel_han_ratio >= 0.50
        )
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

def subtitle_bilingual_cjk_profile(path: Path) -> dict[str, object]:
    suffix = path.suffix.casefold()
    if suffix == ".srt":
        try:
            return bilingual_cjk_profile(parse_srt(path))
        except OSError:
            return {"suspected_bilingual_cjk": False}
    if suffix not in {".ass", ".ssa"}:
        return {"suspected_bilingual_cjk": False}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {"suspected_bilingual_cjk": False}
    cues: list[tuple[float, float, str]] = []
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
        if payload and end > start:
            cues.append((start, end, payload))
    return bilingual_cjk_profile(cues)


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
    # next to a kana-containing line (e.g. 出発前 / ルーデルドルフ閣下が…).  Do not
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
    consistent_inline_bilingual = bool(
        mixed_line_cues >= 2
        and mixed_line_cues / max(len(cues), 1) >= 0.25
    )

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
            strong_simplified_hints = sum(
                ch in _STRONG_SIMPLIFIED_CHINESE_HINTS for ch in compact
            )
            likely_translation = bool(
                consistent_inline_bilingual or strong_simplified_hints >= 2
            )
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
            index in paired_han_indices
            or duration <= 0.45
            or len(compact) > 4
            or has_simplified_hint
        )
        if translated:
            removed += 1
            continue
        filtered.append((start, end, text))

    profile = dict(profile)
    profile["removed_han_only_cues"] = removed
    profile["removed_inline_chinese_lines"] = inline_removed
    profile["remaining_cues"] = len(filtered)
    profile["suspected_bilingual_cjk"] = bool(
        profile.get("suspected_bilingual_cjk") or inline_removed
    )
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
    for index, (start, end, text) in enumerate(normalized, start=1):
        payload = plain_subtitle_text(text)
        if not payload or end <= start:
            continue
        blocks.append(
            f"{index}\n{_seconds_to_timestamp(start)} --> {_seconds_to_timestamp(end)}\n{payload}"
        )
    payload_text = "\n\n".join(blocks) + ("\n" if blocks else "")
    # Write atomically: callers treat an existing output as a ready cache, so
    # an interrupted write must never leave a truncated file at *path*.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload_text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _cached_srt_ready(path: Path) -> bool:
    """A cached SRT is reusable only when it still parses to real cues."""
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        return bool(parse_srt(path))
    except (OSError, ValueError):
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
            return subtitle, {"reason": "already_clean", "cleaned": False}
    except ValueError:
        pass

    stat = subtitle.stat()
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-v16".encode()
    ).hexdigest()[:20]
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v16-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if _cached_srt_ready(output):
        return output, {"reason": "cached", "cleaned": True, "output": str(output)}
    output.unlink(missing_ok=True)

    cues = parse_srt(subtitle)
    if not cues:
        return subtitle, {"reason": "no_valid_cues", "cleaned": False}

    cues, bilingual_profile = _filter_parallel_chinese_cues(cues)
    if not cues:
        return subtitle, {"reason": "no_japanese_cues_after_bilingual_filter", "cleaned": False}

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
            text = subtitle.read_text(encoding="utf-8-sig", errors="replace")
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


def _ass_geometry_cues(
    source: Path,
) -> tuple[list[tuple[float, float, str]], dict[str, int]]:
    """Assemble positioned caption fragments before text/language cleanup.

    Ruby is only discarded when a smaller kana fragment sits above a matching
    kanji-containing base in the same interval. Small standalone dialogue and
    unpositioned events are retained. Moving/drawing events are not assembled.
    """
    text = source.read_text(encoding="utf-8-sig", errors="replace")
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
            names = [
                part.strip().casefold() for part in line.split(":", 1)[1].split(",")
            ]
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
        if (
            section != "[events]"
            or not line.casefold().startswith("dialogue:")
            or not fields
        ):
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
                return float(style.get(name, default))
            except (ValueError, TypeError):
                return default

        def override(tag: str, default: float, raw_text: str = raw_text) -> float:
            matches = re.findall(r"\\" + tag + r"(-?\d+(?:\.\d+)?)", raw_text)
            return float(matches[-1]) if matches else default

        positions = re.findall(
            r"\\pos\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", raw_text
        )
        position = (
            positions[0]
            if len(positions) == 1 and "\n" not in payload and r"\move" not in raw_text
            else None
        )
        font = override("fs", number("fontsize", 40.0))
        height = font * override("fscy", number("scaley", 100.0)) / 100.0
        # Inline scale changes often narrow only the closing punctuation.
        # Its final override is not the width of the entire base sentence.
        horizontal_scales = [
            float(v) for v in re.findall(r"\\fscx(-?\d+(?:\.\d+)?)", raw_text)
        ]
        scale_x = (
            max(horizontal_scales) if horizontal_scales else number("scalex", 100.0)
        )
        width = (
            font
            * scale_x
            / 100.0
            * sum(1.0 if ord(ch) >= 0x2E80 else 0.55 for ch in payload)
        )
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
        groups.setdefault((float(event["start"]), float(event["end"])), []).append(
            event
        )
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
                "".join(
                    str(event["text"])
                    for event in sorted(row, key=lambda event: float(event["x"]))
                )
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


def _manual_ass_to_srt(source: Path, output: Path) -> bool:
    cues, _geometry = _ass_geometry_cues(source)
    if not cues:
        return False
    cues, _bilingual_profile = _filter_parallel_chinese_cues(cues)
    if not cues:
        return False
    cues, _parallel_merged = _merge_parallel_cues(cues)
    write_srt(cues, output, preserve_overlaps=True)
    return True


def convert_to_plain_srt(
    subtitle: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Convert ASS/SSA to styling-free SRT. Existing SRT is returned unchanged."""
    suffix = subtitle.suffix.casefold()
    if suffix == ".srt":
        return subtitle, {"reason": "already_srt", "converted": False}
    if suffix not in {".ass", ".ssa"}:
        return subtitle, {"reason": "unsupported_format", "converted": False}

    stat = subtitle.stat()
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:plain-srt-v9-geometry-layers".encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "converted"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v19-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if _cached_srt_ready(output):
        return output, {"reason": "cached", "converted": True, "output": str(output)}
    output.unlink(missing_ok=True)

    # FFmpeg's plain-text encoder drops layout before our language filter can
    # distinguish ruby/base fragments. Positioned captions use the ASS parser.
    geometry_cues, geometry = _ass_geometry_cues(subtitle)
    if geometry["positioned_events"] >= 2:
        geometry_cues, bilingual_profile = _filter_parallel_chinese_cues(geometry_cues)
        geometry_cues, parallel_merged = _merge_parallel_cues(geometry_cues)
        if geometry_cues:
            write_srt(geometry_cues, output, preserve_overlaps=True)
            return output, {"reason": "converted", "converted": True, "method": "ass-geometry", "output": str(output), "geometry": geometry, "parallel_merged": parallel_merged, "bilingual_cjk": bool(bilingual_profile.get("suspected_bilingual_cjk")), "bilingual_removed": int(bilingual_profile.get("removed_han_only_cues") or 0), "bilingual_profile": bilingual_profile}

    filename_language = subtitle_filename_language_profile(subtitle.name)
    explicit_mixed_cjk = (
        filename_language.get("purity") == "mixed_japanese_chinese"
    )
    # ffmpeg may merge two ASS tracks into one multiline cue before we can
    # identify the Chinese half. Explicit mixed files use the manual parser,
    # which keeps parallel Dialogue events separate until filtering.
    resolved = (
        None
        if explicit_mixed_cjk
        else (shutil.which(ffmpeg_path) if "/" not in ffmpeg_path else ffmpeg_path)
    )
    ffmpeg_error = ""
    ffmpeg_output = output.with_name(f".{output.stem}.ffmpeg.srt")
    if resolved:
        ffmpeg_output.unlink(missing_ok=True)
        command = [
            resolved,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(subtitle),
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
                        cues, parallel_merged = _merge_parallel_cues(cues)
                        write_srt(cues, output, preserve_overlaps=True)
                        return output, {
                            "reason": "converted",
                            "converted": True,
                            "method": "ffmpeg",
                            "output": str(output),
                            "parallel_merged": parallel_merged,
                            "bilingual_cjk": bool(
                                bilingual_profile.get("suspected_bilingual_cjk")
                            ),
                            "bilingual_removed": int(
                                bilingual_profile.get("removed_han_only_cues") or 0
                            ),
                            "bilingual_profile": bilingual_profile,
                        }
            ffmpeg_error = completed.stdout[-1000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            ffmpeg_error = str(exc)
        finally:
            ffmpeg_output.unlink(missing_ok=True)

    output.unlink(missing_ok=True)
    try:
        if _manual_ass_to_srt(subtitle, output):
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
        "error": ffmpeg_error if verbose else "Не удалось преобразовать ASS/SSA в SRT",
    }
