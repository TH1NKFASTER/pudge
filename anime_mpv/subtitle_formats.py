from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
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
    relevant = japanese + han_only
    transition_ratio = transitions / max(relevant - 1, 1)
    short_han_ratio = short_han / max(han_only, 1)
    suspected = bool(
        japanese >= 20
        and han_only >= 20
        and short_han_ratio >= 0.50
        and transition_ratio >= 0.35
    )
    return {
        "suspected_bilingual_cjk": suspected,
        "japanese_cues": japanese,
        "han_only_cues": han_only,
        "short_han_cues": short_han,
        "short_han_ratio": round(short_han_ratio, 4),
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


def _filter_parallel_chinese_cues(
    cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], dict[str, object]]:
    profile = bilingual_cjk_profile(cues)
    if not profile.get("suspected_bilingual_cjk"):
        return cues, profile

    filtered: list[tuple[float, float, str]] = []
    removed = 0
    for start, end, text in cues:
        if _cue_script_kind(text) != "han_only":
            filtered.append((start, end, text))
            continue
        compact = re.sub(r"[^\u3400-\u4dbf\u4e00-\u9fff]", "", text)
        duration = end - start
        has_simplified_hint = any(ch in _SIMPLIFIED_CHINESE_HINTS for ch in compact)
        # Parallel bilingual ASS tracks become alternating SRT cues after
        # overlap removal. The translated line is usually squeezed to the
        # minimum 300 ms duration. Longer full sentences and lines containing
        # simplified-Chinese-only characters are also translations. Preserve
        # short ambiguous kanji-only Japanese cues such as 私, 本当 or 大丈夫.
        translated = bool(
            duration <= 0.45
            or len(compact) > 4
            or has_simplified_hint
        )
        if translated:
            removed += 1
            continue
        filtered.append((start, end, text))

    profile = dict(profile)
    profile["removed_han_only_cues"] = removed
    profile["remaining_cues"] = len(filtered)
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
) -> list[tuple[float, float, str]]:
    """Make playback SRT strictly non-overlapping and safe for mpv.

    By default cues are sorted for malformed third-party files. Piecewise
    retiming passes ``preserve_order=True``: dialogue order is then sacred and
    must never be changed merely to make timestamps look chronological.

    Every emitted cue starts after zero and lasts at least 300 ms. When two
    cues collide, trim the previous cue only if it stays readable; otherwise
    delay and, if necessary, extend the current cue.
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
            gap = start - previous_end
            if gap < minimum_gap:
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
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    normalized = _separate_touching_cues(cues, preserve_order=preserve_order)
    for index, (start, end, text) in enumerate(normalized, start=1):
        payload = plain_subtitle_text(text)
        if not payload or end <= start:
            continue
        blocks.append(
            f"{index}\n{_seconds_to_timestamp(start)} --> {_seconds_to_timestamp(end)}\n{payload}"
        )
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return path


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
        if subtitle.name.startswith("v12-"):
            return subtitle, {"reason": "already_clean", "cleaned": False}
    except ValueError:
        pass

    stat = subtitle.stat()
    digest = hashlib.sha1(
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-v12".encode()
    ).hexdigest()[:20]
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v12-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, {"reason": "cached", "cleaned": True, "output": str(output)}

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
    write_srt(cues, output)
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


def _manual_ass_to_srt(source: Path, output: Path) -> bool:
    text = source.read_text(encoding="utf-8-sig", errors="replace")
    fields: list[str] = []
    cues: list[tuple[float, float, str]] = []
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
        raw_text = row.get("text", "")
        # ASS vector drawings are not dialogue and become garbage in SRT.
        if re.search(r"\\p[1-9]", raw_text, re.IGNORECASE):
            continue
        try:
            start = _timestamp_to_seconds(row.get("start", ""))
            end = _timestamp_to_seconds(row.get("end", ""))
        except ValueError:
            continue
        payload = plain_subtitle_text(raw_text)
        if payload and end > start:
            cues.append((start, end, payload))
    if not cues:
        return False
    cues, _parallel_merged = _merge_parallel_cues(cues)
    write_srt(cues, output)
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
        f"{subtitle.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:plain-srt-v4".encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "converted"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v12-{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, {"reason": "cached", "converted": True, "output": str(output)}

    resolved = shutil.which(ffmpeg_path) if "/" not in ffmpeg_path else ffmpeg_path
    ffmpeg_error = ""
    if resolved:
        command = [
            resolved,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(subtitle),
            str(output),
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
            if completed.returncode == 0 and output.exists() and output.stat().st_size > 0:
                # Rewrite through our parser to strip any style tags retained by ffmpeg.
                cues = parse_srt(output)
                if cues:
                    cues, parallel_merged = _merge_parallel_cues(cues)
                    write_srt(cues, output)
                    return output, {
                        "reason": "converted",
                        "converted": True,
                        "method": "ffmpeg",
                        "output": str(output),
                        "parallel_merged": parallel_merged,
                    }
            ffmpeg_error = completed.stdout[-1000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            ffmpeg_error = str(exc)

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
