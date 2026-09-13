from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz

from .alignment_quality import build_alignment_report
from .audio_activity import (
    analyze_audio_activity as analyze_audio_activity,
    gate_activity_regions as gate_activity_regions,
    merge_activity_regions,
)
from .database import Database
from .metadata_cache import MetadataCache
from .work_scheduler import WorkPriority
from .reading_audio_alignment import (
    align_light_novel_to_transcript,
    chapter_audio_text,
    normalize_reading_text,
    audio_position_for_light_novel,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
    _punctuation_boundaries,
)

LOGGER = logging.getLogger(__name__)

AUDIOBOOK_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
_POSITION_WRITE_INTERVAL = 10.0
_MONITOR_POLL_INTERVAL = 0.25
_STOP_GRACE_SECONDS = 0.12
_STOP_IPC_TIMEOUT = 0.05
_PLAYBACK_STALL_SECONDS = 1.1
_PLAYBACK_MOTION_EPSILON = 0.015
_STT_CHUNK_SECONDS = 300.0
_STT_MLX_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
_STT_MLX_MEMORY_LIMIT_BYTES = 6 * 1024 * 1024 * 1024
_LEGACY_AUDIOBOOK_STT_OUTPUT_RE = re.compile(r"/file-\d{4}\.json(?:\s|$)")
_READING_AUDIO_ALIGNMENT_REVISION = "reading-audio-v3-leading-prefix-v22"
_CHAPTER_START_PRECISION_MODEL = "mlx-community/whisper-small-mlx"
_CHAPTER_START_PRECISION_BEFORE_SECONDS = 24.0
_CHAPTER_START_PRECISION_AFTER_SECONDS = 48.0
_CHAPTER_START_READING_SOURCE_LIMIT = 320
_CHAPTER_START_JITEN_BASE = "https://api.jiten.moe/api"


_READING_NOTATION_RE = re.compile(r"([\u3400-\u9fff々〆ヵヶ]+)\[([^\]]+)\]")

def _reading_notation_to_hiragana(value: Any) -> str:
    """Convert Jiten-style ruby notation into a compact spoken kana form."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return ""
    text = _READING_NOTATION_RE.sub(lambda match: match.group(2), text)
    out: list[str] = []
    for character in text:
        code = ord(character)
        if "ァ" <= character <= "ヶ":
            character = chr(code - 0x60)
        if "ぁ" <= character <= "ゟ" or character == "ー":
            out.append(character)
    return "".join(out)


def _reading_hints_from_cached_parse(
    parsed: dict[str, Any],
    *,
    source_limit: int = 240,
) -> list[dict[str, Any]]:
    """Build source-offset -> spoken-reading hints from an existing Jiten parse."""

    paragraphs = parsed.get("paragraphs") or []
    token_groups = parsed.get("tokens") or []
    vocabulary = parsed.get("vocabulary") or []
    vocab: dict[tuple[int, int], dict[str, Any]] = {}
    for item in vocabulary:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item.get("wordId")), int(item.get("readingIndex")))
        except (TypeError, ValueError):
            continue
        vocab[key] = item

    hints: list[dict[str, Any]] = []
    base_offset = 0
    for paragraph_index, paragraph_value in enumerate(paragraphs):
        paragraph = str(paragraph_value or "")
        spoken_paragraph = chapter_audio_text(paragraph)
        normalized_paragraph = normalize_reading_text(spoken_paragraph)
        # Reader image figures are zero-width in the audiobook coordinate
        # system. Keep their token-group slot, but never advance source offset.
        if paragraph.strip() and not spoken_paragraph.strip():
            continue
        if base_offset >= int(source_limit):
            break
        rows = token_groups[paragraph_index] if paragraph_index < len(token_groups) else []
        if not isinstance(rows, list):
            rows = []
        for token in rows:
            if not isinstance(token, dict):
                continue
            try:
                start = max(0, int(token.get("start") or 0))
                end = max(start, int(token.get("end") or (start + int(token.get("length") or 0))))
            except (TypeError, ValueError):
                continue
            if end <= start or start >= len(paragraph):
                continue
            end = min(len(paragraph), end)
            surface = paragraph[start:end]
            source_start = base_offset + len(normalize_reading_text(paragraph[:start]))
            source_end = base_offset + len(normalize_reading_text(paragraph[:end]))
            if source_start >= int(source_limit) or source_end <= source_start:
                continue
            card = token.get("card") if isinstance(token.get("card"), dict) else {}
            try:
                key = (int(token.get("wordId")), int(token.get("readingIndex")))
            except (TypeError, ValueError):
                key = None
            vocab_row = vocab.get(key) if key is not None else None
            reading_value = (
                token.get("reading")
                or card.get("reading")
                or ((vocab_row or {}).get("reading") if isinstance(vocab_row, dict) else "")
            )
            if not reading_value and isinstance(token.get("rubies"), list):
                reading_value = "".join(
                    str(row.get("text") or row.get("reading") or "")
                    for row in token.get("rubies") or []
                    if isinstance(row, dict)
                )
            reading = _reading_notation_to_hiragana(reading_value)
            if not reading:
                continue
            hints.append(
                {
                    "offset_start": int(source_start),
                    "offset_end": int(source_end),
                    "surface": normalize_reading_text(surface),
                    "reading": reading,
                }
            )
        base_offset += len(normalized_paragraph)
    hints.sort(key=lambda row: (int(row["offset_start"]), int(row["offset_end"])))
    # Keep all parsed reading hints inside the already-bounded source window.
    # A historical 64-token cap happened to end at offset ~142 in Spice and
    # Wolf II / 第三幕, exactly before 検問を通り通行証をもらって.  The token
    # parser knew those words, but the runtime bridge never received their
    # readings, so it could only linearly interpolate one coarse 142→155 gap.
    return hints


def terminate_legacy_audiobook_stt_workers(*, grace_seconds: float = 0.6) -> list[int]:
    """Terminate pre-v131 whole-book audiobook STT workers left behind by upgrades.

    Legacy audiobook workers are uniquely identified by the old ``file-0001.json``
    checkpoint naming convention.  Current chunk workers always write
    ``chunk-00001.json`` and are deliberately excluded.
    """
    if os.name != "posix":
        return []
    try:
        completed = subprocess.run(
            ["ps", "-axo", "uid=,pid=,ppid=,command="],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    try:
        own_uid = os.getuid()
    except AttributeError:
        return []
    current_pid = os.getpid()
    candidates: list[int] = []
    for raw_line in (completed.stdout or "").splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", raw_line)
        if not match:
            continue
        uid, pid, _ppid, command = int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4)
        if uid != own_uid or pid == current_pid:
            continue
        if "pudge.subtitles.stt_worker" not in command or "--words" not in command:
            continue
        if not _LEGACY_AUDIOBOOK_STT_OUTPUT_RE.search(command):
            continue
        candidates.append(pid)

    terminated: list[int] = []
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        terminated.append(pid)

    if not terminated:
        return []
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    remaining = set(terminated)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except PermissionError:
                remaining.discard(pid)
        if remaining:
            time.sleep(0.04)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return terminated



def audiobook_series_path_matches(value: str, series_title: str) -> bool:
    """Strict series identity for audiobook release/file paths.

    A requested series may be a prefix of a legitimate volume title
    (``狼と香辛料II``), but must not match from the middle of a spin-off title
    (``新説 狼と香辛料``).  Evaluate path components after removing common
    author/index wrappers instead of doing an arbitrary substring match.
    """
    def key(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", html.unescape(str(text or "")))
        return re.sub(r"[^0-9A-Za-zぁ-ゟ゠-ヿ一-鿿々〆ヶ]+", "", normalized).casefold()

    needle = key(series_title)
    if not needle:
        return False
    components = [part.strip() for part in re.split(r"[/\\]+", unicodedata.normalize("NFKC", str(value or ""))) if part.strip()]
    for raw in components or [str(value or "")]:
        cleaned = str(raw)
        # Remove repeated author/index/store wrappers only from the beginning.
        for _ in range(4):
            newer = re.sub(r"^\s*[\[【(（][^\]】)）]{1,100}[\]】)）]\s*", "", cleaned).strip()
            if newer == cleaned:
                break
            cleaned = newer
        component = key(cleaned)
        if not component.startswith(needle):
            continue
        suffix = component[len(needle):]
        if not suffix:
            return True
        # A real volume appends a volume marker/roman numeral or audiobook
        # annotation.  A different Japanese title appended after the requested
        # series (e.g. 狼と香辛料 狼と羊皮紙II) is a spin-off, not that series.
        if suffix[0].isdigit() or re.match(r"^[ivxlcdm]+(?:完全版|オーディオブック|audiobook|unabridged|$)", suffix, re.I):
            return True
        if suffix.startswith(("完全版", "オーディオブック", "audiobook", "unabridged")):
            return True
    return False


def managed_audiobook_series_conflict(paths: list[str]) -> dict[str, str] | None:
    """Return a confident mismatch for a Pudge-managed audiobook download.

    The outer ``Pudge Audiobooks/<series>/Volume XX`` folders are generated by
    Pudge and therefore cannot validate the torrent's own identity.  Only the
    release-relative remainder is inspected.  We reject only when the expected
    series text actually occurs there but never as a strict title prefix; an
    English-only/opaque filename remains unknown rather than being destroyed.
    """
    for raw_path in paths:
        parts = [part for part in Path(str(raw_path or "")).parts if part]
        try:
            root_i = next(i for i, part in enumerate(parts) if part == "Pudge Audiobooks")
        except StopIteration:
            continue
        if root_i + 3 >= len(parts):
            continue
        expected = parts[root_i + 1]
        volume_i = root_i + 2
        if not re.fullmatch(r"(?i)volume\s+\d{1,3}", parts[volume_i]):
            continue
        remainder_parts = parts[volume_i + 1 :]
        if not remainder_parts:
            continue
        remainder = "/".join(remainder_parts)
        expected_key = re.sub(
            r"[^0-9A-Za-zぁ-ゟ゠-ヿ一-鿿々〆ヶ]+",
            "",
            unicodedata.normalize("NFKC", expected),
        ).casefold()
        remainder_key = re.sub(
            r"[^0-9A-Za-zぁ-ゟ゠-ヿ一-鿿々〆ヶ]+",
            "",
            unicodedata.normalize("NFKC", remainder),
        ).casefold()
        if not expected_key or expected_key not in remainder_key:
            continue
        if audiobook_series_path_matches(remainder, expected):
            continue
        return {"expected_series": expected, "source": remainder}
    return None


def terminate_orphaned_audiobook_players(
    cache_dir: Path,
    *,
    grace_seconds: float = 0.6,
) -> list[int]:
    """Terminate audiobook mpv processes whose owning Pudge PID is gone."""
    if os.name != "posix":
        return []
    try:
        completed = subprocess.run(
            ["ps", "-axo", "uid=,pid=,ppid=,command="],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    try:
        own_uid = os.getuid()
    except AttributeError:
        return []
    ipc_root = (Path(cache_dir).expanduser() / "audiobook-ipc").resolve()
    candidates: list[tuple[int, Path]] = []
    for raw_line in (completed.stdout or "").splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", raw_line)
        if not match:
            continue
        uid, pid, command = int(match.group(1)), int(match.group(2)), match.group(4)
        if uid != own_uid or pid == os.getpid() or "mpv" not in command:
            continue
        ipc_match = re.search(r"--input-ipc-server=(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
        if not ipc_match:
            continue
        ipc_text = next((value for value in ipc_match.groups() if value), "")
        ipc_path = Path(ipc_text).expanduser()
        owner_match = re.fullmatch(r"book-\d+-(\d+)\.sock", ipc_path.name)
        if not owner_match:
            continue
        try:
            if ipc_path.parent.resolve() != ipc_root:
                continue
        except OSError:
            continue
        owner_pid = int(owner_match.group(1))
        try:
            os.kill(owner_pid, 0)
            owner_alive = True
        except ProcessLookupError:
            owner_alive = False
        except PermissionError:
            owner_alive = True
        if owner_alive:
            continue
        candidates.append((pid, ipc_path))

    terminated: list[int] = []
    for pid, _ipc in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        terminated.append(pid)
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    remaining = set(terminated)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except PermissionError:
                remaining.discard(pid)
        if remaining:
            time.sleep(0.04)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for pid, ipc_path in candidates:
        if pid in terminated:
            try:
                ipc_path.unlink(missing_ok=True)
            except OSError:
                pass
    return terminated


def _audiobook_volume(
    value: str,
    *,
    series_title: str = "",
    allow_bare_numeric_volume_dirs: bool = True,
) -> int | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    path_parts = [part.strip() for part in re.split(r"[/\\]+", text) if part.strip()]

    # Prefer explicit volume identity anywhere in the path before considering
    # bare numeric directories.  Collection packs often look like
    # ``狼と香辛料II/01/track.mp3`` or ``Series/02/01/track.mp3``: the closest
    # numeric directory is a chapter/track, not the volume.
    for part in path_parts or [text]:
        for pattern in (
            r"(?i)\b(?:vol(?:ume)?|v)\s*[._ -]*0*(\d{1,3})\b",
            r"第\s*0*(\d{1,3})\s*巻",
            r"0*(\d{1,3})\s*巻",
        ):
            match = re.search(pattern, part)
            if match:
                number = int(match.group(1))
                if 0 < number <= 300:
                    return number

    # Collection filenames frequently carry a canonical bracketed book index,
    # e.g. ``[11] 狼と香辛料XI Side ColorsII``.  That leading index is the
    # volume; the trailing ``II`` belongs to the subtitle and must never turn
    # Volume 11 into Volume 2.  Only trust the bracket when the remainder is an
    # exact-series title, so chapter folders such as ``[02] Chapter 2`` remain
    # chapters rather than volumes.
    if series_title:
        for part in path_parts or [text]:
            indexed = re.match(r"^\s*[\[【(（]\s*0*(\d{1,3})\s*[\]】)）]\s*(.+)$", part)
            if not indexed:
                continue
            number = int(indexed.group(1))
            remainder = indexed.group(2).strip()
            if 0 < number <= 300 and audiobook_series_path_matches(remainder, series_title):
                return number

    # When a series title is known, prefer the numeral attached immediately to
    # that series name over a numeral at the end of a subtitle.  For example:
    # ``狼と香辛料XI Side ColorsII`` -> XI (11), not II (2), and
    # ``狼と香辛料XIX Spring LogII`` -> XIX (19).
    def roman_number(token: str) -> int | None:
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        token = str(token or "").upper()
        total = 0
        previous = 0
        for char in reversed(token):
            amount = values.get(char, 0)
            if not amount:
                return None
            if amount < previous:
                total -= amount
            else:
                total += amount
                previous = amount
        if not (0 < total <= 300):
            return None
        canonical = ""
        remaining = total
        for amount, glyph in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            while remaining >= amount:
                canonical += glyph
                remaining -= amount
        return total if canonical == token else None

    if series_title:
        def compact_title(value: str) -> str:
            normalized = unicodedata.normalize("NFKC", str(value or ""))
            normalized = re.sub(r"^\s*[\[【(（][^\]】)）]{1,100}[\]】)）]\s*", "", normalized).strip()
            normalized = re.sub(r"(?i)\.(?:m4b|m4a|mp3|aac|opus|ogg|flac|wav)$", "", normalized)
            normalized = re.sub(r"\s*\[[A-Z0-9][A-Z0-9._-]{3,}\]\s*$", "", normalized, flags=re.I)
            return re.sub(r"[^0-9A-Za-zぁ-ゟ゠-ヿ一-鿿々〆ヶ]+", "", normalized)

        series_key = compact_title(series_title).casefold()
        if series_key:
            for part in path_parts or [text]:
                component = compact_title(part)
                component_folded = component.casefold()
                if not component_folded.startswith(series_key):
                    continue
                suffix = component[len(compact_title(series_title)):]
                arabic = re.match(r"^(\d{1,3})(?:$|\D)", suffix)
                if arabic:
                    number = int(arabic.group(1))
                    if 0 < number <= 300:
                        return number
                roman = re.match(r"^([IVXLCDM]{1,8})(?:$|[^IVXLCDM])", suffix, flags=re.I)
                if roman:
                    number = roman_number(roman.group(1))
                    if number is not None:
                        return number

    # Japanese audiobook volume names commonly attach an Arabic or Roman
    # numeral directly to the title (狼と香辛料2 / 狼と香辛料II).  Check every
    # path component, not only the final filename, so chapter subdirectories
    # beneath a volume title cannot steal the volume identity.
    def clean_component(part: str) -> str:
        cleaned = re.sub(r"(?i)\.(?:m4b|m4a|mp3|aac|opus|ogg|flac|wav)$", "", part).strip()
        cleaned = re.sub(r"\s*\[[A-Z0-9][A-Z0-9._-]{3,}\]\s*$", "", cleaned, flags=re.I)
        return cleaned

    for part in path_parts or [text]:
        stem_text = clean_component(part)
        suffix = re.search(r"[ぁ-ゟ゠-ヿ一-鿿].*?(\d{1,3})\s*$", stem_text)
        if suffix:
            number = int(suffix.group(1))
            if 0 < number <= 300:
                return number

        roman = re.search(r"[ぁ-ゟ゠-ヿ一-鿿].*?([IVXLCDM]{1,8})\s*$", stem_text, flags=re.I)
        if roman:
            values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
            token = roman.group(1).upper()
            total = 0
            previous = 0
            valid = True
            for char in reversed(token):
                amount = values.get(char, 0)
                if not amount:
                    valid = False
                    break
                if amount < previous:
                    total -= amount
                else:
                    total += amount
                    previous = amount
            if valid and 0 < total <= 50:
                canonical = ""
                remaining = total
                for amount, glyph in ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
                    while remaining >= amount:
                        canonical += glyph
                        remaining -= amount
                if canonical == token:
                    return total

    # A bare numeric directory is too ambiguous to treat as a volume on its
    # own: audiobook packs very often use ``01/02/...`` for chapters or discs.
    # Accept it only when it is the *immediate child* of a path component that
    # identifies the requested series, e.g. ``狼と香辛料/02/01.mp3``.  This is
    # the structural signal that was missing in v155 and caused chapter 02 to
    # be mistaken for Volume 2 in unrelated/single-volume releases.
    def component_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        return re.sub(r"[^0-9A-Za-zぁ-ゟ゠-ヿ一-鿿]+", "", normalized).casefold()

    series_key = component_key(series_title)
    if series_key and len(path_parts) > 2:
        for index, part in enumerate(path_parts[:-2]):
            part_key = component_key(part)
            if not part_key or not (series_key in part_key or part_key in series_key):
                continue
            child = path_parts[index + 1]
            match = re.fullmatch(r"[\[（(]?\s*0*(\d{1,3})\s*[\]）)]?", child)
            if match:
                number = int(match.group(1))
                if 0 < number <= 300:
                    return number

    # Preserve the generic pack parser's historical ability to understand
    # ``Series Collection/02/book.m4b`` layouts, but let callers disable this
    # fallback when the release itself is a single explicit volume.  That is
    # the crucial distinction for Nyaa: in a Vol. 10 release, a root ``02``
    # directory is a chapter/disc, not Volume 2.
    if allow_bare_numeric_volume_dirs and len(path_parts) > 1:
        for part in path_parts[:-1]:
            match = re.fullmatch(r"(?i)(?:vol(?:ume)?[ ._-]*)?0*(\d{1,3})", part)
            if match:
                number = int(match.group(1))
                if 0 < number <= 300:
                    return number
    return None



def audiobook_torrent_files_from_payload(payload: bytes) -> list[dict[str, Any]]:
    """Extract the file table from a .torrent payload without adding it to a client.

    This intentionally implements only the small bencode subset needed for torrent
    metadata.  It lets the Nyaa audiobook picker inspect collection contents without
    starting/reserving a multi-volume torrent first.
    """
    data = bytes(payload or b"")
    if not data or len(data) > 12 * 1024 * 1024 or not data.startswith(b"d"):
        return []
    pos = 0
    items = 0

    def parse(depth: int = 0):
        nonlocal pos, items
        if depth > 32 or pos >= len(data):
            raise ValueError("invalid bencode")
        items += 1
        if items > 100_000:
            raise ValueError("torrent metadata too large")
        token = data[pos : pos + 1]
        if token == b"i":
            pos += 1
            end = data.find(b"e", pos)
            if end < 0:
                raise ValueError("invalid integer")
            value = int(data[pos:end])
            pos = end + 1
            return value
        if token == b"l":
            pos += 1
            values = []
            while pos < len(data) and data[pos : pos + 1] != b"e":
                values.append(parse(depth + 1))
            if pos >= len(data):
                raise ValueError("unterminated list")
            pos += 1
            return values
        if token == b"d":
            pos += 1
            values = {}
            while pos < len(data) and data[pos : pos + 1] != b"e":
                key = parse(depth + 1)
                if not isinstance(key, bytes):
                    raise ValueError("invalid dictionary key")
                values[key] = parse(depth + 1)
            if pos >= len(data):
                raise ValueError("unterminated dictionary")
            pos += 1
            return values
        if token.isdigit():
            colon = data.find(b":", pos)
            if colon < 0:
                raise ValueError("invalid byte string")
            size = int(data[pos:colon])
            if size < 0 or size > len(data):
                raise ValueError("invalid byte string length")
            pos = colon + 1
            end = pos + size
            if end > len(data):
                raise ValueError("truncated byte string")
            value = data[pos:end]
            pos = end
            return value
        raise ValueError("unsupported bencode token")

    def text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    try:
        root = parse()
    except (ValueError, TypeError, OverflowError):
        return []
    if not isinstance(root, dict):
        return []
    info = root.get(b"info")
    if not isinstance(info, dict):
        return []
    rows: list[dict[str, Any]] = []
    raw_files = info.get(b"files")
    if isinstance(raw_files, list):
        for index, raw in enumerate(raw_files):
            if not isinstance(raw, dict):
                continue
            parts = raw.get(b"path.utf-8") or raw.get(b"path") or []
            if not isinstance(parts, list):
                continue
            name = "/".join(text(part).strip("/\\") for part in parts if text(part).strip("/\\"))
            if not name:
                continue
            try:
                size = max(0, int(raw.get(b"length") or 0))
            except (TypeError, ValueError):
                size = 0
            rows.append({"index": index, "name": name, "size": size, "priority": 0})
    else:
        name = text(info.get(b"name.utf-8") or info.get(b"name")).strip()
        if name:
            try:
                size = max(0, int(info.get(b"length") or 0))
            except (TypeError, ValueError):
                size = 0
            rows.append({"index": 0, "name": name, "size": size, "priority": 0})
    return rows


def audiobook_torrent_pack_plan(
    files: list[dict[str, Any]],
    *,
    series_title: str = "",
    allow_bare_numeric_volume_dirs: bool = True,
) -> dict[str, Any]:
    """Normalize torrent file metadata and group selectively downloadable audio by volume.

    The returned ``file_ids`` are the exact provider indices accepted by both
    qBittorrent and Pudge's aria2 adapter.  This is infrastructure only: it does
    not change file priorities or start downloads.
    """
    normalized: list[dict[str, Any]] = []
    for fallback_index, raw in enumerate(files or []):
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("index", raw.get("file_index", raw.get("id", fallback_index)))
        try:
            file_id = int(raw_id)
        except (TypeError, ValueError):
            file_id = fallback_index
        name = str(raw.get("name") or raw.get("path") or "").strip()
        if not name:
            continue
        try:
            size = max(0, int(raw.get("size", raw.get("length", 0)) or 0))
        except (TypeError, ValueError):
            size = 0
        suffix = Path(name).suffix.casefold()
        volume = _audiobook_volume(
            name,
            series_title=series_title,
            allow_bare_numeric_volume_dirs=allow_bare_numeric_volume_dirs,
        )
        normalized.append({
            "file_id": file_id,
            "name": name,
            "size_bytes": size,
            "priority": int(raw.get("priority") or 0),
            "extension": suffix,
            "volume": volume,
            "is_audio": suffix in AUDIOBOOK_EXTENSIONS,
            "is_archive": suffix in {".zip", ".rar", ".7z"},
        })

    payload_files = [row for row in normalized if row["is_audio"] or row["is_archive"]]
    archives = [row for row in payload_files if row["is_archive"]]
    audio = [row for row in payload_files if row["is_audio"]]
    if len(archives) == 1 and not audio:
        return {
            "selective": False,
            "reason": "single_archive",
            "files": normalized,
            "volumes": [],
            "ungrouped_audio": [],
            "archive_file_ids": [archives[0]["file_id"]],
        }

    grouped: dict[int, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for row in audio:
        volume = row.get("volume")
        if isinstance(volume, int) and volume > 0:
            grouped.setdefault(volume, []).append(row)
        else:
            ungrouped.append(row)
    volumes = []
    for volume, rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda row: (str(row["name"]).casefold(), int(row["file_id"])))
        volumes.append({
            "volume": volume,
            "file_ids": [int(row["file_id"]) for row in rows],
            "size_bytes": sum(int(row["size_bytes"]) for row in rows),
            "files": rows,
        })
    return {
        "selective": bool(audio and grouped and not archives),
        "reason": "ok" if audio and grouped and not archives else (
            "archive_mixed_with_audio" if archives else "volume_not_detected"
        ),
        "files": normalized,
        "volumes": volumes,
        "ungrouped_audio": sorted(ungrouped, key=lambda row: str(row["name"]).casefold()),
        "archive_file_ids": [int(row["file_id"]) for row in archives],
    }


def _audiobook_path_label(value: str) -> str:
    path = Path(str(value or ""))
    name = path.name
    return Path(name).stem if Path(name).suffix else name


def _audiobook_title_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = re.sub(r"^\s*\[[^\]]{1,80}\]\s*", " ", text)
    # Source/store suffixes are metadata, not title identity.  Keep author
    # brackets intact, but strip well-known audiobook provider annotations.
    text = re.sub(
        r"(?i)\[(?:audiobook\.jp|audible|amazon|asin|storytel|kikubon|listen|audio)[^\]]{0,100}\]",
        " ",
        text,
    )
    text = re.sub(r"(?i)(?:～|~)?\s*(?:完全版\s*)?(?:オーディオブック|朗読版|audio\s*book|audiobook)", " ", text)
    text = re.sub(
        r"(?i)\b(?:light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\b",
        " ",
        text,
    )
    text = re.sub(r"第\s*0*\d{1,3}\s*巻|0*\d{1,3}\s*巻", " ", text)
    text = re.sub(r"\.(?:m4b|m4a|mp3|aac|opus|ogg|flac|wav)$", "", text, flags=re.I)
    return re.sub(r"[^\wぁ-ゟ゠-ヿ一-鿿]+", "", text).casefold()


def _audiobook_title_match_score(left: str, right: str) -> float:
    left_key, right_key = _audiobook_title_key(left), _audiobook_title_key(right)
    if not left_key or not right_key:
        return 0.0
    score = float(fuzz.ratio(left_key, right_key))
    shorter, longer = sorted((left_key, right_key), key=len)
    # Strong containment handles legitimate title prefixes such as
    # また、同じ夢を見ていた -> 同じ夢を見ていた without requiring AniList.
    if len(shorter) >= 6 and shorter in longer:
        score = max(score, 98.0 - min(5.0, (len(longer) - len(shorter)) * 0.5))
    return score


def _roman_volume_value(token: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    token = str(token or "").upper()
    if not token:
        return None
    total = 0
    previous = 0
    for char in reversed(token):
        amount = values.get(char)
        if amount is None:
            return None
        if amount < previous:
            total -= amount
        else:
            total += amount
            previous = amount
    if not (0 < total <= 300):
        return None
    canonical = ""
    remaining = total
    for amount, glyph in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while remaining >= amount:
            canonical += glyph
            remaining -= amount
    return total if canonical == token else None


def _audiobook_series_title(value: str, *, volume: int | None = None) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).strip()
    text = re.sub(r"^\s*\[[^\]]{1,80}\]\s*", "", text)
    text = re.sub(r"(?i)\[(?:audiobook\.jp|audible|amazon|asin|storytel|kikubon|listen|audio)[^\]]{0,100}\]", " ", text)
    text = re.sub(r"(?i)(?:～|~)?\s*(?:完全版\s*)?(?:オーディオブック|朗読版|audio\s*book|audiobook)", " ", text)
    text = re.sub(r"(?i)\b(?:light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\b", " ", text)
    text = re.sub(r"第\s*0*\d{1,3}\s*巻|0*\d{1,3}\s*巻", " ", text)
    text = re.sub(r"\.(?:m4b|m4a|mp3|aac|opus|ogg|flac|wav)$", "", text, flags=re.I)
    text = re.sub(r"\s*\[[A-Z0-9]{6,20}\]\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ～~-/")

    # Light-novel links often expose titles such as ``狼と香辛料 02`` while
    # standalone audiobook folders use ``狼と香辛料II``.  Once the volume is
    # known, strip only a matching terminal bare marker so both resolve to one
    # stable library series.  Requiring Japanese text avoids turning generic
    # numeric/roman titles into accidental series names.
    if volume and re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", text):
        arabic = re.search(r"\s*0*(\d{1,3})\s*$", text)
        if arabic and int(arabic.group(1)) == int(volume):
            text = text[: arabic.start()].rstrip(" ～~-/")
        else:
            roman = re.search(r"\s*([IVXLCDM]{1,8})\s*$", text, flags=re.I)
            if roman and _roman_volume_value(roman.group(1)) == int(volume):
                text = text[: roman.start()].rstrip(" ～~-/")
    return text


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


class AudiobookService:
    """Resumable mpv audiobook playback with chapters, bookmarks and paired reading."""

    def __init__(
        self,
        database: Database,
        *,
        ffprobe: str,
        mpv: str,
        cache_dir: Path,
        cover_cache_dir: Path | None = None,
        ffmpeg: str = "ffmpeg",
        python: str | None = None,
        stt_model: str = "mlx-community/whisper-tiny",
        job_center: Any | None = None,
        work_scheduler: Any | None = None,
    ) -> None:
        self.db = database
        self.ffprobe = ffprobe
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.mpv = mpv
        self.python = str(python or os.getenv("PUDGE_PYTHON", "").strip() or sys.executable)
        self.stt_model = str(stt_model or "mlx-community/whisper-tiny")
        self.cache_dir = Path(cache_dir)
        self.cover_cache_dir = Path(cover_cache_dir) if cover_cache_dir is not None else self.cache_dir / "covers"
        self.cover_cache_dir.mkdir(parents=True, exist_ok=True)
        self.job_center = job_center
        self.work_scheduler = work_scheduler
        self._probe_cache = MetadataCache(self.cache_dir, "audiobook-probe", schema="v2")
        self._players: dict[int, subprocess.Popen[Any]] = {}
        self._ipc_paths: dict[int, Path] = {}
        self._playback_sessions: dict[int, str] = {}
        # Serialize playback ownership transitions.  A previous monitor may
        # finish after a new player has already started for the same book.
        self._playback_lock = threading.RLock()
        self._last_positions: dict[int, float] = {}
        self._last_motion_at: dict[int, float] = {}
        self._startup_targets: dict[int, dict[str, float | int]] = {}
        self._speeds: dict[int, float] = {}
        self._sleep_deadlines: dict[int, float] = {}
        self._sleep_chapter_ends: dict[int, float] = {}
        self._alignment_jobs: dict[int, dict[str, Any]] = {}
        self._alignment_processes: dict[int, subprocess.Popen[Any]] = {}
        self._alignment_generations: dict[int, int] = {}
        self._alignment_cancel_events: dict[int, threading.Event] = {}
        self._transcription_jobs: dict[int, dict[str, Any]] = {}
        self._transcription_processes: dict[int, subprocess.Popen[Any]] = {}
        self._transcription_events: dict[int, threading.Event] = {}
        self._transcription_cancel_events: dict[int, threading.Event] = {}
        self._transcription_queue: list[int] = []
        self._transcription_dispatcher: threading.Thread | None = None
        self._cover_queue: list[Path] = []
        self._cover_dispatcher: threading.Thread | None = None
        self._metadata_probe_pending: set[int] = set()
        self._lock = threading.Lock()
        self._closed_event = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_threads: list[threading.Thread] = []
        self._tempo_filter_args_cache: tuple[str, ...] | None = None
        self._fingerprint_cache: dict[tuple[str, int, int], tuple[float, str]] = {}
        self._alignment_payload_cache: dict[tuple[int, int, str], dict[str, Any]] = {}
        self._alignment_report_cache: dict[tuple[int, int, str], dict[str, Any] | None] = {}
        self._reader_parse_alignment_refresh_seen: set[tuple[int, int, str]] = set()
        # v134: upgrades used to leave pre-chunking MLX workers orphaned under launchd.
        # Clean only the unmistakable legacy file-0001.json workers; current chunk workers survive.
        self._legacy_stt_workers_terminated = terminate_legacy_audiobook_stt_workers()
        self._orphan_players_terminated = terminate_orphaned_audiobook_players(self.cache_dir)

    def _tracked_thread(
        self,
        *,
        target: Any,
        name: str,
        args: tuple[Any, ...] = (),
    ) -> threading.Thread | None:
        if self._closed_event.is_set():
            return None

        def runner() -> None:
            try:
                target(*args)
            finally:
                current = threading.current_thread()
                with self._worker_lock:
                    self._worker_threads = [
                        thread for thread in self._worker_threads if thread is not current
                    ]

        thread = threading.Thread(target=runner, name=name, daemon=True)
        with self._worker_lock:
            if self._closed_event.is_set():
                return None
            self._worker_threads.append(thread)
        return thread

    @staticmethod
    def _worker_alive(thread: Any) -> bool:
        checker = getattr(thread, "is_alive", None)
        return bool(checker()) if callable(checker) else False

    def active_worker_names(self) -> list[str]:
        with self._worker_lock:
            return sorted(
                str(getattr(thread, "name", "audiobook-worker"))
                for thread in self._worker_threads
                if self._worker_alive(thread)
            )

    def close(self, *, timeout: float = 5.0) -> list[str]:
        self._closed_event.set()
        with self._lock:
            self._transcription_queue.clear()
            cancel_events = [
                *self._transcription_cancel_events.values(),
                *self._alignment_cancel_events.values(),
            ]
            events = list(self._transcription_events.values())
            processes = [
                *self._alignment_processes.values(),
                *self._transcription_processes.values(),
            ]
            for ln_book_id, job in list(self._alignment_jobs.items()):
                if str(job.get("status") or "") in {"queued", "transcribing", "aligning"}:
                    self._alignment_jobs[ln_book_id] = {
                        **job,
                        "status": "cancelled",
                        "ready": False,
                        "error": "",
                    }
            for job_id, job in list(self._transcription_jobs.items()):
                if str(job.get("status") or "") in {"queued", "transcribing"}:
                    self._transcription_jobs[job_id] = {
                        **job,
                        "status": "cancelled",
                        "ready": False,
                        "error": "",
                    }
        for cancel_event in cancel_events:
            cancel_event.set()
        for event in events:
            event.set()
        self.stop_all()
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._worker_lock:
                workers = [
                    thread for thread in self._worker_threads
                    if self._worker_alive(thread) and thread is not threading.current_thread()
                ]
            if not workers:
                break
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            for thread in workers:
                thread.join(min(0.25, remaining))

        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        return self.active_worker_names()

    def _tempo_filter_args(self) -> list[str]:
        """Use mpv's Chromium-derived pitch-preserving tempo path.

        mpv enables ``scaletempo2`` automatically when speed changes while
        audio pitch correction is on. Its default ``scaletempo2`` parameters
        are the Chromium defaults, which gives a more browser/YouTube-like
        result than forcing a custom WSOLA window here.
        """
        with self._lock:
            cached = self._tempo_filter_args_cache
        if cached is not None:
            return list(cached)

        selected = ("--audio-pitch-correction=yes",)
        with self._lock:
            self._tempo_filter_args_cache = selected
        return list(selected)

    def _probe(self, path: Path, *, timeout: float = 30.0) -> tuple[float, list[dict[str, Any]]]:
        stat = path.stat()
        key = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        cached = self._probe_cache.get(key, ttl_seconds=180 * 24 * 3600)
        if isinstance(cached, dict):
            return float(cached.get("duration") or 0.0), [
                dict(item) for item in cached.get("chapters") or [] if isinstance(item, dict)
            ]

        completed = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_format", "-show_chapters", "-of", "json", str(path)],
            text=True,
            capture_output=True,
            timeout=max(0.5, float(timeout)),
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "ffprobe could not read this audiobook")
        payload = json.loads(completed.stdout or "{}")
        try:
            duration = max(0.0, float(payload.get("format", {}).get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        chapters: list[dict[str, Any]] = []
        for index, chapter in enumerate(payload.get("chapters") or []):
            try:
                start = float(chapter.get("start_time") or 0.0)
                end = float(chapter.get("end_time") or start)
            except (TypeError, ValueError):
                continue
            tags = chapter.get("tags") if isinstance(chapter.get("tags"), dict) else {}
            chapters.append(
                {
                    "index": index,
                    "title": str(tags.get("title") or f"Chapter {index + 1}"),
                    "start": start,
                    "end": end,
                }
            )
        self._probe_cache.put(key, {"duration": duration, "chapters": chapters})
        self._probe_cache.prune(older_than_seconds=365 * 24 * 3600, max_entries=2000)
        return duration, chapters

    def _embedded_cover_paths(self, path: Path) -> tuple[Path, Path, Path] | None:
        try:
            path = path.expanduser().resolve()
            stat = path.stat()
        except OSError:
            return None
        digest = hashlib.sha256(
            f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24]
        return (
            path,
            self.cover_cache_dir / f"audiobook-{digest}.jpg",
            self.cover_cache_dir / f"audiobook-{digest}.none",
        )

    def _embedded_cover_url(self, path: Path, *, extract: bool = True) -> str:
        """Return a cached embedded-art URL and optionally extract it."""
        paths = self._embedded_cover_paths(path)
        if paths is None:
            return ""
        source, target, missing = paths
        if target.is_file() and target.stat().st_size > 0:
            return f"covers/{target.name}"
        if missing.exists() or not extract:
            return ""
        try:
            completed = subprocess.run(
                [
                    self.ffmpeg,
                    "-v", "error",
                    "-y",
                    "-i", str(source),
                    "-map", "0:v:0",
                    "-frames:v", "1",
                    str(target),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            missing.unlink(missing_ok=True)
            return f"covers/{target.name}"
        target.unlink(missing_ok=True)
        try:
            missing.touch()
        except OSError:
            pass
        return ""

    def _queue_embedded_cover(self, path: Path) -> None:
        paths = self._embedded_cover_paths(path)
        if paths is None:
            return
        source, target, missing = paths
        if target.is_file() or missing.exists():
            return
        with self._lock:
            if source not in self._cover_queue:
                self._cover_queue.append(source)
            current = self._cover_dispatcher
            if current is not None and current.is_alive():
                return
            thread = self._tracked_thread(
                target=self._cover_dispatch_loop,
                name="audiobook-cover-dispatcher",
            )
            if thread is None:
                return
            self._cover_dispatcher = thread
        thread.start()

    def _cover_dispatch_loop(self) -> None:
        while True:
            with self._lock:
                if not self._cover_queue:
                    if self._cover_dispatcher is threading.current_thread():
                        self._cover_dispatcher = None
                    return
                source = self._cover_queue.pop(0)
            self._embedded_cover_url(source, extract=True)

    def _book_embedded_cover_url(self, book_id: int) -> str:
        rows = self._file_rows(int(book_id))
        if not rows:
            return ""
        source = Path(str(rows[0].get("path") or ""))
        if source.suffix.casefold() not in {".m4b", ".m4a"}:
            return ""
        cached = self._embedded_cover_url(source, extract=False)
        if cached:
            return cached
        self._queue_embedded_cover(source)
        return ""

    def _book_embedded_cover_pending(self, book_id: int) -> bool:
        rows = self._file_rows(int(book_id))
        if not rows:
            return False
        source = Path(str(rows[0].get("path") or ""))
        if source.suffix.casefold() not in {".m4b", ".m4a"}:
            return False
        paths = self._embedded_cover_paths(source)
        if paths is None:
            return False
        _source, target, missing = paths
        return not target.is_file() and not missing.exists()

    def _folder_files(self, folder: Path) -> list[Path]:
        rows = [
            path.resolve()
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIOBOOK_EXTENSIONS
        ]
        rows.sort(key=lambda path: _natural_key(str(path.relative_to(folder))))
        return rows

    @staticmethod
    def _looks_like_disc_folder(path: Path) -> bool:
        name = unicodedata.normalize("NFKC", path.name).strip().casefold()
        return bool(re.match(r"^(?:cd|disc|disk|part|track|chapter|chap|volume|vol)[ ._-]*\d+(?:\D.*)?$", name))

    def folder_import_targets(self, folder: Path) -> list[Path]:
        """Resolve a selected folder into one or several audiobook roots.

        A folder with audio files directly inside is one audiobook.  A library
        folder whose immediate children each contain audio is a collection and
        imports one audiobook per child.  Disc/part subfolders are deliberately
        kept together as one audiobook.
        """
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Audiobook folder does not exist")
        try:
            direct_audio = [
                item for item in folder.iterdir()
                if item.is_file() and item.suffix.casefold() in AUDIOBOOK_EXTENSIONS
            ]
            children = [item.resolve() for item in folder.iterdir() if item.is_dir() and not item.name.startswith(".")]
        except OSError as exc:
            raise ValueError(f"Could not read audiobook folder: {exc}") from exc
        if direct_audio:
            return [folder]
        groups = [child for child in children if self._folder_files(child)]
        groups.sort(key=lambda path: _natural_key(path.name))
        if len(groups) >= 2 and not all(self._looks_like_disc_folder(path) for path in groups):
            return groups
        return [folder]

    def import_folder_collection(
        self,
        folder: Path,
        *,
        auto_link: bool = True,
        prepare_transcription: bool = True,
    ) -> list[dict[str, Any]]:
        return [
            self.import_folder(
                target,
                auto_link=auto_link,
                prepare_transcription=prepare_transcription,
            )
            for target in self.folder_import_targets(folder)
        ]

    def _upsert(
        self,
        *,
        path: Path,
        title: str,
        duration: float,
        files: list[dict[str, Any]],
        chapters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at)
                VALUES(?,?,?,0,0,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    title=excluded.title,duration=excluded.duration,updated_at=excluded.updated_at
                """,
                (str(path), title, float(duration), now, now),
            )
            row = conn.execute("SELECT * FROM audiobooks WHERE path=?", (str(path),)).fetchone()
            assert row is not None
            book_id = int(row["id"])
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book_id,))
            conn.executemany(
                """
                INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end)
                VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        book_id,
                        int(item["index"]),
                        str(item["path"]),
                        str(item["title"]),
                        float(item["duration"]),
                        float(item["start"]),
                        float(item["end"]),
                    )
                    for item in files
                ],
            )
            conn.executemany(
                """
                INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end)
                VALUES(?,?,?,?,?)
                """,
                [
                    (
                        book_id,
                        int(chapter["index"]),
                        str(chapter["title"]),
                        float(chapter["start"]),
                        float(chapter["end"]),
                    )
                    for chapter in chapters
                ],
            )
        return self.book(book_id)

    def _queue_metadata_refresh(
        self,
        book_id: int,
        path: Path,
        *,
        title_override: str | None = None,
        auto_link: bool = True,
        prepare_transcription: bool = True,
    ) -> None:
        book_id = int(book_id)
        with self._lock:
            if book_id in self._metadata_probe_pending:
                return
            self._metadata_probe_pending.add(book_id)
        thread = self._tracked_thread(
            target=self._metadata_refresh_worker,
            args=(book_id, path, title_override, auto_link, prepare_transcription),
            name=f"audiobook-metadata-{book_id}",
        )
        if thread is not None:
            thread.start()

    def _metadata_refresh_worker(
        self,
        book_id: int,
        path: Path,
        title_override: str | None = None,
        auto_link: bool = True,
        prepare_transcription: bool = True,
    ) -> None:
        try:
            duration, embedded = self._probe(path, timeout=180.0)
            files = [{
                "index": 0, "path": str(path), "title": path.stem,
                "duration": duration, "start": 0.0, "end": duration,
            }]
            chapters = embedded or [{
                "index": 0, "title": path.stem, "start": 0.0, "end": duration,
            }]
            self._upsert(
                path=path,
                title=str(title_override or path.stem),
                duration=duration,
                files=files,
                chapters=chapters,
            )
            if auto_link:
                self.auto_link_audiobook(int(book_id))
            if duration > 0 and prepare_transcription:
                self.prepare_transcription(int(book_id))
        except Exception as exc:
            self._set_transcription_job(int(book_id), {
                "status": "idle", "ready": False,
                "metadata_error": str(exc),
            })
        finally:
            with self._lock:
                self._metadata_probe_pending.discard(int(book_id))

    def import_file(
        self,
        path: Path,
        *,
        title_override: str | None = None,
        auto_link: bool = True,
        prepare_transcription: bool = True,
    ) -> dict[str, Any]:
        path = path.expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in AUDIOBOOK_EXTENSIONS:
            raise ValueError("Unsupported audiobook format")
        metadata_pending = False
        try:
            # Local files normally probe in milliseconds.  iCloud placeholders
            # must not freeze the UI for the old 30-second ffprobe timeout.
            # Keep compatibility with tests/plugins that monkeypatch the old
            # one-argument _probe(path) contract.
            try:
                duration, embedded = self._probe(path, timeout=3.0)
            except TypeError as exc:
                if "unexpected keyword argument 'timeout'" not in str(exc):
                    raise
                duration, embedded = self._probe(path)
        except subprocess.TimeoutExpired:
            duration, embedded, metadata_pending = 0.0, [], True
        files = [{
            "index": 0, "path": str(path), "title": path.stem,
            "duration": duration, "start": 0.0, "end": duration,
        }]
        chapters = embedded or [{"index": 0, "title": path.stem, "start": 0.0, "end": duration}]
        book = self._upsert(
            path=path,
            title=str(title_override or path.stem),
            duration=duration,
            files=files,
            chapters=chapters,
        )
        book_id = int(book["id"])
        if auto_link:
            self.auto_link_audiobook(book_id)
        if metadata_pending:
            if title_override is None and auto_link:
                # Preserve the legacy two-argument hook contract used by tests
                # and external integrations for ordinary imports.
                self._queue_metadata_refresh(book_id, path)
            else:
                self._queue_metadata_refresh(
                    book_id,
                    path,
                    title_override=title_override,
                    auto_link=auto_link,
                    prepare_transcription=prepare_transcription,
                )
        elif prepare_transcription:
            self.prepare_transcription(book_id)
        return self.book(book_id)

    def import_folder(
        self,
        folder: Path,
        *,
        auto_link: bool = True,
        prepare_transcription: bool = True,
        title_override: str | None = None,
    ) -> dict[str, Any]:
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Audiobook folder does not exist")
        paths = self._folder_files(folder)
        if not paths:
            raise ValueError("No supported audio files found in this folder")
        files: list[dict[str, Any]] = []
        chapters: list[dict[str, Any]] = []
        cursor = 0.0
        chapter_index = 0
        for index, path in enumerate(paths):
            duration, embedded = self._probe(path)
            start = cursor
            end = start + max(0.0, duration)
            files.append(
                {
                    "index": index,
                    "path": str(path),
                    "title": path.stem,
                    "duration": duration,
                    "start": start,
                    "end": end,
                }
            )
            if embedded:
                for chapter in embedded:
                    local_start = max(0.0, float(chapter.get("start") or 0.0))
                    local_end = max(local_start, float(chapter.get("end") or local_start))
                    chapters.append(
                        {
                            "index": chapter_index,
                            "title": str(chapter.get("title") or f"Chapter {chapter_index + 1}"),
                            "start": start + min(local_start, max(0.0, duration)),
                            "end": start + min(local_end, max(0.0, duration)),
                        }
                    )
                    chapter_index += 1
            else:
                chapters.append({"index": chapter_index, "title": path.stem, "start": start, "end": end})
                chapter_index += 1
            cursor = end
        book = self._upsert(
            path=folder,
            title=str(title_override or folder.name),
            duration=cursor,
            files=files,
            chapters=chapters,
        )
        if auto_link:
            self.auto_link_audiobook(int(book["id"]))
        if prepare_transcription:
            self.prepare_transcription(int(book["id"]))
        return self.book(int(book["id"]))

    def _file_rows(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audiobook_files WHERE book_id=? ORDER BY file_index", (int(book_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_playback_position(self, book_id: int, position: float) -> None:
        book_id = int(book_id)
        value = float(position)
        now = time.monotonic()
        with self._lock:
            previous = self._last_positions.get(book_id)
            self._last_positions[book_id] = value
            if (
                previous is None
                or abs(value - float(previous)) >= _PLAYBACK_MOTION_EPSILON
            ):
                self._last_motion_at[book_id] = now

    def is_playing(self, book_id: int) -> bool:
        with self._lock:
            process = self._players.get(int(book_id))
            return bool(process is not None and process.poll() is None)

    def is_paused(self, book_id: int) -> bool:
        """Return mpv's explicit pause state.

        A short period without position movement is normal while mpv switches
        files in an audiobook playlist.  It must not be treated as a user pause.
        """

        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None or ipc_path is None:
            return False
        return self._ipc_get(ipc_path, "pause") is True

    def is_playback_active(self, book_id: int) -> bool:
        """Return whether mpv is running and actively advancing audio."""

        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None:
            return False
        if ipc_path is None:
            return True

        if self.is_paused(book_id):
            return False
        idle = self._ipc_get(ipc_path, "idle-active")
        if idle is True:
            return False

        with self._lock:
            last_motion_at = getattr(self, "_last_motion_at", {}).get(book_id)
        if (
            last_motion_at is not None
            and time.monotonic() - float(last_motion_at)
            > _PLAYBACK_STALL_SECONDS
        ):
            return False
        return True

    @staticmethod
    def _chapter_for_position(chapters: list[dict[str, Any]], position: float) -> dict[str, Any] | None:
        if not chapters:
            return None
        return next(
            (
                chapter
                for chapter in chapters
                if float(chapter["start"]) <= position < float(chapter["end"])
            ),
            chapters[-1],
        )

    def _bookmarks(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audiobook_bookmarks WHERE book_id=? ORDER BY sort_order,position,id", (int(book_id),)
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "position": float(row["position"]),
                "title": str(row["title"] or ""),
                "sort_order": int(row["sort_order"] or 0),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def _book_display_position(
        self,
        book_id: int,
        *,
        persisted: float,
        duration: float,
    ) -> float:
        """Expose monitor-fed mpv position without forcing another IPC read."""

        book_id = int(book_id)
        value = float(persisted or 0.0)
        with self._lock:
            process = self._players.get(book_id)
            live_position = self._last_positions.get(book_id)
        if process is not None and process.poll() is None and live_position is not None:
            value = float(live_position)
        value = max(0.0, value)
        limit = max(0.0, float(duration or 0.0))
        return min(value, limit) if limit > 0 else value

    def book(self, book_id: int, *, include_transcription: bool = True) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
            chapters = conn.execute(
                "SELECT * FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index", (int(book_id),)
            ).fetchall()
            file_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM audiobook_files WHERE book_id=?", (int(book_id),)
                ).fetchone()[0]
            )
            identity = conn.execute(
                "SELECT * FROM media_identities WHERE kind='audiobook' AND local_id=?",
                (int(book_id),),
            ).fetchone()
            has_novels = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone()
            linked_novel = (
                conn.execute(
                    "SELECT b.id,b.title,b.volume,b.anilist_id,b.cover_url FROM reading_audio_links l "
                    "JOIN ln_books b ON b.id=l.ln_book_id WHERE l.audiobook_id=? "
                    "ORDER BY l.updated_at DESC LIMIT 1",
                    (int(book_id),),
                ).fetchone()
                if has_novels is not None
                else None
            )
        if row is None:
            raise KeyError(f"Unknown audiobook id={book_id}")
        chapter_payload = [
            {
                "index": int(chapter["chapter_index"]),
                "title": str(chapter["title"]),
                "start": float(chapter["start"] or 0.0),
                "end": float(chapter["end"] or 0.0),
            }
            for chapter in chapters
        ]
        position = self._book_display_position(
            int(book_id),
            persisted=float(row["position"] or 0.0),
            duration=float(row["duration"] or 0.0),
        )
        player_running = self.is_playing(int(book_id))
        paused = self.is_paused(int(book_id)) if player_running else False
        current_chapter = self._chapter_for_position(chapter_payload, position)
        book_path = Path(str(row["path"]))
        tts_generated = book_path.is_dir() and (book_path / ".pudge-audiobook-profile.json").is_file()
        with self._lock:
            deadline = self._sleep_deadlines.get(int(book_id))
            chapter_end = self._sleep_chapter_ends.get(int(book_id))
        external_cover = str(
            (linked_novel["cover_url"] if linked_novel is not None else "")
            or (identity["cover_url"] if identity is not None else "")
            or ""
        )
        embedded_cover = "" if external_cover else self._book_embedded_cover_url(int(book_id))
        cover_pending = bool(not external_cover and not embedded_cover and self._book_embedded_cover_pending(int(book_id)))
        linked_volume = (
            int(linked_novel["volume"] or 0)
            if linked_novel is not None and linked_novel["volume"] is not None
            else 0
        )
        inferred_volume = linked_volume or (
            _audiobook_volume(str(row["title"]))
            or _audiobook_volume(str(row["path"]))
            or _audiobook_volume(_audiobook_path_label(str(row["path"])))
            or 0
        )
        series_source = str(linked_novel["title"] if linked_novel is not None else row["title"])
        series_title = _audiobook_series_title(series_source, volume=inferred_volume) or str(row["title"])
        return {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "title": str(row["title"]),
            "duration": float(row["duration"] or 0.0),
            "position": position,
            "finished": bool(row["finished"]),
            "playing": player_running and not paused,
            "player_running": player_running,
            "paused": paused,
            "speed": float(self._speeds.get(int(book_id), row["speed"] or 1.0)),
            "multi_file": Path(str(row["path"])).is_dir() or file_count > 1,
            "file_count": file_count,
            "tts_generated": tts_generated,
            "chapters": chapter_payload,
            "current_chapter": current_chapter,
            "bookmarks": self._bookmarks(int(book_id)),
            "sleep_timer_seconds": max(0, round(deadline - time.monotonic())) if deadline else None,
            "sleep_at_chapter_end": chapter_end is not None,
            "transcription": self.transcription_status(int(book_id)) if include_transcription else {"status": "unknown", "ready": False},
            "anilist_id": int(identity["anilist_id"]) if identity is not None else (
                int(linked_novel["anilist_id"]) if linked_novel is not None and linked_novel["anilist_id"] is not None else None
            ),
            "anilist_title": str(identity["title"] or "") if identity is not None else (
                str(linked_novel["title"] or "") if linked_novel is not None else ""
            ),
            "anilist_site_url": str(identity["site_url"] or "") if identity is not None else (
                f"https://anilist.co/manga/{int(linked_novel['anilist_id'])}"
                if linked_novel is not None and linked_novel["anilist_id"] is not None else ""
            ),
            "linked_light_novel": dict(linked_novel) if linked_novel is not None else None,
            "series_title": series_title,
            "series_key": _audiobook_title_key(series_title),
            "volume": int(inferred_volume),
            "cover_url": external_cover or embedded_cover,
            "cover_pending": cover_pending,
            "metadata_pending": int(book_id) in self._metadata_probe_pending,
        }

    def auto_link_audiobook(self, audiobook_id: int) -> dict[str, Any] | None:
        """Link an unambiguous local LN/audiobook pair and share its AniList identity."""

        audiobook_id = int(audiobook_id)
        self._last_auto_link_reason = "starting"
        # Keep discovery, ambiguity checks, and occupancy checks on one SQLite
        # snapshot.  Opening a fresh connection between those phases made the
        # AniList-free title linker unnecessarily timing-sensitive while a LN
        # import and audiobook import were completing close together.
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone() is None:
                self._last_auto_link_reason = "no-ln-table"
                return None
            audio = conn.execute(
                "SELECT id,path,title FROM audiobooks WHERE id=?", (audiobook_id,)
            ).fetchone()
            if audio is None:
                self._last_auto_link_reason = "no-audio"
                return None
            existing = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? LIMIT 1",
                (audiobook_id,),
            ).fetchone()
            if existing is not None:
                self._last_auto_link_reason = "already-linked"
                return {"ln_book_id": int(existing["ln_book_id"]), "audiobook_id": audiobook_id}
            identity = conn.execute(
                "SELECT * FROM media_identities WHERE kind='audiobook' AND local_id=?",
                (audiobook_id,),
            ).fetchone()
            novels = conn.execute(
                "SELECT id,title,file_path,volume,anilist_id,cover_url FROM ln_books ORDER BY updated_at DESC"
            ).fetchall()

            audio_title = str(audio["title"] or Path(str(audio["path"])).stem)
            audio_volume = _audiobook_volume(audio_title) or _audiobook_volume(_audiobook_path_label(str(audio["path"])))
            identity_id = int(identity["anilist_id"]) if identity is not None else None
            ranked: list[tuple[float, Any]] = []
            for novel in novels:
                novel_volume = int(novel["volume"] or 0) or None
                if audio_volume and novel_volume and audio_volume != novel_volume:
                    continue
                score = _audiobook_title_match_score(audio_title, str(novel["title"]))
                if identity_id and novel["anilist_id"] is not None and int(novel["anilist_id"]) == identity_id:
                    score = max(score, 120.0)
                if audio_volume and novel_volume == audio_volume:
                    score += 8.0
                ranked.append((score, novel))
            ranked.sort(key=lambda item: item[0], reverse=True)
            if not ranked:
                self._last_auto_link_reason = "no-ranked-candidates"
                return None
            best_score, novel = ranked[0]
            margin = best_score - (ranked[1][0] if len(ranked) > 1 else 0.0)
            if best_score < 90.0 or margin < 8.0:
                self._last_auto_link_reason = f"ambiguous:{best_score:.3f}:{margin:.3f}"
                return None
            ln_book_id = int(novel["id"])
            occupied = conn.execute(
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=?",
                (ln_book_id,),
            ).fetchone()
            if occupied is not None and int(occupied["audiobook_id"]) != audiobook_id:
                self._last_auto_link_reason = f"occupied:{int(occupied['audiobook_id'])}"
                return None
            if identity is None and novel["anilist_id"] is not None:
                media_id = int(novel["anilist_id"])
                conn.execute(
                    "INSERT OR REPLACE INTO media_identities(kind,local_id,anilist_id,anilist_type,title,cover_url,site_url,updated_at) "
                    "VALUES('audiobook',?,?,?,?,?,?,?)",
                    (
                        audiobook_id,
                        media_id,
                        "MANGA",
                        str(novel["title"] or audio_title),
                        str(novel["cover_url"] or ""),
                        f"https://anilist.co/manga/{media_id}",
                        time.time(),
                    ),
                )
            elif identity_id and novel["anilist_id"] is None:
                conn.execute(
                    "UPDATE ln_books SET anilist_id=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?",
                    (
                        identity_id,
                        str(identity["cover_url"] or ""),
                        time.time(),
                        ln_book_id,
                    ),
                )

        self.link_light_novel(ln_book_id, audiobook_id)
        self._last_auto_link_reason = f"linked:{ln_book_id}:{best_score:.3f}"
        return {"ln_book_id": ln_book_id, "audiobook_id": audiobook_id, "score": best_score}

    def auto_link_light_novel(self, ln_book_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone() is None:
                return None
            linked = conn.execute(
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=?",
                (int(ln_book_id),),
            ).fetchone()
            audiobook_ids = [
                int(row["id"])
                for row in conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC")
            ]
        if linked is not None:
            return {"ln_book_id": int(ln_book_id), "audiobook_id": int(linked["audiobook_id"])}
        for audiobook_id in audiobook_ids:
            result = self.auto_link_audiobook(audiobook_id)
            if result and int(result["ln_book_id"]) == int(ln_book_id):
                return result
        return None

    def search_catalog(self) -> list[dict[str, Any]]:
        """Return lightweight metadata for global search without STT/cover side effects."""
        with self.db.connect() as conn:
            books = conn.execute("SELECT id,title,path FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
            identities = {
                int(row["local_id"]): row
                for row in conn.execute(
                    "SELECT local_id,anilist_id,title,cover_url,site_url FROM media_identities WHERE kind='audiobook'"
                ).fetchall()
            }
            has_novels = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone() is not None
            links = {}
            if has_novels:
                for row in conn.execute(
                    "SELECT l.audiobook_id,b.title,b.volume,b.anilist_id,b.cover_url,l.updated_at "
                    "FROM reading_audio_links l JOIN ln_books b ON b.id=l.ln_book_id "
                    "ORDER BY l.updated_at"
                ).fetchall():
                    links[int(row["audiobook_id"])] = row
        result: list[dict[str, Any]] = []
        for row in books:
            book_id = int(row["id"])
            identity = identities.get(book_id)
            linked = links.get(book_id)
            title = str(row["title"] or "")
            linked_title = str(linked["title"] or "") if linked is not None else ""
            linked_volume = int(linked["volume"] or 0) if linked is not None else 0
            inferred_volume = linked_volume or (
                _audiobook_volume(title)
                or _audiobook_volume(str(row["path"] or ""))
                or _audiobook_volume(_audiobook_path_label(str(row["path"] or "")))
                or 0
            )
            series_title = _audiobook_series_title(
                linked_title or title, volume=inferred_volume
            ) or title
            result.append(
                {
                    "id": book_id,
                    "title": title,
                    "path": str(row["path"] or ""),
                    "series_title": series_title,
                    "series_key": _audiobook_title_key(series_title),
                    "volume": int(inferred_volume),
                    "anilist_id": int(identity["anilist_id"]) if identity is not None else (int(linked["anilist_id"]) if linked is not None and linked["anilist_id"] is not None else None),
                    "anilist_title": str(identity["title"] or "") if identity is not None else linked_title,
                    "cover_url": str((identity["cover_url"] if identity is not None else "") or (linked["cover_url"] if linked is not None else "") or ""),
                    "linked_light_novel": ({"title": linked_title, "volume": linked["volume"], "anilist_id": linked["anilist_id"]} if linked is not None else None),
                }
            )
        return result

    def _drop_invalid_managed_links(self) -> list[dict[str, Any]]:
        """Unlink confidently misidentified Pudge-managed audiobook downloads.

        Do not delete the audiobook or downloaded files.  Only the automatic LN
        link is removed, so a bad Nyaa match cannot consume hours of STT and the
        user can search/pair again.
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT l.ln_book_id,l.audiobook_id FROM reading_audio_links l ORDER BY l.ln_book_id"
            ).fetchall()
        removed: list[dict[str, Any]] = []
        for row in rows:
            audiobook_id = int(row["audiobook_id"])
            paths = [str(item.get("path") or "") for item in self._file_rows(audiobook_id)]
            conflict = managed_audiobook_series_conflict(paths)
            if conflict is None:
                continue
            ln_book_id = int(row["ln_book_id"])
            with self.db.connect() as conn:
                conn.execute("DELETE FROM reading_audio_links WHERE ln_book_id=?", (ln_book_id,))
            self.cancel_transcription(audiobook_id)
            payload = {
                "ln_book_id": ln_book_id,
                "audiobook_id": audiobook_id,
                **conflict,
            }
            removed.append(payload)
            LOGGER.warning(
                "Audiobook managed link rejected ln=%s audio=%s expected_series=%r source=%r",
                ln_book_id,
                audiobook_id,
                conflict.get("expected_series"),
                conflict.get("source"),
            )
        return removed

    def _linked_audiobook_ids(self) -> set[int]:
        with self.db.connect() as conn:
            return {
                int(row["audiobook_id"])
                for row in conn.execute(
                    "SELECT DISTINCT audiobook_id FROM reading_audio_links"
                ).fetchall()
            }

    def state(self, *, queue_missing: bool = True) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
        books = [self.book(int(row["id"])) for row in rows]
        # STT exists to align an audiobook to reading text.  Merely opening the
        # Audiobooks page must not enqueue every unlinked book for hours of work.
        linked_ids = self._linked_audiobook_ids() if queue_missing else set()
        if queue_missing:
            for book in books:
                audiobook_id = int(book["id"])
                if float(book.get("duration") or 0.0) <= 0.0 and Path(str(book.get("path") or "")).is_file():
                    self._queue_metadata_refresh(audiobook_id, Path(str(book["path"])))
                    book["metadata_pending"] = True
                    continue
                if audiobook_id not in linked_ids:
                    continue
                transcription = book.get("transcription") or {}
                if str(transcription.get("status") or "") in {"idle", "queued"} and not transcription.get("ready"):
                    book["transcription"] = self.prepare_transcription(
                        audiobook_id,
                        priority=WorkPriority.BACKGROUND,
                    )
        return {"books": books}

    def resume_pending_transcriptions(self) -> int:
        """Resume only analysis needed by an explicit LN↔audiobook link."""
        self._drop_invalid_managed_links()
        with self.db.connect() as conn:
            links = conn.execute(
                "SELECT ln_book_id,audiobook_id FROM reading_audio_links ORDER BY updated_at DESC,ln_book_id"
            ).fetchall()
        resumed = 0
        seen_audio: set[int] = set()
        for row in links:
            audiobook_id = int(row["audiobook_id"])
            if audiobook_id in seen_audio:
                continue
            seen_audio.add(audiobook_id)
            if self._load_transcript(audiobook_id) is not None:
                continue
            book = self.book(audiobook_id)
            source = Path(str(book.get("path") or ""))
            if float(book.get("duration") or 0.0) <= 0.0 and source.is_file():
                self._queue_metadata_refresh(audiobook_id, source)
                continue
            self.prepare_transcription(audiobook_id, priority=WorkPriority.BACKGROUND)
            resumed += 1
        for row in links:
            try:
                self.prepare_alignment(
                    int(row["ln_book_id"]), priority=WorkPriority.BACKGROUND
                )
            except Exception:
                continue
        return resumed

    def set_position(self, book_id: int, position: float) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT duration FROM audiobooks WHERE id=?", (int(book_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown audiobook id={book_id}")
            duration = float(row["duration"] or 0.0)
            value = max(0.0, min(float(position), duration or float(position)))
            finished = bool(duration > 0 and value >= duration * 0.98)
            conn.execute(
                "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                (value, int(finished), time.time(), int(book_id)),
            )

    @staticmethod
    def _ipc_command(ipc_path: Path, command: list[Any]) -> dict[str, Any] | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(ipc_path))
                client.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
                payload = json.loads(client.recv(4096).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


    @staticmethod
    def _ipc_commands_no_wait(
        ipc_path: Path,
        commands: list[list[Any]],
        *,
        timeout: float = _STOP_IPC_TIMEOUT,
    ) -> bool:
        # Queue mpv IPC commands without waiting for replies.
        try:
            payload = b"".join(
                json.dumps({"command": command}).encode("utf-8") + b"\n"
                for command in commands
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.01, float(timeout)))
                client.connect(str(ipc_path))
                client.sendall(payload)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _wait_or_kill(
        process: subprocess.Popen[Any],
        *,
        timeout: float = _STOP_GRACE_SECONDS,
    ) -> None:
        try:
            process.wait(timeout=max(0.01, float(timeout)))
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=max(0.01, float(timeout)))
        except subprocess.TimeoutExpired:
            pass

    @classmethod
    def _ipc_get(cls, ipc_path: Path, property_name: str) -> Any:
        payload = cls._ipc_command(ipc_path, ["get_property", property_name])
        return payload.get("data") if payload else None

    def _global_position(self, book_id: int, ipc_path: Path) -> float | None:
        raw = self._ipc_get(ipc_path, "time-pos")
        try:
            local = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            local = None
        if local is None:
            return None
        files = self._file_rows(book_id)
        if len(files) <= 1:
            return local
        raw_index = self._ipc_get(ipc_path, "playlist-pos")
        try:
            index = int(raw_index) if raw_index is not None else 0
        except (TypeError, ValueError):
            index = 0
        match = next((row for row in files if int(row["file_index"]) == index), None)
        return float(match["start"] if match else 0.0) + local

    def _reconcile_startup_position(
        self,
        book_id: int,
        ipc_path: Path,
        live_position: float,
    ) -> float:
        """Repair mpv startup when command-line --start was not honored.

        mpv can expose IPC before a per-file ``--start`` seek has taken effect.
        The monitor previously persisted that transient ~0s position, causing
        paired LN reading to jump backwards and lose the requested resume point.
        During a short startup window, explicitly seek to the requested target
        when the first live clock is implausibly far away.
        """

        book_id = int(book_id)
        now = time.monotonic()
        with self._lock:
            target = dict(getattr(self, "_startup_targets", {}).get(book_id) or {})
        if not target:
            return float(live_position)
        launched_at = float(target.get("launched_at") or now)
        if now - launched_at > 6.0:
            with self._lock:
                getattr(self, "_startup_targets", {}).pop(book_id, None)
            return float(live_position)
        requested = float(target.get("global_position") or 0.0)
        speed = max(0.5, float(target.get("speed") or 1.0))
        expected = requested + max(0.0, now - launched_at) * speed
        drift = float(live_position) - expected
        if abs(drift) <= 2.0:
            with self._lock:
                getattr(self, "_startup_targets", {}).pop(book_id, None)
            return float(live_position)

        files = self._file_rows(book_id)
        selected_index = int(target.get("file_index") or 0)
        local_position = max(0.0, float(target.get("local_position") or requested))
        if len(files) > 1:
            current_index = self._ipc_get(ipc_path, "playlist-pos")
            try:
                current_index_value = int(current_index) if current_index is not None else -1
            except (TypeError, ValueError):
                current_index_value = -1
            if current_index_value != selected_index:
                response = self._ipc_command(ipc_path, ["playlist-play-index", selected_index])
                if response is None or response.get("error") != "success":
                    return float(live_position)

        response = self._ipc_command(
            ipc_path,
            ["seek", local_position, "absolute", "exact"],
        )
        if response is None or response.get("error") != "success":
            return float(live_position)
        with self._lock:
            getattr(self, "_startup_targets", {}).pop(book_id, None)
            self._last_positions[book_id] = requested
            self._last_motion_at[book_id] = now
        # Do not persist the stale pre-seek IPC sample.  The next monitor poll
        # will read the authoritative post-seek position.
        return requested


    def _sleep_reached(self, book_id: int, position: float | None) -> bool:
        with self._lock:
            deadline = self._sleep_deadlines.get(book_id)
            chapter_end = self._sleep_chapter_ends.get(book_id)
        return bool(
            (deadline is not None and time.monotonic() >= deadline)
            or (chapter_end is not None and position is not None and position >= chapter_end - 0.25)
        )


    def _playback_session_owned(
        self,
        book_id: int,
        process: subprocess.Popen[Any],
        ipc_path: Path,
        session_id: str,
    ) -> bool:
        book_id = int(book_id)
        with self._lock:
            return bool(
                self._playback_sessions.get(book_id) == session_id
                and self._players.get(book_id) is process
                and self._ipc_paths.get(book_id) == ipc_path
            )

    def _monitor(
        self,
        book_id: int,
        process: subprocess.Popen[Any],
        ipc_path: Path,
        session_id: str,
    ) -> None:
        last_saved = 0.0
        last_position: float | None = None
        try:
            while process.poll() is None:
                if not self._playback_session_owned(book_id, process, ipc_path, session_id):
                    break
                position = self._global_position(book_id, ipc_path)
                with self._playback_lock:
                    if not self._playback_session_owned(
                        book_id, process, ipc_path, session_id
                    ):
                        break
                    if position is not None:
                        position = self._reconcile_startup_position(
                            book_id, ipc_path, position
                        )
                        last_position = position
                        self._record_playback_position(book_id, position)
                        if time.monotonic() - last_saved >= _POSITION_WRITE_INTERVAL:
                            self.set_position(book_id, position)
                            last_saved = time.monotonic()
                    if self._sleep_reached(book_id, position):
                        self._ipc_commands_no_wait(
                            ipc_path,
                            [
                                ["set_property", "mute", True],
                                ["set_property", "pause", True],
                                ["quit"],
                            ],
                        )
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        if position is not None:
                            self.set_position(book_id, position)
                        self._wait_or_kill(process)
                        break
                time.sleep(_MONITOR_POLL_INTERVAL)
        finally:
            with self._playback_lock:
                owned = self._playback_session_owned(
                    book_id, process, ipc_path, session_id
                )
                if owned:
                    if last_position is not None:
                        self.set_position(book_id, last_position)
                    with self._lock:
                        if (
                            self._playback_sessions.get(int(book_id)) == session_id
                            and self._players.get(int(book_id)) is process
                            and self._ipc_paths.get(int(book_id)) == ipc_path
                        ):
                            self._players.pop(int(book_id), None)
                            self._ipc_paths.pop(int(book_id), None)
                            self._playback_sessions.pop(int(book_id), None)
                            self._last_positions.pop(int(book_id), None)
                            self._startup_targets.pop(int(book_id), None)
                            self._last_motion_at.pop(int(book_id), None)
                            self._sleep_deadlines.pop(int(book_id), None)
                            self._sleep_chapter_ends.pop(int(book_id), None)
                # IPC paths are unique per playback session, so an old monitor
                # can safely remove only its own socket without touching the new one.
                ipc_path.unlink(missing_ok=True)

    def stop(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        # Legacy tests construct a minimal service via __new__. Create the
        # transition lock lazily there; normal instances already have it.
        playback_lock = getattr(self, "_playback_lock", None)
        if playback_lock is None:
            playback_lock = threading.RLock()
            self._playback_lock = playback_lock
        with playback_lock:
            with self._lock:
                process = self._players.get(book_id)
                ipc_path = self._ipc_paths.get(book_id)
                sessions = getattr(self, "_playback_sessions", {})
                session_id = sessions.get(book_id)
                final_position = self._last_positions.get(book_id)
            if process is None or process.poll() is not None:
                return {"ok": True, "book": self.book(book_id), "stopped": False}

            if ipc_path is not None:
                # Do not wait for mpv replies here. Muting and quitting are queued in
                # one short IPC write, while process termination is the hard fallback.
                self._ipc_commands_no_wait(
                    ipc_path,
                    [
                        ["set_property", "mute", True],
                        ["set_property", "pause", True],
                        ["quit"],
                    ],
                )

            try:
                process.terminate()
            except OSError:
                pass

            # Position is continuously cached by the monitor, so Stop never performs
            # the former sequence of blocking IPC reads before silencing playback.
            if final_position is not None:
                self.set_position(book_id, final_position)

            self._wait_or_kill(process)

            with self._lock:
                sessions = getattr(self, "_playback_sessions", {})
                session_still_owned = (
                    session_id is None or sessions.get(book_id) == session_id
                )
                if self._players.get(book_id) is process and session_still_owned:
                    self._players.pop(book_id, None)
                    self._ipc_paths.pop(book_id, None)
                    sessions.pop(book_id, None)
                    self._last_positions.pop(book_id, None)
                    getattr(self, "_startup_targets", {}).pop(book_id, None)
                    getattr(self, "_last_motion_at", {}).pop(book_id, None)
                    self._sleep_deadlines.pop(book_id, None)
                    self._sleep_chapter_ends.pop(book_id, None)
            if ipc_path is not None:
                ipc_path.unlink(missing_ok=True)
            return {"ok": True, "book": self.book(book_id), "stopped": True}

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._players)
            alignment_processes = list(self._alignment_processes.values())
            transcription_processes = list(self._transcription_processes.values())
        for book_id in ids:
            try:
                self.stop(book_id)
            except Exception:
                continue
        for process in [*alignment_processes, *transcription_processes]:
            if process.poll() is None:
                process.terminate()

    def play(
        self, book_id: int, start: float | None = None, speed: float = 1.0
    ) -> dict[str, Any]:
        if self._closed_event.is_set():
            raise RuntimeError("Audiobook service is closed")
        with self._playback_lock:
            return self._play_locked(int(book_id), start=start, speed=speed)

    def _play_locked(
        self, book_id: int, start: float | None = None, speed: float = 1.0
    ) -> dict[str, Any]:
        book_id = int(book_id)
        speed = max(0.5, min(3.0, float(speed or 1.0)))
        if self.is_playing(book_id):
            # _playback_lock is re-entrant, so the public Stop path retains its
            # fast-stop contract while serializing this ownership transition.
            self.stop(book_id)
        book = self.book(book_id)
        position = float(book["position"] if start is None else start)
        # A small smart rewind makes returning after a break less disorienting.
        if start is None and position > 12:
            with self.db.connect() as conn:
                row = conn.execute("SELECT last_played_at FROM audiobooks WHERE id=?", (book_id,)).fetchone()
            if row is not None and time.time() - float(row["last_played_at"] or 0.0) > 5 * 60:
                position = max(0.0, position - 10.0)
        files = self._file_rows(book_id)
        if not files:
            path = Path(str(book["path"]))
            if not path.is_file():
                raise FileNotFoundError(path)
            files = [
                {
                    "file_index": 0,
                    "path": str(path),
                    "start": 0.0,
                    "end": float(book["duration"] or 0.0),
                }
            ]
        selected_index = 0
        local_start = position
        for row in files:
            start_at = float(row.get("start") or 0.0)
            end_at = float(row.get("end") or start_at)
            if position >= start_at:
                selected_index = int(row.get("file_index") or 0)
                local_start = max(0.0, position - start_at)
            if position < end_at:
                break

        ipc_dir = self.cache_dir / "audiobook-ipc"
        ipc_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"{time.monotonic_ns():x}-{threading.get_ident():x}"
        ipc_path = ipc_dir / f"book-{book_id}-{os.getpid()}-{session_id}.sock"
        ipc_path.unlink(missing_ok=True)
        command = [
            self.mpv,
            # Audiobooks and paired LN reading must never load the user's
            # normal mpv configuration. In particular, JitenMPV and
            # jpdb-mpv-plugin are video study integrations and can spawn
            # their own GUI processes even though audiobook playback has no
            # video window.
            "--no-config",
            "--load-scripts=no",
            "--no-video",
            "--force-window=no",
        ]
        command.extend(self._tempo_filter_args())
        command.extend(
            [
                f"--speed={speed:.3f}",
                "--audio-display=no",
                f"--input-ipc-server={ipc_path}",
            ]
        )
        if len(files) > 1:
            command.append(f"--playlist-start={selected_index}")

        # mpv command-line options normally apply to every file in a playlist.
        # Keep the resume offset local to the initially selected file, or the
        # same offset is applied again when mpv advances to the next chapter.
        for row in files:
            path_value = str(row["path"])
            if int(row.get("file_index") or 0) == selected_index and local_start > 0:
                command.extend(
                    [
                        "--{",
                        f"--start={max(0.0, local_start):.3f}",
                        path_value,
                        "--}",
                    ]
                )
            else:
                command.append(path_value)
        process = subprocess.Popen(command)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET speed=?,last_played_at=?,updated_at=? WHERE id=?",
                (speed, time.time(), time.time(), book_id),
            )
        with self._lock:
            self._players[book_id] = process
            self._ipc_paths[book_id] = ipc_path
            self._playback_sessions[book_id] = session_id
            self._last_positions[book_id] = position
            launched_at = time.monotonic()
            self._last_motion_at[book_id] = launched_at
            self._startup_targets[book_id] = {
                "global_position": position,
                "local_position": local_start,
                "file_index": selected_index,
                "speed": speed,
                "launched_at": launched_at,
            }
            self._speeds[book_id] = speed
        monitor = self._tracked_thread(
            target=self._monitor,
            args=(book_id, process, ipc_path, session_id),
            name=f"audiobook-{book_id}",
        )
        if monitor is not None:
            monitor.start()
        return {"ok": True, "book_id": book_id, "position": position, "playing": True, "speed": speed}

    def set_paused(self, book_id: int, paused: bool) -> dict[str, Any]:
        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None or ipc_path is None:
            return {
                "ok": False,
                "book_id": book_id,
                "playing": False,
                "player_running": False,
            }

        value = bool(paused)
        # Transport controls are latency-sensitive: writing pause/resume to the
        # local mpv socket is enough. Waiting for mpv's JSON reply made a space
        # press visibly lag behind the reader highlight. The paired-state poll
        # verifies the resulting state immediately afterwards.
        sent = self._ipc_commands_no_wait(
            ipc_path,
            [["set_property", "pause", value]],
        )
        if sent and not value:
            with self._lock:
                self._last_motion_at[book_id] = time.monotonic()
        return {
            "ok": bool(sent),
            "book_id": book_id,
            "playing": not value,
            "player_running": True,
            "paused": value,
        }

    def set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        book_id = int(book_id)
        value = max(0.5, min(3.0, float(speed or 1.0)))
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
            self._speeds[book_id] = value
            startup_target = getattr(self, "_startup_targets", {}).get(book_id)
            if startup_target is not None:
                startup_target["speed"] = value
        LOGGER.info("Audiobook speed request book=%s speed=%.3f player=%s", book_id, value, ipc_path is not None)
        with self.db.connect() as conn:
            conn.execute("UPDATE audiobooks SET speed=?,updated_at=? WHERE id=?", (value, time.time(), book_id))
        live_speed: Any = None
        restarted = False
        if ipc_path is not None and self.is_playing(book_id):
            applied = False
            for _attempt in range(3):
                response = self._ipc_command(ipc_path, ["set_property", "speed", value])
                live_speed = self._ipc_get(ipc_path, "speed")
                try:
                    applied = bool(
                        response is not None
                        and response.get("error") == "success"
                        and live_speed is not None
                        and abs(float(live_speed) - value) < 0.01
                    )
                except (TypeError, ValueError):
                    applied = False
                if applied:
                    break
                time.sleep(0.04)
            if not applied:
                position = self._global_position(book_id, ipc_path)
                self.play(
                    book_id,
                    start=float(position if position is not None else self.book(book_id)["position"]),
                    speed=value,
                )
                restarted = True
        LOGGER.info(
            "Audiobook speed applied book=%s requested=%.3f live=%r restarted=%s",
            book_id,
            value,
            live_speed,
            restarted,
        )
        return {"ok": True, "book_id": book_id, "speed": value, "book": self.book(book_id)}

    def seek(self, book_id: int, seconds: float) -> dict[str, Any]:
        book_id = int(book_id)
        with self._lock:
            getattr(self, "_startup_targets", {}).pop(book_id, None)
        delta = float(seconds)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None and self.is_playing(book_id):
            self._ipc_command(ipc_path, ["seek", delta, "relative", "exact"])
            time.sleep(0.03)
            position = self._global_position(book_id, ipc_path)
            if position is not None:
                self.set_position(book_id, position)
        else:
            book = self.book(book_id)
            self.set_position(book_id, float(book["position"] or 0.0) + delta)
        return {"ok": True, "book": self.book(book_id)}

    def seek_to(self, book_id: int, position: float) -> dict[str, Any]:
        book_id = int(book_id)
        with self._lock:
            getattr(self, "_startup_targets", {}).pop(book_id, None)
        book = self.book(book_id)
        value = max(0.0, min(float(position), float(book["duration"] or position)))
        if self.is_playing(book_id):
            files = self._file_rows(book_id)
            selected = next(
                (
                    row
                    for row in files
                    if float(row.get("start") or 0.0)
                    <= value
                    < float(row.get("end") or value + 0.001)
                ),
                files[-1] if files else None,
            )
            with self._lock:
                ipc_path = self._ipc_paths.get(book_id)
            current_index = self._ipc_get(ipc_path, "playlist-pos") if ipc_path else None
            selected_index = int(selected.get("file_index") or 0) if selected else 0
            same_file = len(files) <= 1 or (
                current_index is not None and int(current_index) == selected_index
            )
            local_position = value - float(selected.get("start") or 0.0) if selected else value
            response = (
                self._ipc_command(
                    ipc_path,
                    ["seek", max(0.0, local_position), "absolute", "exact"],
                )
                if ipc_path is not None and same_file
                else None
            )
            if response is None or response.get("error") != "success":
                self.play(book_id, start=value, speed=float(book["speed"] or 1.0))
            self.set_position(book_id, value)
        else:
            self.set_position(book_id, value)
        return {"ok": True, "book": self.book(book_id)}

    def set_sleep_timer(
        self,
        book_id: int,
        *,
        seconds: float | None = None,
        end_of_chapter: bool = False,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None:
            live = self._global_position(book_id, ipc_path)
            if live is not None:
                position = live
        with self._lock:
            self._sleep_deadlines.pop(book_id, None)
            self._sleep_chapter_ends.pop(book_id, None)
            if end_of_chapter:
                chapter = self._chapter_for_position(book["chapters"], position)
                if chapter is not None:
                    self._sleep_chapter_ends[book_id] = float(chapter["end"])
            elif seconds is not None and float(seconds) > 0:
                self._sleep_deadlines[book_id] = time.monotonic() + float(seconds)
        return {"ok": True, "book": self.book(book_id)}

    def add_bookmark(self, book_id: int, title: str = "") -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None:
            live = self._global_position(book_id, ipc_path)
            if live is not None:
                position = live
        label = str(title or "").strip() or f"{int(position // 60)}:{int(position % 60):02d}"
        with self.db.connect() as conn:
            next_order = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sort_order),-1)+1 FROM audiobook_bookmarks WHERE book_id=?",
                    (book_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) "
                "VALUES(?,?,?,?,?)",
                (book_id, position, label, next_order, time.time()),
            )
            bookmark_id = int(cursor.lastrowid)
        return {"ok": True, "bookmark_id": bookmark_id, "book": self.book(book_id)}

    def rename_bookmark(self, bookmark_id: int, title: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT book_id FROM audiobook_bookmarks WHERE id=?", (int(bookmark_id),)
            ).fetchone()
            if row is None:
                return {"ok": False}
            book_id = int(row["book_id"])
            conn.execute(
                "UPDATE audiobook_bookmarks SET title=? WHERE id=?",
                (str(title or "").strip(), int(bookmark_id)),
            )
        return {"ok": True, "book": self.book(book_id)}

    def reorder_bookmarks(self, book_id: int, bookmark_ids: list[int]) -> dict[str, Any]:
        book_id = int(book_id)
        requested = [int(value) for value in bookmark_ids]
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM audiobook_bookmarks WHERE book_id=? ORDER BY sort_order,position,id",
                (book_id,),
            ).fetchall()
            existing = [int(row["id"]) for row in rows]
            if sorted(requested) != sorted(existing):
                raise ValueError("Bookmark reorder must contain every bookmark exactly once")
            conn.executemany(
                "UPDATE audiobook_bookmarks SET sort_order=? WHERE id=? AND book_id=?",
                [(index, bookmark_id, book_id) for index, bookmark_id in enumerate(requested)],
            )
        return {"ok": True, "book": self.book(book_id)}

    def restore_bookmark(
        self,
        book_id: int,
        position: float,
        title: str,
        sort_order: int,
        created_at: float,
        bookmark_id: int | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        position = max(0.0, float(position))
        title = str(title or "").strip()
        created_at = float(created_at or time.time())
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM audiobook_bookmarks WHERE book_id=? ORDER BY sort_order,position,id",
                (book_id,),
            ).fetchall()
            existing_ids = [int(row["id"]) for row in rows]
            index = max(0, min(int(sort_order), len(existing_ids)))
            requested_id = int(bookmark_id) if bookmark_id is not None else 0
            if requested_id > 0 and conn.execute(
                "SELECT 1 FROM audiobook_bookmarks WHERE id=?", (requested_id,)
            ).fetchone() is None:
                cursor = conn.execute(
                    "INSERT INTO audiobook_bookmarks(id,book_id,position,title,sort_order,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (requested_id, book_id, position, title, len(existing_ids), created_at),
                )
                restored_id = int(cursor.lastrowid or requested_id)
            else:
                cursor = conn.execute(
                    "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (book_id, position, title, len(existing_ids), created_at),
                )
                restored_id = int(cursor.lastrowid)
            ordered_ids = [*existing_ids]
            ordered_ids.insert(index, restored_id)
            conn.executemany(
                "UPDATE audiobook_bookmarks SET sort_order=? WHERE id=? AND book_id=?",
                [(order, item_id, book_id) for order, item_id in enumerate(ordered_ids)],
            )
        return {"ok": True, "bookmark_id": restored_id, "book": self.book(book_id)}

    def delete_bookmark(self, bookmark_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM audiobook_bookmarks WHERE id=?", (int(bookmark_id),)
            ).fetchone()
            if row is None:
                return {"ok": False}
            book_id = int(row["book_id"])
            deleted = dict(row)
            conn.execute("DELETE FROM audiobook_bookmarks WHERE id=?", (int(bookmark_id),))
            remaining = conn.execute(
                "SELECT id FROM audiobook_bookmarks WHERE book_id=? ORDER BY sort_order,position,id",
                (book_id,),
            ).fetchall()
            conn.executemany(
                "UPDATE audiobook_bookmarks SET sort_order=? WHERE id=? AND book_id=?",
                [(order, int(item["id"]), book_id) for order, item in enumerate(remaining)],
            )
        return {"ok": True, "deleted": deleted, "book": self.book(book_id)}

    def mark_finished(self, book_id: int, finished: bool = True) -> dict[str, Any]:
        if self.is_playing(int(book_id)):
            self.stop(int(book_id))
        book = self.book(int(book_id))
        position = float(book["duration"] or 0.0) if finished else 0.0
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                (position, int(bool(finished)), time.time(), int(book_id)),
            )
        return {"ok": True, "book": self.book(int(book_id))}

    def _transcript_fingerprint(self, audiobook_id: int) -> str:
        audiobook_id = int(audiobook_id)
        key = ("transcript", audiobook_id, 0)
        now = time.monotonic()
        with self._lock:
            cached = self._fingerprint_cache.get(key)
        if cached is not None and now - cached[0] < 5.0:
            return cached[1]
        digest = hashlib.sha256(f"audiobook-stt-v3-chunks\0{self.stt_model}\0".encode())
        for row in self._file_rows(audiobook_id):
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        value = digest.hexdigest()[:28]
        with self._lock:
            self._fingerprint_cache[key] = (now, value)
        return value

    def _transcript_path(self, audiobook_id: int) -> Path:
        return (
            self.cache_dir
            / "audiobook-transcripts"
            / f"{self._transcript_fingerprint(int(audiobook_id))}.json"
        )

    def _legacy_transcript_path_v2(self, audiobook_id: int) -> Path:
        digest = hashlib.sha256(f"audiobook-stt-v2\0{self.stt_model}\0".encode())
        for row in self._file_rows(int(audiobook_id)):
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        return self.cache_dir / "audiobook-transcripts" / f"{digest.hexdigest()[:28]}.json"

    def _migrate_legacy_transcript_v2(self, audiobook_id: int) -> Path | None:
        target = self._transcript_path(int(audiobook_id))
        if target.is_file():
            return target
        legacy = self._legacy_transcript_path_v2(int(audiobook_id))
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != "audiobook-stt-v2" or not isinstance(payload.get("segments"), list):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        migrated = {**payload, "schema": "audiobook-stt-v3", "migrated_from": "audiobook-stt-v2"}
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(migrated, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
        return target

    def _activity_fingerprint(self, audiobook_id: int) -> str:
        digest = hashlib.sha256(b"audiobook-activity-v1\0")
        for row in self._file_rows(int(audiobook_id)):
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        return digest.hexdigest()[:28]

    def _activity_path(self, audiobook_id: int) -> Path:
        return (
            self.cache_dir
            / "audiobook-activity"
            / f"{self._activity_fingerprint(int(audiobook_id))}.json"
        )

    def _resolved_ffmpeg(self) -> str:
        resolved = (
            shutil.which(self.ffmpeg)
            if os.sep not in self.ffmpeg
            else str(Path(self.ffmpeg).expanduser())
        )
        if not resolved or not Path(resolved).is_file():
            raise FileNotFoundError(
                f"Configured ffmpeg was not found: {self.ffmpeg}. Re-run install.sh."
            )
        return str(Path(resolved).resolve())

    @staticmethod
    def _transcript_activity_regions(
        segments: list[dict[str, Any]],
    ) -> list[dict[str, float]]:
        regions: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            words = segment.get("words")
            rows = words if isinstance(words, list) and words else [segment]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    start = float(row.get("start") or segment.get("start") or 0.0)
                    end = float(row.get("end") or segment.get("end") or start)
                except (TypeError, ValueError):
                    continue
                regions.append({"start": start, "end": end})
        return merge_activity_regions(regions, bridge_seconds=0.10)

    def _load_or_analyze_activity(
        self,
        audiobook_id: int,
        transcript_segments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, float]], str]:
        """Load precise cached activity without blocking first alignment on full-book FFT.

        Whisper word timestamps already provide a good speech mask.  Older code decoded
        every audiobook file and ran FFT before the first text alignment, which could
        keep the UI process busy for minutes on multi-file books.  Reuse FFT when it
        already exists; otherwise align immediately from STT activity.
        """
        output = self._activity_path(int(audiobook_id))
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("schema") == "audiobook-activity-v1":
            regions = merge_activity_regions(cached.get("regions") or [], bridge_seconds=0.0)
            if regions:
                return regions, "fft"
        return self._transcript_activity_regions(transcript_segments), "stt"

    def _load_transcript(self, audiobook_id: int) -> dict[str, Any] | None:
        path = self._transcript_path(int(audiobook_id))
        if not path.is_file():
            migrated = self._migrate_legacy_transcript_v2(int(audiobook_id))
            if migrated is not None:
                path = migrated
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return (
            payload
            if isinstance(payload, dict)
            and payload.get("schema") in {"audiobook-stt-v2", "audiobook-stt-v3"}
            and isinstance(payload.get("segments"), list)
            else None
        )

    def transcription_status(self, audiobook_id: int) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        cached = self._load_transcript(audiobook_id)
        if cached is not None:
            return {
                "status": "ready",
                "ready": True,
                "model": str(cached.get("model") or self.stt_model),
                "segment_count": len(cached.get("segments") or []),
            }
        with self._lock:
            job = dict(self._transcription_jobs.get(audiobook_id) or {})
            queue = list(self._transcription_queue)
        if job and str(job.get("status") or "") in {"queued", "transcribing"}:
            job["background"] = True
            job["elapsed_seconds"] = max(
                0.0,
                time.time() - float(job.get("started_at") or time.time()),
            )
            if str(job.get("status") or "") == "queued" and audiobook_id in queue:
                job["queue_position"] = queue.index(audiobook_id) + 1
                job["queue_size"] = len(queue)
        return job or {"status": "idle", "ready": False, "background": True}

    def _set_transcription_job(self, audiobook_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            current = dict(self._transcription_jobs.get(int(audiobook_id)) or {})
            now = time.time()
            updated = {
                **current,
                **payload,
                "audiobook_id": int(audiobook_id),
                "started_at": float(payload.get("started_at") or current.get("started_at") or now),
                "updated_at": now,
            }
            self._transcription_jobs[int(audiobook_id)] = updated
        job_id = str(updated.get("job_id") or "")
        if self.job_center is None or not job_id:
            return
        status = str(updated.get("status") or "queued")
        if status == "ready":
            self.job_center.finish(
                job_id,
                message="Transcription ready",
                result={"audiobook_id": int(audiobook_id), "segment_count": updated.get("segment_count", 0)},
            )
        elif status == "error":
            self.job_center.fail(job_id, updated.get("error") or "Japanese STT failed")
        elif status == "cancelled":
            self.job_center.cancelled(job_id)
        else:
            self.job_center.update(
                job_id,
                state="running" if status == "transcribing" else "queued",
                current=float(updated.get("processed_audio_seconds") or 0.0),
                total=float(updated.get("total_duration") or 0.0),
                message=(
                    f"STT file {int(updated.get('file') or 1)}/{int(updated.get('file_count') or 1)}"
                    if status == "transcribing"
                    else "Queued"
                ),
            )
            self.job_center.checkpoint(
                job_id,
                {
                    "audiobook_id": int(audiobook_id),
                    "file": int(updated.get("file") or 0),
                    "file_count": int(updated.get("file_count") or 0),
                    "processed_audio_seconds": float(
                        updated.get("processed_audio_seconds") or 0.0
                    ),
                },
            )

    def _transcription_checkpoint_dir(self, output: Path) -> Path:
        return output.with_name(f".{output.stem}-chunks")

    @staticmethod
    def _valid_stt_chunk_result(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        segments = payload.get("segments") if isinstance(payload, dict) else None
        return payload if isinstance(segments, list) else None

    def _transcription_chunk_plan(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        global_chunk = 0
        for file_number, row in enumerate(files, 1):
            duration = max(0.0, float(row.get("duration") or 0.0))
            span_duration = max(0.0, float(row.get("end") or 0.0) - float(row.get("start") or 0.0))
            duration = max(duration, span_duration)
            # Old imports can contain duration=0.  Unknown duration is never
            # proof that a source is short: probe it before deciding whether
            # passing the original file to MLX is safe.
            if duration <= 0.0:
                try:
                    duration = max(0.0, float(self._probe(Path(str(row["path"])))[0] or 0.0))
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    duration = _STT_CHUNK_SECONDS + 0.01
            # Preserve the old short-file path exactly: no lossy/reencoded
            # intermediate file when the source itself is already bounded.
            count = max(1, int((duration + _STT_CHUNK_SECONDS - 1e-9) // _STT_CHUNK_SECONDS)) if duration > _STT_CHUNK_SECONDS else 1
            for chunk_index in range(count):
                local_start = chunk_index * _STT_CHUNK_SECONDS
                chunk_duration = (
                    max(0.01, min(_STT_CHUNK_SECONDS, duration - local_start))
                    if duration > 0
                    else 0.0
                )
                global_chunk += 1
                chunks.append(
                    {
                        "global_chunk": global_chunk,
                        "file": file_number,
                        "file_count": len(files),
                        "chunk": chunk_index + 1,
                        "chunk_count": count,
                        "source": str(row["path"]),
                        "local_start": local_start,
                        "duration": chunk_duration,
                        "global_offset": float(row.get("start") or 0.0) + local_start,
                        "direct": count == 1,
                    }
                )
        total_chunks = len(chunks)
        for chunk in chunks:
            chunk["total_chunks"] = total_chunks
        return chunks

    def _extract_stt_chunk(
        self,
        source: Path,
        destination: Path,
        *,
        start: float,
        duration: float,
        cancel_event: threading.Event | None,
    ) -> None:
        destination.unlink(missing_ok=True)
        command = [
            self._resolved_ffmpeg(), "-v", "error", "-nostdin", "-y",
            "-ss", f"{max(0.0, start):.3f}",
            "-t", f"{max(0.01, duration):.3f}",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "flac", str(destination),
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=3)
                    raise InterruptedError("Transcription cancelled")
                if time.monotonic() - started > max(120.0, duration * 2.0):
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=3)
                    raise RuntimeError("ffmpeg STT chunk extraction timed out")
                time.sleep(0.08)
            stderr = process.stderr.read() if process.stderr is not None else ""
            if process.returncode != 0 or not destination.is_file():
                raise RuntimeError((stderr or "ffmpeg could not extract STT chunk").strip()[-1200:])
        finally:
            if process.stderr is not None:
                process.stderr.close()

    def _transcribe_worker(
        self,
        audiobook_id: int,
        output: Path,
        event: threading.Event,
        *,
        heavy_lease: Any | None = None,
    ) -> None:
        with self._lock:
            cancel_event = self._transcription_cancel_events.get(audiobook_id)
            job = dict(self._transcription_jobs.get(audiobook_id) or {})
        requested_priority = WorkPriority(int(job.get("priority") or int(WorkPriority.BACKGROUND)))
        if self.work_scheduler is not None and heavy_lease is None:
            if not self.work_scheduler.background_allowed(priority=requested_priority, resource="cpu"):
                self._set_transcription_job(audiobook_id, {
                    "status": "queued", "ready": False,
                    "phase": "waiting_for_foreground", "wait_reason": "foreground",
                })
            heavy_lease = self.work_scheduler.acquire_heavy(
                "audiobook-stt", blocking=True, foreground_sensitive=True,
                wait_for_foreground=True, cancel_event=cancel_event,
                priority=requested_priority,
            )
            if heavy_lease is None:
                self._set_transcription_job(audiobook_id, {"status":"cancelled","ready":False,"error":""})
                event.set(); return
        self._set_transcription_job(audiobook_id, {
            "status": "transcribing", "ready": False,
            "phase": "starting", "wait_reason": "",
        })
        output.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_dir = self._transcription_checkpoint_dir(output)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        current_audio: Path | None = None
        current_progress: Path | None = None
        try:
            files = self._file_rows(int(audiobook_id))
            if not files:
                raise ValueError("Audiobook has no readable files")
            chunks = self._transcription_chunk_plan(files)
            total_duration = sum(max(0.0, float(row.get("duration") or 0.0)) for row in files)
            started_at = time.time()
            completed_duration = 0.0
            all_segments: list[dict[str, Any]] = []
            for chunk in chunks:
                with self._lock:
                    cancel_event = self._transcription_cancel_events.get(audiobook_id)
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Transcription cancelled")
                source = Path(str(chunk["source"]))
                if not source.is_file():
                    raise FileNotFoundError(source)
                chunk_number = int(chunk["global_chunk"])
                result_path = checkpoint_dir / f"chunk-{chunk_number:05d}.json"
                cached = self._valid_stt_chunk_result(result_path)
                chunk_duration = max(0.0, float(chunk["duration"] or 0.0))
                if cached is not None:
                    all_segments.extend(self._shift_transcription_segments(
                        [item for item in (cached.get("segments") or []) if isinstance(item, dict)],
                        float(chunk["global_offset"]),
                    ))
                    completed_duration += chunk_duration
                    self._set_transcription_job(audiobook_id, {
                        "status":"transcribing","ready":False,
                        "file":int(chunk["file"]),"file_count":int(chunk["file_count"]),
                        "chunk":chunk_number,"chunk_count":int(chunk["total_chunks"]),
                        "chunk_progress_percent":100,"resumed_chunk":True,
                        "progress_percent":completed_duration/total_duration*100.0 if total_duration else 100.0,
                        "processed_audio_seconds":completed_duration,
                        "remaining_audio_seconds":max(0.0,total_duration-completed_duration),
                        "total_duration":total_duration,"started_at":started_at,
                    })
                    continue

                current_progress = checkpoint_dir / f"chunk-{chunk_number:05d}.progress.json"
                current_progress.unlink(missing_ok=True)
                stdout_path = checkpoint_dir / f"chunk-{chunk_number:05d}.stdout.log"
                stderr_path = checkpoint_dir / f"chunk-{chunk_number:05d}.stderr.log"
                if bool(chunk["direct"]):
                    input_audio = source
                    current_audio = None
                else:
                    current_audio = checkpoint_dir / f"chunk-{chunk_number:05d}.flac"
                    self._set_transcription_job(audiobook_id, {
                        "status":"transcribing","ready":False,"phase":"extracting",
                        "file":int(chunk["file"]),"file_count":int(chunk["file_count"]),
                        "chunk":chunk_number,"chunk_count":int(chunk["total_chunks"]),
                        "progress_percent":completed_duration/total_duration*100.0 if total_duration else 0.0,
                        "processed_audio_seconds":completed_duration,"total_duration":total_duration,
                        "started_at":started_at,
                    })
                    self._extract_stt_chunk(
                        source, current_audio, start=float(chunk["local_start"]),
                        duration=chunk_duration, cancel_event=cancel_event,
                    )
                    input_audio = current_audio

                self._set_transcription_job(audiobook_id, {
                    "status":"transcribing","ready":False,"phase":"transcribing",
                    "file":int(chunk["file"]),"file_count":int(chunk["file_count"]),
                    "chunk":chunk_number,"chunk_count":int(chunk["total_chunks"]),
                    "chunk_progress_percent":0,
                    "progress_percent":completed_duration/total_duration*100.0 if total_duration else 0.0,
                    "processed_audio_seconds":completed_duration,
                    "remaining_audio_seconds":max(0.0,total_duration-completed_duration),
                    "total_duration":total_duration,"started_at":started_at,
                })
                command = [self.python,"-m","pudge.subtitles.stt_worker","--words",str(input_audio),str(result_path),self.stt_model,str(current_progress)]
                environment = os.environ.copy()
                ffmpeg = self._resolved_ffmpeg()
                environment["PATH"] = os.pathsep.join(part for part in (str(Path(ffmpeg).resolve().parent), environment.get("PATH", "")) if part)
                environment.setdefault("PUDGE_MLX_CACHE_LIMIT_BYTES", str(_STT_MLX_CACHE_LIMIT_BYTES))
                environment.setdefault("PUDGE_MLX_MEMORY_LIMIT_BYTES", str(_STT_MLX_MEMORY_LIMIT_BYTES))
                timeout = max(12 * 60, min(45 * 60, max(60.0, chunk_duration) * 5.0))
                with stdout_path.open("w",encoding="utf-8") as stdout_file, stderr_path.open("w",encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(command, env=environment, text=True, stdout=stdout_file, stderr=stderr_file)
                    with self._lock:
                        self._transcription_processes[audiobook_id] = process
                    chunk_started=time.monotonic(); reported=-1
                    try:
                        while process.poll() is None:
                            if cancel_event is not None and cancel_event.is_set():
                                process.terminate()
                                try: process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill(); process.wait(timeout=5)
                                raise InterruptedError("Transcription cancelled")
                            if time.monotonic()-chunk_started >= timeout:
                                process.terminate()
                                try: process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill(); process.wait(timeout=5)
                                raise subprocess.TimeoutExpired(command, timeout)
                            progress={}
                            try: progress=json.loads(current_progress.read_text(encoding="utf-8"))
                            except (OSError,ValueError,TypeError,json.JSONDecodeError): pass
                            current_percent=max(0,min(100,int(progress.get("percent") or 0))) if progress else reported
                            memory=progress.get("memory") if isinstance(progress,dict) and isinstance(progress.get("memory"),dict) else {}
                            peak=int(memory.get("peak_bytes") or 0) if memory else 0
                            if peak > _STT_MLX_MEMORY_LIMIT_BYTES:
                                process.terminate()
                                try: process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill(); process.wait(timeout=5)
                                raise RuntimeError(f"MLX STT memory safety limit exceeded: {peak / (1024**3):.1f} GiB")
                            if current_percent>=0 and current_percent!=reported:
                                reported=current_percent
                                processed=completed_duration+chunk_duration*current_percent/100.0
                                self._set_transcription_job(audiobook_id, {
                                    "status":"transcribing","ready":False,"phase":"transcribing",
                                    "file":int(chunk["file"]),"file_count":int(chunk["file_count"]),
                                    "chunk":chunk_number,"chunk_count":int(chunk["total_chunks"]),
                                    "chunk_progress_percent":current_percent,
                                    "progress_percent":processed/total_duration*100.0 if total_duration else float(current_percent),
                                    "processed_audio_seconds":processed,
                                    "remaining_audio_seconds":max(0.0,total_duration-processed),
                                    "total_duration":total_duration,"mlx_memory":memory,"started_at":started_at,
                                })
                            time.sleep(.25)
                    finally:
                        with self._lock:
                            if self._transcription_processes.get(audiobook_id) is process:
                                self._transcription_processes.pop(audiobook_id,None)
                stdout=stdout_path.read_text(encoding="utf-8",errors="replace")
                stderr=stderr_path.read_text(encoding="utf-8",errors="replace")
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Transcription cancelled")
                payload=self._valid_stt_chunk_result(result_path)
                if process.returncode!=0 or payload is None:
                    result_path.unlink(missing_ok=True)
                    raise RuntimeError((stderr or stdout).strip()[-1200:] or "Japanese STT failed")
                all_segments.extend(self._shift_transcription_segments(
                    [item for item in (payload.get("segments") or []) if isinstance(item,dict)],
                    float(chunk["global_offset"]),
                ))
                completed_duration += chunk_duration
                if current_audio is not None:
                    current_audio.unlink(missing_ok=True); current_audio=None
                current_progress.unlink(missing_ok=True); current_progress=None

            payload={"schema":"audiobook-stt-v3","model":self.stt_model,"created_at":time.time(),"chunk_seconds":_STT_CHUNK_SECONDS,"segments":all_segments}
            temporary=output.with_suffix(output.suffix+".tmp")
            temporary.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
            temporary.replace(output)
            self._set_transcription_job(audiobook_id,{"status":"ready","ready":True,"progress_percent":100.0,"processed_audio_seconds":total_duration,"remaining_audio_seconds":0.0,"segment_count":len(all_segments)})
            with self.db.connect() as conn:
                links=conn.execute("SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=?",(audiobook_id,)).fetchall()
            for row in links:
                try:
                    self.prepare_alignment(
                        int(row["ln_book_id"]), priority=requested_priority
                    )
                except Exception:
                    continue
        except InterruptedError:
            self._set_transcription_job(audiobook_id,{"status":"cancelled","ready":False,"error":""})
        except subprocess.TimeoutExpired:
            self._set_transcription_job(audiobook_id,{"status":"error","ready":False,"error":"Japanese STT chunk timed out"})
        except Exception as exc:
            self._set_transcription_job(audiobook_id,{"status":"error","ready":False,"error":str(exc)})
        finally:
            if current_audio is not None: current_audio.unlink(missing_ok=True)
            if current_progress is not None: current_progress.unlink(missing_ok=True)
            with self._lock:
                self._transcription_cancel_events.pop(audiobook_id,None)
            if heavy_lease is not None: heavy_lease.release()
            event.set()

    def prepare_transcription(
        self,
        audiobook_id: int,
        *,
        force: bool = False,
        attempt_of: str = "",
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
    ) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        if self._closed_event.is_set():
            raise RuntimeError("Audiobook service is closed")
        requested_priority = WorkPriority(int(priority))
        book = self.book(audiobook_id)
        output = self._transcript_path(audiobook_id)
        if force:
            output.unlink(missing_ok=True)
            shutil.rmtree(self._transcription_checkpoint_dir(output), ignore_errors=True)
        elif self._load_transcript(audiobook_id) is not None:
            return self.transcription_status(audiobook_id)
        with self._lock:
            current = self._transcription_jobs.get(audiobook_id) or {}
            if current.get("status") in {"queued", "transcribing"}:
                current_priority = WorkPriority(int(current.get("priority") or int(WorkPriority.BACKGROUND)))
                if current.get("status") == "queued" and requested_priority < current_priority:
                    current = {**current, "priority": int(requested_priority)}
                    self._transcription_jobs[audiobook_id] = current
                    if audiobook_id in self._transcription_queue:
                        self._transcription_queue = [value for value in self._transcription_queue if value != audiobook_id]
                        self._transcription_queue.append(audiobook_id)
                        self._transcription_queue.sort(
                            key=lambda value: (
                                int((self._transcription_jobs.get(int(value)) or {}).get("priority") or int(WorkPriority.BACKGROUND)),
                                float((self._transcription_jobs.get(int(value)) or {}).get("started_at") or 0.0),
                            )
                        )
                existing = dict(current)
                if current.get("status") == "queued" and audiobook_id in self._transcription_queue:
                    existing["queue_position"] = self._transcription_queue.index(audiobook_id) + 1
                    existing["queue_size"] = len(self._transcription_queue)
                return existing
            event = threading.Event()
            cancel_event = threading.Event()
            started_at = time.time()
            total_duration = sum(
                max(0.0, float(row.get("duration") or 0.0))
                for row in self._file_rows(audiobook_id)
            )
            self._transcription_events[audiobook_id] = event
            self._transcription_cancel_events[audiobook_id] = cancel_event
            job_id = (
                self.job_center.start(
                    "stt",
                    f"STT · {book['title']}",
                    payload={"audiobook_id": audiobook_id},
                    total=total_duration,
                    attempt_of=str(attempt_of or ""),
                    resumable=True,
                    correlation_id=f"audiobook-stt:{audiobook_id}",
                )
                if self.job_center is not None
                else ""
            )
            self._transcription_jobs[audiobook_id] = {
                "status": "queued",
                "ready": False,
                "audiobook_id": audiobook_id,
                "background": True,
                "priority": int(requested_priority),
                "phase": "queued",
                "wait_reason": "",
                "progress_percent": 0.0,
                "processed_audio_seconds": 0.0,
                "remaining_audio_seconds": total_duration,
                "total_duration": total_duration,
                "started_at": started_at,
                "updated_at": started_at,
                "job_id": job_id,
            }
            if audiobook_id not in self._transcription_queue:
                self._transcription_queue.append(audiobook_id)
                self._transcription_queue.sort(
                    key=lambda value: (
                        int((self._transcription_jobs.get(int(value)) or {}).get("priority") or int(WorkPriority.BACKGROUND)),
                        float((self._transcription_jobs.get(int(value)) or {}).get("started_at") or 0.0),
                    )
                )
            queue_position = self._transcription_queue.index(audiobook_id) + 1
            queue_size = len(self._transcription_queue)
        LOGGER.info(
            "Audiobook STT queued audio=%s title=%r priority=%s queue_pos=%s queue_size=%s duration=%.1f",
            audiobook_id, str(book.get("title") or ""), requested_priority.name.casefold(),
            queue_position, queue_size, total_duration,
        )
        self._ensure_transcription_dispatcher()
        return self.transcription_status(audiobook_id)

    def _ensure_transcription_dispatcher(self) -> None:
        with self._lock:
            current = self._transcription_dispatcher
            if current is not None and current.is_alive():
                return
            thread = self._tracked_thread(
                target=self._transcription_dispatch_loop,
                name="audiobook-stt-dispatcher",
            )
            if thread is None:
                return
            self._transcription_dispatcher = thread
        thread.start()

    def _transcription_dispatch_loop(self) -> None:
        last_wait: tuple[int, str] | None = None
        while True:
            with self._lock:
                if self._closed_event.is_set():
                    self._transcription_queue.clear()
                    if self._transcription_dispatcher is threading.current_thread():
                        self._transcription_dispatcher = None
                    return
                if not self._transcription_queue:
                    if self._transcription_dispatcher is threading.current_thread():
                        self._transcription_dispatcher = None
                    return
                # Queue is priority-sorted.  Keep the item in the queue while
                # waiting for foreground/heavy work so a later USER request can
                # move ahead of an older BACKGROUND job.
                audiobook_id = int(self._transcription_queue[0])
                event = self._transcription_events.get(audiobook_id)
                cancel_event = self._transcription_cancel_events.get(audiobook_id)
                job = dict(self._transcription_jobs.get(audiobook_id) or {})
            if event is None:
                with self._lock:
                    self._transcription_queue = [value for value in self._transcription_queue if int(value) != audiobook_id]
                continue
            if cancel_event is not None and cancel_event.is_set():
                with self._lock:
                    self._transcription_queue = [value for value in self._transcription_queue if int(value) != audiobook_id]
                self._set_transcription_job(
                    audiobook_id,
                    {"status": "cancelled", "ready": False, "error": ""},
                )
                event.set()
                continue

            priority = WorkPriority(int(job.get("priority") or int(WorkPriority.BACKGROUND)))
            heavy_lease = None
            if self.work_scheduler is not None:
                if not self.work_scheduler.background_allowed(priority=priority, resource="cpu"):
                    self._set_transcription_job(audiobook_id, {
                        "status": "queued", "ready": False,
                        "phase": "waiting_for_foreground", "wait_reason": "foreground",
                    })
                    if last_wait != (audiobook_id, "foreground"):
                        LOGGER.info(
                            "Audiobook STT waiting audio=%s reason=foreground priority=%s",
                            audiobook_id, priority.name.casefold(),
                        )
                        last_wait = (audiobook_id, "foreground")
                    if cancel_event is not None:
                        cancel_event.wait(0.5)
                    else:
                        time.sleep(0.5)
                    continue
                heavy_lease = self.work_scheduler.acquire_heavy(
                    "audiobook-stt",
                    blocking=False,
                    foreground_sensitive=True,
                    priority=priority,
                    resource="cpu",
                )
                if heavy_lease is None:
                    self._set_transcription_job(audiobook_id, {
                        "status": "queued", "ready": False,
                        "phase": "waiting_for_worker", "wait_reason": "heavy_work",
                    })
                    if last_wait != (audiobook_id, "heavy_work"):
                        LOGGER.info(
                            "Audiobook STT waiting audio=%s reason=heavy_work priority=%s",
                            audiobook_id, priority.name.casefold(),
                        )
                        last_wait = (audiobook_id, "heavy_work")
                    if cancel_event is not None:
                        cancel_event.wait(0.5)
                    else:
                        time.sleep(0.5)
                    continue

            with self._lock:
                # Priority may have changed while probing the scheduler.  If a
                # different job is now first, release and re-evaluate.
                if not self._transcription_queue or int(self._transcription_queue[0]) != audiobook_id:
                    if heavy_lease is not None:
                        heavy_lease.release()
                    continue
                self._transcription_queue.pop(0)
            last_wait = None
            LOGGER.info(
                "Audiobook STT dispatch audio=%s priority=%s",
                audiobook_id, priority.name.casefold(),
            )
            self._transcribe_worker(
                audiobook_id,
                self._transcript_path(audiobook_id),
                event,
                heavy_lease=heavy_lease,
            )

    def cancel_transcription(self, audiobook_id: int) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        with self._lock:
            cancel_event = self._transcription_cancel_events.get(audiobook_id)
            process = self._transcription_processes.get(audiobook_id)
            job = dict(self._transcription_jobs.get(audiobook_id) or {})
        if cancel_event is None:
            return self.transcription_status(audiobook_id)
        cancel_event.set()
        with self._lock:
            was_queued = audiobook_id in self._transcription_queue
            if was_queued:
                self._transcription_queue = [value for value in self._transcription_queue if value != audiobook_id]
        if was_queued:
            self._set_transcription_job(audiobook_id, {"status": "cancelled", "ready": False, "error": ""})
            event = self._transcription_events.get(audiobook_id)
            if event is not None:
                event.set()
        job_id = str(job.get("job_id") or "")
        if self.job_center is not None and job_id:
            self.job_center.request_cancel(job_id)
        if process is not None and process.poll() is None:
            process.terminate()
        return {**self.transcription_status(audiobook_id), "cancel_requested": True}

    def _chapter_start_precision_model(self) -> str:
        override = str(os.getenv("PUDGE_CHAPTER_START_STT_MODEL") or "").strip()
        if override:
            return override
        lowered = self.stt_model.lower()
        if "tiny" in lowered or "base" in lowered:
            return _CHAPTER_START_PRECISION_MODEL
        return self.stt_model

    @staticmethod
    def _alignment_chapter_needs_precision(row: dict[str, Any]) -> bool:
        title = normalize_reading_text(str(row.get("title") or ""))
        if not (title.startswith("第") and title.endswith("幕")):
            return False
        debug = row.get("leading_prefix_debug")
        if not isinstance(debug, dict):
            return True
        reason = str(debug.get("reason") or "")
        if not bool(debug.get("recovered")):
            return True
        if bool(debug.get("degraded")):
            return True
        # A recovered chapter that still had to reconstruct/skip a non-zero
        # opening prefix deserves the precision window too.  v175 considered
        # it "recovered" and therefore never let whisper-small inspect the exact
        # first sentence.
        if bool(debug.get("skipped_ln_prefix")) or bool(debug.get("reconstructed_prose_prefix")):
            return True
        try:
            if int(debug.get("original_first_offset") or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        return reason in {
            "no_prefix_seed",
            "prefix_evidence_too_sparse",
            "marker_hold_fallback",
            "structural_bias_rebase",
        }

    @staticmethod
    def _alignment_chapter_reference_time(row: dict[str, Any]) -> float:
        debug = row.get("leading_prefix_debug")
        if isinstance(debug, dict):
            fallback = debug.get("fallback")
            if isinstance(fallback, dict):
                hazard = fallback.get("hazard")
                if isinstance(hazard, dict):
                    try:
                        return max(0.0, float(hazard.get("left_time") or 0.0))
                    except (TypeError, ValueError):
                        pass
            for key in ("marker_start", "story_start"):
                try:
                    value = float(debug.get(key))
                except (TypeError, ValueError):
                    continue
                if value >= 0.0:
                    return value
        anchors = [item for item in row.get("anchors") or [] if isinstance(item, dict)]
        if anchors:
            try:
                return max(0.0, float(anchors[0].get("time") or 0.0))
            except (TypeError, ValueError):
                pass
        try:
            return max(0.0, float(row.get("start") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _chapter_start_precision_cache_path(
        self,
        alignment_output: Path,
        chapter_index: int,
    ) -> Path:
        return (
            self.cache_dir
            / "reading-audio-chapter-start-stt"
            / f"{alignment_output.stem}-ch{int(chapter_index):04d}.json"
        )

    def _cached_chapter_start_reading_hints(
        self,
        ln_book_id: int,
    ) -> dict[int, list[dict[str, Any]]]:
        """Reuse reader/Jiten parse data to bridge kanji-vs-kana STT spelling."""

        output: dict[int, list[dict[str, Any]]] = {}
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT chapter_index,text_hash FROM ln_chapters "
                "WHERE book_id=? ORDER BY chapter_index",
                (int(ln_book_id),),
            ).fetchall()
            for row in rows:
                digest = str(row["text_hash"] or "")
                if not digest:
                    continue
                cached = conn.execute(
                    "SELECT parsed_json FROM ln_parse_cache WHERE text_hash=?",
                    (digest,),
                ).fetchone()
                if cached is None:
                    continue
                try:
                    parsed = json.loads(str(cached["parsed_json"] or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(parsed, dict):
                    continue
                hints = _reading_hints_from_cached_parse(parsed)
                if hints:
                    output[int(row["chapter_index"])] = hints
        return output

    def maybe_refresh_alignment_after_reader_parse(
        self,
        ln_book_id: int,
        chapter_index: int,
    ) -> bool:
        """Rebuild a degraded chapter start once the canonical reader parse exists.

        The audiobook worker can race ahead of the LN reader.  If the direct
        prefix Jiten request produces zero usable readings, v172 used to keep a
        degraded hold forever even though opening the chapter moments later
        populated ``ln_parse_cache`` with the exact ruby readings needed by the
        phonetic matcher.  Re-run alignment once per alignment fingerprint when
        that canonical cache becomes available.
        """

        ln_book_id = int(ln_book_id)
        chapter_index = int(chapter_index)
        link = self.link_for_light_novel(ln_book_id, include_alignment=False, include_transcription=False)
        if link is None:
            return False
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(ln_book_id, audiobook_id)
        if not isinstance(alignment, dict):
            return False
        chapter_row = next(
            (
                row
                for row in alignment.get("chapters") or []
                if isinstance(row, dict) and int(row.get("chapter_index") or 0) == chapter_index
            ),
            None,
        )
        if not isinstance(chapter_row, dict):
            return False
        debug = chapter_row.get("leading_prefix_debug")
        if not isinstance(debug, dict) or not bool(debug.get("degraded")):
            return False
        if int(debug.get("reading_hint_count") or 0) > 0:
            return False
        reason = str(debug.get("reason") or "")
        if reason not in {
            "no_prefix_seed",
            "prefix_evidence_too_sparse",
            "structural_bias_rebase",
            "marker_hold_fallback",
        }:
            return False

        hints = self._cached_chapter_start_reading_hints(ln_book_id).get(chapter_index) or []
        if not hints:
            return False
        processing = alignment.get("processing") if isinstance(alignment.get("processing"), dict) else {}
        fingerprint = str(processing.get("input_fingerprint") or self._alignment_fingerprint(ln_book_id, audiobook_id))
        refresh_key = (ln_book_id, chapter_index, fingerprint)
        with self._lock:
            if refresh_key in self._reader_parse_alignment_refresh_seen:
                return False
            # Do not let a force refresh race with a currently active build.
            # Leave the key retryable while another build is still running.
            current = self._alignment_jobs.get(ln_book_id) or {}
            if current.get("status") in {"queued", "transcribing", "aligning"}:
                return False
            self._reader_parse_alignment_refresh_seen.add(refresh_key)
            self._alignment_payload_cache.clear()
            report_cache = getattr(self, "_alignment_report_cache", None)
            if isinstance(report_cache, dict):
                report_cache.clear()
        try:
            self.prepare_alignment(ln_book_id, force=True, priority=WorkPriority.USER)
        except Exception:
            with self._lock:
                self._reader_parse_alignment_refresh_seen.discard(refresh_key)
            return False
        return True

    def _chapter_start_reading_cache_path(
        self,
        text_hash: str,
    ) -> Path:
        digest = str(text_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            digest = hashlib.sha256(digest.encode("utf-8", errors="replace")).hexdigest()
        return self.cache_dir / "reading-audio-chapter-start-reading" / f"{digest}.json"

    def _ensure_chapter_start_reading_hints(
        self,
        ln_book_id: int,
        chapter_index: int,
        text: str,
        text_hash: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Get phonetic chapter-start hints without depending on reader load order.

        v171 only reused the full ``ln_parse_cache``.  On a fresh alignment the
        audiobook worker can run before the reader has parsed that chapter, so
        the exact same book later shows furigana in the UI while alignment saw
        zero readings.  For a precision retry, parse only the first few hundred
        normalized characters and cache that tiny result separately.
        """

        normalized_text = chapter_audio_text(str(text or ""))
        digest = str(text_hash or "").strip() or hashlib.sha256(
            normalized_text.encode("utf-8")
        ).hexdigest()

        # First prefer the canonical full-reader cache if it appeared since the
        # initial alignment pass.
        with self.db.connect() as conn:
            try:
                cached = conn.execute(
                    "SELECT parsed_json FROM ln_parse_cache WHERE text_hash=?",
                    (digest,),
                ).fetchone()
            except Exception:
                cached = None
        if cached is not None:
            try:
                parsed = json.loads(str(cached["parsed_json"] or ""))
            except (TypeError, ValueError, json.JSONDecodeError, KeyError, IndexError):
                parsed = None
            if isinstance(parsed, dict):
                hints = _reading_hints_from_cached_parse(
                    parsed, source_limit=_CHAPTER_START_READING_SOURCE_LIMIT
                )
                if hints:
                    return hints, {
                        "source": "ln_parse_cache",
                        "hint_count": len(hints),
                        "chapter_index": int(chapter_index),
                    }

        cache_path = self._chapter_start_reading_cache_path(digest)
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "chapter-start-reading-v1"
            and str(payload.get("text_hash") or "") == digest
            and isinstance(payload.get("hints"), list)
        ):
            hints = [dict(row) for row in payload.get("hints") or [] if isinstance(row, dict)]
            if hints:
                return hints, {
                    "source": "prefix_cache",
                    "hint_count": len(hints),
                    "chapter_index": int(chapter_index),
                }

        with self.db.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM ln_settings WHERE key='jiten_api_key'"
                ).fetchone()
            except Exception:
                row = None
        token = str((row["value"] if row is not None else "") or "").strip()
        if not token:
            return [], {
                "source": "unavailable",
                "hint_count": 0,
                "chapter_index": int(chapter_index),
                "error": "jiten-api-key-unavailable",
            }

        # Keep enough raw text to cover 320 normalized characters even when the
        # EPUB opening contains whitespace/punctuation.
        raw_prefix = normalized_text[: max(640, _CHAPTER_START_READING_SOURCE_LIMIT * 3)]
        paragraphs = [part.strip() for part in raw_prefix.split("\n") if part.strip()]
        if not paragraphs and raw_prefix.strip():
            paragraphs = [raw_prefix.strip()]
        if not paragraphs:
            return [], {
                "source": "unavailable",
                "hint_count": 0,
                "chapter_index": int(chapter_index),
                "error": "empty-chapter-prefix",
            }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"ApiKey {token}",
            "User-Agent": "pudge",
        }
        last_error = ""
        result: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{_CHAPTER_START_JITEN_BASE}/reader/parse",
                    headers=headers,
                    json={"text": paragraphs},
                    timeout=30,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.6 * (2 ** attempt))
                        continue
                response.raise_for_status()
                candidate = response.json() if response.content else {}
                if isinstance(candidate, dict) and not candidate.get("error_message"):
                    result = candidate
                    break
                last_error = str(candidate.get("error_message") or "invalid-jiten-response") if isinstance(candidate, dict) else "invalid-jiten-response"
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(0.6 * (2 ** attempt))

        if result is None:
            return [], {
                "source": "jiten_prefix_api",
                "hint_count": 0,
                "chapter_index": int(chapter_index),
                "error": last_error or "jiten-prefix-parse-failed",
            }

        parsed = {
            "paragraphs": paragraphs,
            "tokens": result.get("tokens") or [],
            "vocabulary": result.get("vocabulary") or [],
        }
        hints = _reading_hints_from_cached_parse(
            parsed, source_limit=_CHAPTER_START_READING_SOURCE_LIMIT
        )
        token_groups = result.get("tokens") or []
        vocabulary_rows = result.get("vocabulary") or []
        reading_field_count = sum(
            1
            for item in vocabulary_rows
            if isinstance(item, dict) and str(item.get("reading") or "").strip()
        ) if isinstance(vocabulary_rows, list) else 0
        token_count = sum(
            len(group) for group in token_groups if isinstance(group, list)
        ) if isinstance(token_groups, list) else 0
        metadata = {
            "source": "jiten_prefix_api",
            "hint_count": len(hints),
            "chapter_index": int(chapter_index),
            "jiten_token_count": int(token_count),
            "jiten_vocabulary_count": int(len(vocabulary_rows)) if isinstance(vocabulary_rows, list) else 0,
            "jiten_reading_field_count": int(reading_field_count),
        }
        if hints:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            saved = {
                "schema": "chapter-start-reading-v1",
                "text_hash": digest,
                "created_at": time.time(),
                "hints": hints,
            }
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(saved, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
        return hints, metadata

    def _extract_global_audio_window(
        self,
        audiobook_id: int,
        destination: Path,
        *,
        start: float,
        duration: float,
    ) -> tuple[float, float]:
        requested_start = max(0.0, float(start))
        requested_end = requested_start + max(0.25, float(duration))
        overlaps: list[dict[str, Any]] = []
        for row in self._file_rows(int(audiobook_id)):
            try:
                row_start = float(row.get("start") or 0.0)
                row_end = float(row.get("end") or row_start)
            except (TypeError, ValueError):
                continue
            overlap_start = max(requested_start, row_start)
            overlap_end = min(requested_end, row_end)
            if overlap_end <= overlap_start + 0.05:
                continue
            overlaps.append(
                {
                    "source": Path(str(row.get("path") or "")),
                    "global_start": overlap_start,
                    "local_start": max(0.0, overlap_start - row_start),
                    "duration": overlap_end - overlap_start,
                }
            )
        if not overlaps:
            raise ValueError("Audiobook precision window does not overlap a readable file")
        actual_start = float(overlaps[0]["global_start"])
        actual_duration = sum(float(row["duration"]) for row in overlaps)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if len(overlaps) == 1:
            row = overlaps[0]
            self._extract_stt_chunk(
                Path(row["source"]),
                destination,
                start=float(row["local_start"]),
                duration=float(row["duration"]),
                cancel_event=None,
            )
            return actual_start, actual_duration

        work = Path(tempfile.mkdtemp(prefix="pudge-chapter-start-parts-", dir=str(destination.parent)))
        try:
            parts: list[Path] = []
            for index, row in enumerate(overlaps):
                part = work / f"part-{index:03d}.flac"
                self._extract_stt_chunk(
                    Path(row["source"]),
                    part,
                    start=float(row["local_start"]),
                    duration=float(row["duration"]),
                    cancel_event=None,
                )
                parts.append(part)
            concat_file = work / "concat.txt"
            concat_file.write_text(
                "".join(f"file '{part.as_posix()}'\\n" for part in parts),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    self._resolved_ffmpeg(),
                    "-v", "error", "-nostdin", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_file),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac",
                    str(destination),
                ],
                text=True,
                capture_output=True,
                timeout=max(60.0, actual_duration * 3.0),
                check=False,
            )
            if completed.returncode != 0 or not destination.is_file():
                raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg concat failed").strip()[-1200:])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return actual_start, actual_duration

    def _precision_chapter_start_segments(
        self,
        audiobook_id: int,
        alignment_output: Path,
        *,
        chapter_index: int,
        reference_time: float,
        total_duration: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        model = self._chapter_start_precision_model()
        cache = self._chapter_start_precision_cache_path(alignment_output, chapter_index)
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cached = None
        if (
            isinstance(cached, dict)
            and cached.get("schema") == "audiobook-chapter-start-stt-v1"
            and str(cached.get("model") or "") == model
            and isinstance(cached.get("segments"), list)
        ):
            return (
                [dict(item) for item in cached.get("segments") or [] if isinstance(item, dict)],
                {"cached": True, "model": model, "window_start": cached.get("window_start"), "window_end": cached.get("window_end")},
            )

        window_start = max(0.0, float(reference_time) - _CHAPTER_START_PRECISION_BEFORE_SECONDS)
        duration_limit = float(total_duration) if float(total_duration) > 0.0 else float(reference_time) + _CHAPTER_START_PRECISION_AFTER_SECONDS
        window_end = min(
            duration_limit,
            float(reference_time) + _CHAPTER_START_PRECISION_AFTER_SECONDS,
        )
        if window_end <= window_start + 0.25:
            return [], {"cached": False, "model": model, "error": "empty-window"}
        cache.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"pudge-chapter-start-{int(chapter_index):04d}-", dir=str(cache.parent)))
        audio = work / "window.flac"
        result_path = work / "result.json"
        progress_path = work / "progress.json"
        try:
            actual_start, actual_duration = self._extract_global_audio_window(
                int(audiobook_id),
                audio,
                start=window_start,
                duration=window_end - window_start,
            )
            command = [
                self.python,
                "-m",
                "pudge.subtitles.stt_worker",
                "--words",
                str(audio),
                str(result_path),
                model,
                str(progress_path),
            ]
            environment = os.environ.copy()
            ffmpeg = self._resolved_ffmpeg()
            environment["PATH"] = os.pathsep.join(
                part
                for part in (str(Path(ffmpeg).resolve().parent), environment.get("PATH", ""))
                if part
            )
            environment.setdefault("PUDGE_MLX_CACHE_LIMIT_BYTES", str(_STT_MLX_CACHE_LIMIT_BYTES))
            environment.setdefault("PUDGE_MLX_MEMORY_LIMIT_BYTES", str(_STT_MLX_MEMORY_LIMIT_BYTES))
            completed = subprocess.run(
                command,
                env=environment,
                text=True,
                capture_output=True,
                timeout=max(20 * 60.0, actual_duration * 12.0),
                check=False,
            )
            payload = self._valid_stt_chunk_result(result_path)
            if completed.returncode != 0 or payload is None:
                raise RuntimeError(
                    (completed.stderr or completed.stdout or "precision chapter-start STT failed").strip()[-1200:]
                )
            shifted = self._shift_transcription_segments(
                [item for item in payload.get("segments") or [] if isinstance(item, dict)],
                actual_start,
            )
            saved = {
                "schema": "audiobook-chapter-start-stt-v1",
                "model": model,
                "chapter_index": int(chapter_index),
                "reference_time": round(float(reference_time), 3),
                "window_start": round(actual_start, 3),
                "window_end": round(actual_start + actual_duration, 3),
                "created_at": time.time(),
                "segments": shifted,
            }
            temporary = cache.with_suffix(cache.suffix + ".tmp")
            temporary.write_text(json.dumps(saved, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(cache)
            return shifted, {
                "cached": False,
                "model": model,
                "window_start": saved["window_start"],
                "window_end": saved["window_end"],
            }
        except Exception as exc:
            LOGGER.warning(
                "Chapter-start precision STT failed audio=%s chapter=%s model=%s: %s",
                audiobook_id,
                chapter_index,
                model,
                exc,
            )
            return [], {"cached": False, "model": model, "error": str(exc)}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _alignment_fingerprint(self, ln_book_id: int, audiobook_id: int) -> str:
        ln_book_id, audiobook_id = int(ln_book_id), int(audiobook_id)
        cache_key = ("alignment", ln_book_id, audiobook_id)
        now = time.monotonic()
        with self._lock:
            cached = self._fingerprint_cache.get(cache_key)
        if cached is not None and now - cached[0] < 5.0:
            return cached[1]
        # Chapter labels are presentation metadata.  Re-parsing an EPUB because
        # a better Contents title was discovered must not invalidate hours of
        # audiobook STT/alignment when the actual chapter text is unchanged.
        digest = hashlib.sha256(
            f"{_READING_AUDIO_ALIGNMENT_REVISION}-text-only\0{self.stt_model}\0".encode()
        )
        with self.db.connect() as conn:
            chapters = conn.execute(
                "SELECT chapter_index,text_hash FROM ln_chapters "
                "WHERE book_id=? ORDER BY chapter_index",
                (int(ln_book_id),),
            ).fetchall()
            files = conn.execute(
                "SELECT file_index,path,start,end FROM audiobook_files "
                "WHERE book_id=? ORDER BY file_index",
                (int(audiobook_id),),
            ).fetchall()
        for row in chapters:
            digest.update(
                f"c:{int(row['chapter_index'])}:{row['text_hash']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        for row in files:
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"a:{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        value = digest.hexdigest()[:28]
        with self._lock:
            self._fingerprint_cache[cache_key] = (now, value)
        return value

    def _alignment_path(self, ln_book_id: int, audiobook_id: int) -> Path:
        fingerprint = self._alignment_fingerprint(int(ln_book_id), int(audiobook_id))
        return self.cache_dir / "reading-audio-alignment" / f"{fingerprint}.json"

    def _alignment_report_path(self, ln_book_id: int, audiobook_id: int) -> Path:
        return self._alignment_path(int(ln_book_id), int(audiobook_id)).with_suffix(".report.json")

    def _load_alignment_report(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any] | None:
        path = self._alignment_report_path(int(ln_book_id), int(audiobook_id))
        cache_key = (int(ln_book_id), int(audiobook_id), path.stem)
        report_cache = getattr(self, "_alignment_report_cache", None)
        if not isinstance(report_cache, dict):
            report_cache = {}
            self._alignment_report_cache = report_cache
        with self._lock:
            if cache_key in report_cache:
                cached = report_cache[cache_key]
                return dict(cached) if isinstance(cached, dict) else None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = None
        result = payload if isinstance(payload, dict) and payload.get("schema") == "pudge-alignment-report-v1" else None
        with self._lock:
            report_cache[cache_key] = dict(result) if isinstance(result, dict) else None
        return dict(result) if isinstance(result, dict) else None

    def _attach_runtime_alignment_context(
        self,
        payload: dict[str, Any],
        ln_book_id: int,
        alignment_output: Path | None = None,
    ) -> None:
        """Attach live LN context used to refine old cached alignment clocks.

        Punctuation is enough to reconstruct silent holds, but it cannot recover
        word timing *inside* a spoken phrase. Chapter-start precision STT already
        exists on disk for recovered starts such as ``第二幕``; pair those cached
        word timestamps with the canonical reader/Jiten hints so the live clock
        can add true word/phrase anchors without re-running STT.
        """

        chapters = {
            int(row.get("chapter_index") or 0): row
            for row in payload.get("chapters") or []
            if isinstance(row, dict)
        }
        if not chapters:
            return

        need_precision_context = any(
            isinstance(row.get("leading_prefix_debug"), dict)
            and bool(row.get("leading_prefix_debug", {}).get("chapter_marker"))
            and (
                "_runtime_reading_hints" not in row
                or "_runtime_precision_segments" not in row
            )
            for row in chapters.values()
        )
        if (
            all("_runtime_punctuation_boundaries" in row for row in chapters.values())
            and not need_precision_context
        ):
            return

        reading_hints_by_chapter = (
            self._cached_chapter_start_reading_hints(int(ln_book_id))
            if need_precision_context
            else {}
        )
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT chapter_index,text FROM ln_chapters "
                "WHERE book_id=? ORDER BY chapter_index",
                (int(ln_book_id),),
            ).fetchall()
        for row in rows:
            chapter_index = int(row["chapter_index"])
            chapter = chapters.get(chapter_index)
            if chapter is None:
                continue
            spoken = chapter_audio_text(str(row["text"] or ""))
            chapter["_runtime_punctuation_boundaries"] = _punctuation_boundaries(spoken)
            chapter.pop("_runtime_enriched_anchors", None)
            chapter.pop("_runtime_punctuation_pause_count", None)
            chapter.pop("_runtime_reading_hint_anchor_count", None)
            chapter.pop("_runtime_reading_hint_debug", None)

            leading_debug = chapter.get("leading_prefix_debug")
            if not (
                need_precision_context
                and isinstance(leading_debug, dict)
                and bool(leading_debug.get("chapter_marker"))
            ):
                continue

            hints = reading_hints_by_chapter.get(chapter_index) or []
            chapter["_runtime_reading_hints"] = [
                dict(item) for item in hints if isinstance(item, dict)
            ]

            precision_segments: list[dict[str, Any]] = []
            if alignment_output is not None:
                cache = self._chapter_start_precision_cache_path(
                    Path(alignment_output),
                    chapter_index,
                )
                try:
                    cached = json.loads(cache.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    cached = None
                if (
                    isinstance(cached, dict)
                    and cached.get("schema") == "audiobook-chapter-start-stt-v1"
                    and isinstance(cached.get("segments"), list)
                ):
                    precision_segments = [
                        dict(item)
                        for item in cached.get("segments") or []
                        if isinstance(item, dict)
                    ]
            chapter["_runtime_precision_segments"] = precision_segments
        payload.pop("_runtime_audio_position_index", None)

    def _load_alignment(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any] | None:
        ln_book_id, audiobook_id = int(ln_book_id), int(audiobook_id)
        path = self._alignment_path(ln_book_id, audiobook_id)
        cache_key = (ln_book_id, audiobook_id, path.stem)
        with self._lock:
            cached = self._alignment_payload_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = None
        if not (isinstance(payload, dict) and payload.get("schema") == "reading-audio-v3"):
            payload = self._migrate_compatible_alignment(ln_book_id, audiobook_id, path)
        if isinstance(payload, dict) and payload.get("schema") == "reading-audio-v3":
            self._attach_runtime_alignment_context(payload, ln_book_id, path)
            with self._lock:
                self._alignment_payload_cache[cache_key] = payload
            return payload
        return None

    def _migrate_compatible_alignment(
        self, ln_book_id: int, audiobook_id: int, destination: Path
    ) -> dict[str, Any] | None:
        """Reuse a pre-title-fingerprint alignment when chapter text is identical.

        v136-v138 included chapter titles in the cache filename, so improving EPUB
        Contents metadata looked like a new book and unnecessarily re-ran matching.
        Validate the cached transcript identity and every matched chapter length,
        then atomically alias that result to the title-insensitive fingerprint.
        """
        root = self.cache_dir / "reading-audio-alignment"
        if not root.is_dir():
            return None
        with self.db.connect() as conn:
            source_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                    (int(ln_book_id),),
                ).fetchall()
            ]
        source_lengths = {
            int(row["chapter_index"]): len(normalize_reading_text(str(row.get("text") or "")))
            for row in source_rows
        }
        if not source_lengths:
            return None
        transcript_fingerprint = self._transcript_fingerprint(int(audiobook_id))
        candidates = sorted(
            (item for item in root.glob("*.json") if not item.name.endswith(".report.json") and item != destination),
            key=lambda item: item.stat().st_mtime_ns if item.exists() else 0,
            reverse=True,
        )[:96]
        for candidate in candidates:
            try:
                cached = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(cached, dict) or cached.get("schema") != "reading-audio-v3":
                continue
            processing = cached.get("processing") if isinstance(cached.get("processing"), dict) else {}
            if str(processing.get("transcript_fingerprint") or "") != transcript_fingerprint:
                continue
            revision = str(processing.get("alignment_algorithm_revision") or "")
            if revision and revision != _READING_AUDIO_ALIGNMENT_REVISION:
                continue
            aligned_rows = [row for row in cached.get("chapters") or [] if isinstance(row, dict)]
            if len(aligned_rows) < 2:
                continue
            # A schema refresh may remove a structural front-matter chapter
            # (notably a plain-text Contents page), shifting every readable
            # chapter index by one while preserving all actual chapter text.
            # Accept only a single constant index shift for which *every*
            # aligned chapter has the exact same normalized length.
            mapped_rows: list[dict[str, Any]] | None = None
            source_titles = {int(row["chapter_index"]): str(row.get("title") or "") for row in source_rows}
            compatible_mappings: list[tuple[int, list[dict[str, Any]]]] = []
            for delta in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8):
                candidate_rows: list[dict[str, Any]] = []
                compatible = True
                for row in aligned_rows:
                    try:
                        old_index = int(row.get("chapter_index") or 0)
                        length = int(row.get("normalized_length") or 0)
                    except (TypeError, ValueError):
                        compatible = False
                        break
                    new_index = old_index + delta
                    if length <= 0 or source_lengths.get(new_index) != length:
                        compatible = False
                        break
                    candidate_rows.append({
                        **row,
                        "chapter_index": new_index,
                        "title": source_titles.get(new_index, str(row.get("title") or "")),
                    })
                if compatible and len(candidate_rows) == len(aligned_rows):
                    compatible_mappings.append((delta, candidate_rows))
            direct = next((rows for delta, rows in compatible_mappings if delta == 0), None)
            if direct is not None:
                mapped_rows = direct
            elif len(compatible_mappings) == 1:
                mapped_rows = compatible_mappings[0][1]
            # Multiple non-zero shifts with identical chapter-length sequences
            # are ambiguous; recomputing once is safer than attaching the wrong
            # audio clock to a neighboring chapter.
            if mapped_rows is None:
                continue
            migrated = dict(cached)
            migrated["chapters"] = mapped_rows
            migrated_processing = dict(processing)
            migrated_processing["alignment_algorithm_revision"] = _READING_AUDIO_ALIGNMENT_REVISION
            migrated_processing["input_fingerprint"] = destination.stem
            migrated_processing["migrated_from_fingerprint"] = candidate.stem
            migrated["processing"] = migrated_processing
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(migrated, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(destination)
            report = build_alignment_report(migrated, [
                {**row, "normalized_length": source_lengths[int(row["chapter_index"])]}
                for row in source_rows
            ])
            report_path = destination.with_suffix(".report.json")
            report_tmp = report_path.with_suffix(".tmp")
            report_tmp.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            report_tmp.replace(report_path)
            return migrated
        return None

    def alignment_status(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False, include_transcription=False)
        if link is None:
            return {"status": "unlinked", "ready": False}
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        if alignment is not None:
            report = self._load_alignment_report(int(ln_book_id), audiobook_id)
            summary = dict(report.get("summary") or {}) if isinstance(report, dict) else {}
            processing = alignment.get("processing") if isinstance(alignment.get("processing"), dict) else {}
            return {
                "status": "ready",
                "ready": True,
                "model": str(alignment.get("model") or self.stt_model),
                "confidence": float(alignment.get("confidence") or 0.0),
                "matched_chapters": len(alignment.get("chapters") or []),
                "anchor_count": int(alignment.get("anchor_count") or 0),
                "timing_source": str(alignment.get("timing_source") or "stt"),
                "quality_grade": str(report.get("grade") or "unknown") if isinstance(report, dict) else "unknown",
                "coverage": float(summary.get("coverage") or 0.0),
                "warning_count": int(summary.get("warning_count") or 0),
                "algorithm_revision": str(
                    processing.get("alignment_algorithm_revision") or ""
                ),
            }
        with self._lock:
            job = dict(self._alignment_jobs.get(int(ln_book_id)) or {})
        if job and int(job.get("audiobook_id") or audiobook_id) != audiobook_id:
            job = {}
        transcription = self.transcription_status(audiobook_id)
        if not transcription.get("ready") and str(transcription.get("status") or "") in {
            "queued",
            "transcribing",
            "error",
        }:
            return {
                **transcription,
                "phase": "transcription",
                "audiobook_id": audiobook_id,
            }
        return job or {"status": "not_prepared", "ready": False}

    @staticmethod
    def _shift_transcription_segments(
        segments: list[dict[str, Any]], offset: float
    ) -> list[dict[str, Any]]:
        shifted: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            clone = dict(segment)
            for key in ("start", "end"):
                try:
                    clone[key] = float(clone.get(key) or 0.0) + float(offset)
                except (TypeError, ValueError):
                    pass
            words = clone.get("words")
            if isinstance(words, list):
                clone["words"] = []
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    word_clone = dict(word)
                    for key in ("start", "end"):
                        try:
                            word_clone[key] = float(word_clone.get(key) or 0.0) + float(offset)
                        except (TypeError, ValueError):
                            pass
                    clone["words"].append(word_clone)
            shifted.append(clone)
        return shifted

    def _alignment_attempt_current_locked(
        self,
        ln_book_id: int,
        audiobook_id: int,
        generation: int,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        if self._closed_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
            return False
        current = self._alignment_jobs.get(int(ln_book_id)) or {}
        return (
            int(current.get("audiobook_id") or -1) == int(audiobook_id)
            and int(current.get("generation") or -1) == int(generation)
            and int(self._alignment_generations.get(int(ln_book_id)) or -1) == int(generation)
            and self._alignment_cancel_events.get(int(ln_book_id)) is cancel_event
        )

    def _alignment_attempt_current(
        self,
        ln_book_id: int,
        audiobook_id: int,
        generation: int,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        with self._lock:
            return self._alignment_attempt_current_locked(
                ln_book_id, audiobook_id, generation, cancel_event
            )

    def _require_alignment_attempt(
        self,
        ln_book_id: int,
        audiobook_id: int,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        if not self._alignment_attempt_current(
            ln_book_id, audiobook_id, generation, cancel_event
        ):
            raise InterruptedError("Alignment attempt superseded")

    def _cancel_alignment_attempt_locked(self, ln_book_id: int) -> None:
        cancel_event = self._alignment_cancel_events.pop(int(ln_book_id), None)
        if cancel_event is not None:
            cancel_event.set()

    def _set_alignment_job(
        self,
        ln_book_id: int,
        audiobook_id: int,
        payload: dict[str, Any],
        *,
        generation: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        with self._lock:
            current = self._alignment_jobs.get(int(ln_book_id)) or {}
            if int(current.get("audiobook_id") or -1) != int(audiobook_id):
                return
            if generation is not None and int(current.get("generation") or -1) != int(generation):
                return
            if cancel_event is not None and self._alignment_cancel_events.get(int(ln_book_id)) is not cancel_event:
                return
            self._alignment_jobs[int(ln_book_id)] = {
                **current,
                **payload,
                "audiobook_id": int(audiobook_id),
            }

    def _prepare_alignment_worker(
        self,
        ln_book_id: int,
        audiobook_id: int,
        output: Path,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        heavy_lease = None
        started_at = time.monotonic()
        with self._lock:
            alignment_job = dict(self._alignment_jobs.get(int(ln_book_id)) or {})
        requested_priority = WorkPriority(
            int(alignment_job.get("priority") or int(WorkPriority.BACKGROUND))
        )
        try:
            self._require_alignment_attempt(
                ln_book_id, audiobook_id, generation, cancel_event
            )
            with self.db.connect() as conn:
                chapters = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT chapter_index,title,text,text_hash FROM ln_chapters "
                        "WHERE book_id=? ORDER BY chapter_index",
                        (int(ln_book_id),),
                    ).fetchall()
                ]
            book = self.book(int(audiobook_id))
            if not chapters:
                raise ValueError("Linked LN or audiobook has no chapters")
            transcript = self._load_transcript(int(audiobook_id))
            if transcript is None:
                self.prepare_transcription(
                    int(audiobook_id), priority=requested_priority
                )
                with self._lock:
                    event = self._transcription_events.get(int(audiobook_id))
                if event is not None:
                    event.wait(timeout=12 * 3600)
                transcript = self._load_transcript(int(audiobook_id))
            if transcript is None:
                status = self.transcription_status(int(audiobook_id))
                raise RuntimeError(str(status.get("error") or "Audiobook transcription is unavailable"))
            all_segments = [
                dict(item)
                for item in transcript.get("segments") or []
                if isinstance(item, dict)
            ]
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "aligning", "ready": False},
                generation=generation,
                cancel_event=cancel_event,
            )
            self._require_alignment_attempt(
                ln_book_id, audiobook_id, generation, cancel_event
            )
            phase_at = time.monotonic()
            speech_regions, timing_source = self._load_or_analyze_activity(
                int(audiobook_id),
                all_segments,
            )
            activity_seconds = time.monotonic() - phase_at
            phase_at = time.monotonic()
            book_duration = float(book["duration"] or 0.0)
            chapter_start_reading_hints = self._cached_chapter_start_reading_hints(int(ln_book_id))
            alignment = align_light_novel_to_transcript(
                chapters,
                all_segments,
                duration=book_duration,
                model=self.stt_model,
                speech_regions=speech_regions,
                chapter_start_reading_hints=chapter_start_reading_hints,
            )
            precision_segments: dict[int, list[dict[str, Any]]] = {}
            precision_metadata: dict[int, dict[str, Any]] = {}
            precision_candidates = [
                row
                for row in alignment.get("chapters") or []
                if isinstance(row, dict) and self._alignment_chapter_needs_precision(row)
            ]
            if precision_candidates:
                self._set_alignment_job(
                    ln_book_id,
                    audiobook_id,
                    {
                        "status": "aligning",
                        "ready": False,
                        "phase": "chapter_start_refinement",
                        "chapter_start_count": len(precision_candidates),
                    },
                    generation=generation,
                    cancel_event=cancel_event,
                )
                chapter_rows = {int(row.get("chapter_index") or 0): row for row in chapters}
                for candidate in precision_candidates:
                    self._require_alignment_attempt(
                        ln_book_id, audiobook_id, generation, cancel_event
                    )
                    chapter_index = int(candidate.get("chapter_index") or 0)
                    reference_time = self._alignment_chapter_reference_time(candidate)
                    reading_metadata: dict[str, Any] = {
                        "source": "existing",
                        "hint_count": len(chapter_start_reading_hints.get(chapter_index) or []),
                    }
                    if not chapter_start_reading_hints.get(chapter_index):
                        source_chapter = chapter_rows.get(chapter_index) or {}
                        hints, reading_metadata = self._ensure_chapter_start_reading_hints(
                            int(ln_book_id),
                            chapter_index,
                            str(source_chapter.get("text") or ""),
                            str(source_chapter.get("text_hash") or ""),
                        )
                        if hints:
                            chapter_start_reading_hints[chapter_index] = hints
                    rows, metadata = self._precision_chapter_start_segments(
                        audiobook_id,
                        output,
                        chapter_index=chapter_index,
                        reference_time=reference_time,
                        total_duration=book_duration,
                    )
                    metadata = {
                        **metadata,
                        "reading_hint_source": str(reading_metadata.get("source") or ""),
                        "reading_hint_count": int(reading_metadata.get("hint_count") or 0),
                        "reading_hint_jiten_token_count": int(reading_metadata.get("jiten_token_count") or 0),
                        "reading_hint_jiten_vocabulary_count": int(reading_metadata.get("jiten_vocabulary_count") or 0),
                        "reading_hint_jiten_reading_field_count": int(reading_metadata.get("jiten_reading_field_count") or 0),
                        **(
                            {"reading_hint_error": str(reading_metadata.get("error") or "")}
                            if reading_metadata.get("error")
                            else {}
                        ),
                    }
                    precision_metadata[chapter_index] = metadata
                    if rows:
                        precision_segments[chapter_index] = rows
                if precision_segments:
                    alignment = align_light_novel_to_transcript(
                        chapters,
                        all_segments,
                        duration=book_duration,
                        model=self.stt_model,
                        speech_regions=speech_regions,
                        chapter_start_segments=precision_segments,
                        chapter_start_reading_hints=chapter_start_reading_hints,
                    )
                    for chapter_row in alignment.get("chapters") or []:
                        if not isinstance(chapter_row, dict):
                            continue
                        chapter_index = int(chapter_row.get("chapter_index") or 0)
                        metadata = precision_metadata.get(chapter_index)
                        if metadata is None:
                            continue
                        debug = chapter_row.get("leading_prefix_debug")
                        if not isinstance(debug, dict):
                            debug = {}
                        chapter_row["leading_prefix_debug"] = {
                            **debug,
                            "precision_model": str(metadata.get("model") or ""),
                            "precision_cached": bool(metadata.get("cached")),
                            "precision_window_start": metadata.get("window_start"),
                            "precision_window_end": metadata.get("window_end"),
                            "reading_hint_source": str(metadata.get("reading_hint_source") or ""),
                            "reading_hint_count": int(metadata.get("reading_hint_count") or 0),
                            "reading_hint_jiten_token_count": int(metadata.get("reading_hint_jiten_token_count") or 0),
                            "reading_hint_jiten_vocabulary_count": int(metadata.get("reading_hint_jiten_vocabulary_count") or 0),
                            "reading_hint_jiten_reading_field_count": int(metadata.get("reading_hint_jiten_reading_field_count") or 0),
                            **(
                                {"reading_hint_error": str(metadata.get("reading_hint_error") or "")}
                                if metadata.get("reading_hint_error")
                                else {}
                            ),
                            **(
                                {"precision_error": str(metadata.get("error") or "")}
                                if metadata.get("error")
                                else {}
                            ),
                        }
            alignment_seconds = time.monotonic() - phase_at
            alignment["timing_source"] = timing_source
            alignment["created_at"] = time.time()
            alignment["processing"] = {
                "schema": "pudge-alignment-pipeline-v1",
                "algorithm": str(alignment.get("schema") or "reading-audio-v3"),
                "alignment_algorithm_revision": _READING_AUDIO_ALIGNMENT_REVISION,
                "input_fingerprint": output.stem,
                "transcript_fingerprint": self._transcript_fingerprint(int(audiobook_id)),
                "transcription_reusable": True,
            }
            report_sources = [
                {
                    **chapter,
                    "normalized_length": len(
                        normalize_reading_text(str(chapter.get("text") or ""))
                    ),
                }
                for chapter in chapters
            ]
            report = build_alignment_report(alignment, report_sources)
            alignment["quality"] = dict(report.get("summary") or {})
            output.parent.mkdir(parents=True, exist_ok=True)
            run_token = f"g{int(generation)}-{threading.get_ident()}-{time.time_ns()}"
            attempt_output = output.with_name(f".{output.name}.{run_token}.tmp")
            report_output = self._alignment_report_path(int(ln_book_id), int(audiobook_id))
            report_temporary = report_output.with_name(
                f".{report_output.name}.{run_token}.tmp"
            )
            try:
                attempt_output.write_text(
                    json.dumps(alignment, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                report_temporary.write_text(
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                with self._lock:
                    if not self._alignment_attempt_current_locked(
                        ln_book_id, audiobook_id, generation, cancel_event
                    ):
                        raise InterruptedError("Alignment attempt superseded")
                    attempt_output.replace(output)
                    report_temporary.replace(report_output)
                    self._alignment_payload_cache.clear()
                    self._alignment_report_cache.clear()
            finally:
                attempt_output.unlink(missing_ok=True)
                report_temporary.unlink(missing_ok=True)
            LOGGER.info(
                "LN audiobook alignment prepared ln=%s audio=%s generation=%s source=%s activity=%.3fs align=%.3fs total=%.3fs anchors=%s",
                ln_book_id, audiobook_id, generation, timing_source, activity_seconds, alignment_seconds,
                time.monotonic() - started_at, int(alignment.get("anchor_count") or 0),
            )
            self._require_alignment_attempt(
                ln_book_id, audiobook_id, generation, cancel_event
            )
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE reading_audio_links SET alignment_mode='stt_acoustic',updated_at=? "
                    "WHERE ln_book_id=? AND audiobook_id=?",
                    (time.time(), int(ln_book_id), int(audiobook_id)),
                )
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {
                    "status": "ready",
                    "ready": True,
                    "confidence": float(alignment.get("confidence") or 0.0),
                    "matched_chapters": len(alignment.get("chapters") or []),
                },
                generation=generation,
                cancel_event=cancel_event,
            )
        except InterruptedError:
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "cancelled", "ready": False, "error": ""},
                generation=generation,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "error", "ready": False, "error": str(exc)},
                generation=generation,
                cancel_event=cancel_event,
            )
        finally:
            if heavy_lease is not None:
                heavy_lease.release()

    def alignment_report(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"status": "unlinked", "ready": False}
        audiobook_id = int(link["book"]["id"])
        report = self._load_alignment_report(int(ln_book_id), audiobook_id)
        if report is None:
            alignment = self._load_alignment(int(ln_book_id), audiobook_id)
            if alignment is None:
                return {"status": "not_prepared", "ready": False}
            with self.db.connect() as conn:
                chapters = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                        (int(ln_book_id),),
                    ).fetchall()
                ]
            report = build_alignment_report(alignment, chapters)
        return {"status": "ready", "ready": True, **report}

    def reprocess_alignment(
        self,
        ln_book_id: int,
        *,
        clear_transcription: bool = False,
    ) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        with self._lock:
            self._cancel_alignment_attempt_locked(int(ln_book_id))
            self._alignment_jobs.pop(int(ln_book_id), None)
        self._alignment_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        self._alignment_report_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        if clear_transcription:
            self._transcript_path(audiobook_id).unlink(missing_ok=True)
        return self.prepare_alignment(int(ln_book_id), force=False)

    def prepare_alignment(
        self,
        ln_book_id: int,
        *,
        force: bool = False,
        priority: WorkPriority | int = WorkPriority.USER,
    ) -> dict[str, Any]:
        if self._closed_event.is_set():
            raise RuntimeError("Audiobook service is closed")
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        requested_priority = WorkPriority(int(priority))
        audiobook_id = int(link["book"]["id"])
        output = self._alignment_path(int(ln_book_id), audiobook_id)
        if force:
            output.unlink(missing_ok=True)
            self._alignment_report_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        if output.is_file():
            return self.alignment_status(int(ln_book_id))
        with self._lock:
            current = self._alignment_jobs.get(int(ln_book_id)) or {}
            if current.get("status") in {"queued", "transcribing", "aligning"}:
                current_priority = WorkPriority(
                    int(current.get("priority") or int(WorkPriority.BACKGROUND))
                )
                if current.get("status") == "queued" and requested_priority < current_priority:
                    current = {**current, "priority": int(requested_priority)}
                    self._alignment_jobs[int(ln_book_id)] = current
                return dict(current)
            self._cancel_alignment_attempt_locked(int(ln_book_id))
            generation = int(self._alignment_generations.get(int(ln_book_id)) or 0) + 1
            cancel_event = threading.Event()
            self._alignment_generations[int(ln_book_id)] = generation
            self._alignment_cancel_events[int(ln_book_id)] = cancel_event
            self._alignment_jobs[int(ln_book_id)] = {
                "status": "queued",
                "ready": False,
                "audiobook_id": audiobook_id,
                "priority": int(requested_priority),
                "generation": generation,
            }
        thread = self._tracked_thread(
            target=self._prepare_alignment_worker,
            args=(int(ln_book_id), audiobook_id, output, generation, cancel_event),
            name=f"reading-audio-align-{int(ln_book_id)}-g{generation}",
        )
        if thread is None:
            raise RuntimeError("Audiobook service is closed")
        thread.start()
        return {"status": "queued", "ready": False, "generation": generation}

    def link_candidates_for_light_novel(self, ln_book_id: int, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        ln_book_id = int(ln_book_id); query = str(query or "").strip(); limit = max(1, min(100, int(limit or 50)))
        with self.db.connect() as conn:
            novel = conn.execute("SELECT id,title,volume,anilist_id FROM ln_books WHERE id=?", (ln_book_id,)).fetchone()
            if novel is None: raise KeyError(f"Unknown light novel id={ln_book_id}")
            audio_rows = conn.execute("SELECT id,title,path,duration FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
            identities = {int(r['local_id']): r for r in conn.execute("SELECT local_id,anilist_id,title FROM media_identities WHERE kind='audiobook'").fetchall()}
            chapter_counts = {int(r['book_id']): int(r['n']) for r in conn.execute("SELECT book_id,COUNT(*) AS n FROM audiobook_chapters GROUP BY book_id").fetchall()}
            ln_text = ''.join(str(r['text'] or '') for r in conn.execute("SELECT text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index LIMIT 3", (ln_book_id,)).fetchall())[:12000]
        def jpkey(text: str) -> str: return re.sub(r"[^ぁ-ゟ゠-ヿ一-鿿]+", "", unicodedata.normalize('NFKC', str(text or '')))
        ln_sample = jpkey(ln_text)
        candidates=[]
        for row in audio_rows:
            aid=int(row['id']); title=str(row['title'] or _audiobook_path_label(str(row['path'])))
            if query and _audiobook_title_match_score(query,title) < 35 and _audiobook_title_key(query) not in _audiobook_title_key(title): continue
            score=_audiobook_title_match_score(str(novel['title']),title); signals=["title"]
            avol=_audiobook_volume(title) or _audiobook_volume(_audiobook_path_label(str(row['path']))); nvol=int(novel['volume'] or 0)
            if avol and nvol:
                if avol==nvol: score+=10; signals.append(f"volume {nvol}")
                # A shared series title is weak evidence when both sides carry
                # explicit, different volume numbers.  Keep the candidate in
                # search results, but push it well below a matching volume.
                else: score-=70; signals.append(f"volume mismatch {avol}/{nvol}")
            ident=identities.get(aid); nid=int(novel['anilist_id']) if novel['anilist_id'] is not None else None
            if ident is not None and ident['anilist_id'] is not None and nid and int(ident['anilist_id'])==nid: score+=30; signals.append('AniList')
            cc=chapter_counts.get(aid,0)
            if cc: signals.append(f"{cc} audio chapters")
            # Existing transcript is useful evidence, but candidate discovery must never launch STT.
            transcript=self._load_transcript(aid)
            if transcript and ln_sample:
                spoken=jpkey(''.join(str(seg.get('text') or '') for seg in (transcript.get('segments') or [])[:160]))[:12000]
                if spoken:
                    grams=lambda x:{x[i:i+4] for i in range(max(0,len(x)-3))}
                    a,b=grams(ln_sample),grams(spoken); overlap=(len(a & b)/max(1,min(len(a),len(b)))) if a and b else 0.0
                    if overlap>0: score+=min(25.0,overlap*100.0); signals.append("audio/text")
            probability=max(0.0,min(0.999,score/125.0))
            candidates.append({'id':aid,'title':title,'duration':float(row['duration'] or 0.0),'chapter_count':cc,'score':round(score,3),'probability':round(probability,4),'signals':signals})
        candidates.sort(key=lambda x:(x['score'],x['title']), reverse=True)
        return candidates[:limit]

    def link_light_novel(
        self,
        ln_book_id: int,
        audiobook_id: int,
        *,
        prepare_alignment: bool = True,
    ) -> dict[str, Any]:
        self.book(int(audiobook_id))
        with self._lock:
            self._cancel_alignment_attempt_locked(int(ln_book_id))
            self._alignment_jobs.pop(int(ln_book_id), None)
            old_process = self._alignment_processes.pop(int(ln_book_id), None)
        if old_process is not None and old_process.poll() is None:
            old_process.terminate()
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at)
                VALUES(?,?,'chapter',?,?)
                ON CONFLICT(ln_book_id) DO UPDATE SET
                    audiobook_id=excluded.audiobook_id,alignment_mode='chapter',updated_at=excluded.updated_at
                """,
                (int(ln_book_id), int(audiobook_id), now, now),
            )
        if prepare_alignment:
            self.prepare_transcription(
                int(audiobook_id), priority=WorkPriority.USER
            )
            self.prepare_alignment(
                int(ln_book_id), priority=WorkPriority.USER
            )
        return {"ok": True, "link": self.link_for_light_novel(int(ln_book_id))}

    def unlink_light_novel(self, ln_book_id: int) -> dict[str, Any]:
        with self._lock:
            self._cancel_alignment_attempt_locked(int(ln_book_id))
            self._alignment_jobs.pop(int(ln_book_id), None)
            process = self._alignment_processes.pop(int(ln_book_id), None)
        if process is not None and process.poll() is None:
            process.terminate()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM reading_audio_links WHERE ln_book_id=?", (int(ln_book_id),))
        return {"ok": True}

    def stop_for_light_novel(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"ok": True, "stopped": False, "audiobook_id": None}
        audiobook_id = int(link["book"]["id"])
        result = self.stop(audiobook_id)
        return {
            "ok": True,
            "stopped": bool(result.get("stopped")),
            "audiobook_id": audiobook_id,
        }

    def link_for_light_novel(
        self, ln_book_id: int, *, include_alignment: bool = True, include_transcription: bool = True
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT audiobook_id,alignment_mode FROM reading_audio_links WHERE ln_book_id=?",
                (int(ln_book_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            book = self.book(int(row["audiobook_id"]), include_transcription=include_transcription)
        except KeyError:
            self.unlink_light_novel(int(ln_book_id))
            return None
        result = {
            "ln_book_id": int(ln_book_id),
            "alignment_mode": str(row["alignment_mode"]),
            "book": book,
        }
        if include_alignment:
            result["alignment"] = self.alignment_status(int(ln_book_id))
        return result

    def _paired_audio_position(self, ln_book_id: int, chapter_index: int, chapter_progress: float) -> tuple[int, float]:
        link = self.link_for_light_novel(int(ln_book_id))
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        book = link["book"]
        alignment = self._load_alignment(int(ln_book_id), int(book["id"]))
        if alignment is not None:
            exact = audio_position_for_light_novel(
                alignment,
                int(chapter_index),
                float(chapter_progress),
            )
            if exact is not None:
                return int(book["id"]), exact
        chapters = book["chapters"] or [{"start": 0.0, "end": float(book["duration"] or 0.0)}]
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM ln_chapters WHERE book_id=?", (int(ln_book_id),)).fetchone()
        ln_count = max(1, int(row[0] if row else 1))
        if ln_count <= 1 or len(chapters) <= 1:
            audio_index = 0
        else:
            audio_index = round(max(0, min(ln_count - 1, int(chapter_index))) / (ln_count - 1) * (len(chapters) - 1))
        chapter = chapters[max(0, min(len(chapters) - 1, audio_index))]
        progress = max(0.0, min(1.0, float(chapter_progress)))
        position = float(chapter["start"]) + progress * max(0.0, float(chapter["end"]) - float(chapter["start"]))
        return int(book["id"]), position

    def play_paired(
        self,
        ln_book_id: int,
        chapter_index: int,
        chapter_progress: float,
        speed: float | None = None,
    ) -> dict[str, Any]:
        audiobook_id, position = self._paired_audio_position(ln_book_id, chapter_index, chapter_progress)
        book = self.book(audiobook_id)
        self.play(audiobook_id, start=position, speed=float(speed or book["speed"] or 1.0))
        return self.paired_state(int(ln_book_id))

    def play_paired_at_offset(
        self,
        ln_book_id: int,
        chapter_index: int,
        character_offset: int,
        speed: float | None = None,
    ) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id))
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        position = (
            audio_position_for_light_novel_offset(
                alignment,
                int(chapter_index),
                int(character_offset),
            )
            if alignment is not None
            else None
        )
        if position is None:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT text FROM ln_chapters WHERE book_id=? AND chapter_index=?",
                    (int(ln_book_id), int(chapter_index)),
                ).fetchone()
            length = max(1, len(str(row["text"] or "")) if row is not None else 1)
            _book_id, position = self._paired_audio_position(
                int(ln_book_id),
                int(chapter_index),
                max(0.0, min(1.0, int(character_offset) / length)),
            )
        book = self.book(audiobook_id)
        if self.is_playing(audiobook_id):
            self.seek_to(audiobook_id, max(0.0, float(position)))
            if speed is not None:
                self.set_speed(audiobook_id, float(speed))
            # "Play from here" is an explicit transport command, not merely a
            # seek.  A paused mpv process must resume immediately after moving.
            self.set_paused(audiobook_id, False)
        else:
            self.play(
                audiobook_id,
                start=max(0.0, float(position)),
                speed=float(speed or book["speed"] or 1.0),
            )
            self.set_position(audiobook_id, max(0.0, float(position)))
        return self.paired_state(int(ln_book_id))

    def _paired_light_novel_chapter_ranges(
        self, ln_book_id: int, audiobook_id: int, duration: float, *, alignment: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if alignment is None:
            alignment = self._load_alignment(int(ln_book_id), int(audiobook_id))
        if alignment is None:
            return []
        ranges: list[dict[str, Any]] = []
        for chapter in alignment.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            try:
                chapter_index = int(chapter.get("chapter_index") or 0)
                start = max(0.0, min(float(duration), float(chapter.get("start") or 0.0)))
                end = max(start, min(float(duration), float(chapter.get("end") or start)))
            except (TypeError, ValueError):
                continue
            if end <= start:
                anchors = [
                    item
                    for item in chapter.get("anchors") or []
                    if isinstance(item, dict) and item.get("time") is not None
                ]
                if anchors:
                    try:
                        start = max(0.0, min(float(duration), float(anchors[0]["time"])))
                        end = max(start, min(float(duration), float(anchors[-1]["time"])))
                    except (TypeError, ValueError, KeyError):
                        continue
            if end <= start:
                continue
            ranges.append(
                {
                    "chapter_index": chapter_index,
                    "title": str(chapter.get("title") or ""),
                    "start": start,
                    "end": end,
                }
            )
        ranges.sort(key=lambda item: (int(item["chapter_index"]), float(item["start"])))
        return ranges

    def paired_state(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False, include_transcription=False)
        if link is None:
            return {"linked": False, "playing": False}
        book = link["book"]
        audiobook_id = int(book["id"])
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(audiobook_id)
        if ipc_path is not None:
            live = self._global_position(audiobook_id, ipc_path)
            if live is not None:
                position = self._reconcile_startup_position(audiobook_id, ipc_path, live)
                self._record_playback_position(audiobook_id, position)
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        lookup_started = time.perf_counter()
        exact = light_novel_position_for_audio(alignment, position) if alignment is not None else None
        position_lookup_ms = round((time.perf_counter() - lookup_started) * 1000.0, 3)
        chapter = self._chapter_for_position(book["chapters"], position)
        if chapter is None:
            chapter_progress = 0.0
            chapter_index = 0
        else:
            span = max(0.001, float(chapter["end"]) - float(chapter["start"]))
            chapter_progress = max(0.0, min(1.0, (position - float(chapter["start"])) / span))
            chapter_index = int(chapter["index"])
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ln_chapters WHERE book_id=?", (int(ln_book_id),)
            ).fetchone()
        ln_count = max(1, int(row[0] if row else 1))
        audio_count = max(1, len(book["chapters"]))
        ln_chapter_index = (
            int(exact["chapter_index"])
            if exact is not None
            else (
                0
                if ln_count <= 1 or audio_count <= 1
                else round(chapter_index / (audio_count - 1) * (ln_count - 1))
            )
        )
        if exact is not None:
            chapter_progress = float(exact["chapter_progress"])
        player_running = self.is_playing(audiobook_id)
        paused = self.is_paused(audiobook_id) if player_running else False
        # "playing" describes the user-visible transport state.  A silent
        # interval or an mpv playlist transition is still playback, not Pause.
        playing = player_running and not paused
        playback_active = self.is_playback_active(audiobook_id) if playing else False
        if alignment is not None:
            quality = alignment.get("quality") if isinstance(alignment.get("quality"), dict) else {}
            report = self._load_alignment_report(int(ln_book_id), audiobook_id)
            chapter_start_debug = None
            for aligned_chapter in alignment.get("chapters") or []:
                if not isinstance(aligned_chapter, dict):
                    continue
                if int(aligned_chapter.get("chapter_index") or 0) != int(ln_chapter_index):
                    continue
                candidate_debug = aligned_chapter.get("leading_prefix_debug")
                if isinstance(candidate_debug, dict):
                    chapter_start_debug = dict(candidate_debug)
                break
            alignment_state = {
                "status": "ready", "ready": True,
                "model": str(alignment.get("model") or self.stt_model),
                "confidence": float(alignment.get("confidence") or 0.0),
                "matched_chapters": len(alignment.get("chapters") or []),
                "anchor_count": int(alignment.get("anchor_count") or 0),
                "timing_source": str(alignment.get("timing_source") or "stt"),
                "quality_grade": str(report.get("grade") or "unknown") if isinstance(report, dict) else "unknown",
                "coverage": float(quality.get("coverage") or 0.0),
                "warning_count": int(quality.get("warning_count") or 0),
                "algorithm_revision": _READING_AUDIO_ALIGNMENT_REVISION,
                "chapter_start_debug": chapter_start_debug,
            }
        else:
            alignment_state = self.alignment_status(int(ln_book_id))
        return {
            "linked": True,
            "playing": playing,
            "playback_active": playback_active,
            "player_running": player_running,
            "paused": paused,
            "audiobook_id": audiobook_id,
            "title": book["title"],
            "position": position,
            "position_lookup_ms": position_lookup_ms,
            "duration": float(book["duration"] or 0.0),
            "chapter_index": chapter_index,
            "ln_chapter_index": ln_chapter_index,
            "chapter_progress": chapter_progress,
            "chapter_char_offset": exact.get("chapter_char_offset") if exact else None,
            "chapter_char_offset_exact": exact.get("chapter_char_offset_exact") if exact else None,
            "chapter_char_count": exact.get("chapter_char_count") if exact else None,
            "anchor_window": exact.get("anchor_window") if exact else None,
            "alignment_mode": "stt" if exact is not None else "chapter",
            "alignment": alignment_state,
            "ln_chapter_ranges": self._paired_light_novel_chapter_ranges(
                int(ln_book_id), audiobook_id, float(book["duration"] or 0.0), alignment=alignment
            ),
            "speed": float(book["speed"] or 1.0),
        }

    def delete_many(self, book_ids: list[int], *, delete_files: bool = False) -> dict[str, Any]:
        ids = list(dict.fromkeys(int(value) for value in book_ids if int(value) > 0))
        if not ids:
            return {"ok": True, "removed": [], "errors": []}

        # Only touch expensive runtime state for jobs/players that actually exist.
        with self._lock:
            active_transcriptions = {book_id for book_id in ids if book_id in self._transcription_cancel_events}
            active_players = {
                book_id for book_id in ids
                if (proc := self._players.get(book_id)) is not None and proc.poll() is None
            }
        for book_id in active_transcriptions:
            self.cancel_transcription(book_id)
        for book_id in active_players:
            try:
                self.stop(book_id)
            except Exception:
                pass

        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT id,path FROM audiobooks WHERE id IN ({placeholders})", ids
            ).fetchall()
            found = {int(row["id"]): Path(str(row["path"])).expanduser() for row in rows}
            for table, column in (
                ("reading_audio_links", "audiobook_id"),
                ("audiobook_bookmarks", "book_id"),
                ("audiobook_files", "book_id"),
                ("audiobook_chapters", "book_id"),
            ):
                conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM audiobooks WHERE id IN ({placeholders})", ids)

        removed = [book_id for book_id in ids if book_id in found]
        if delete_files:
            for source in found.values():
                try:
                    if source.is_dir(): shutil.rmtree(source)
                    elif source.is_file(): source.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"ok": True, "removed": removed, "errors": []}

    def delete(self, book_id: int, *, delete_files: bool = False) -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        self.cancel_transcription(book_id)
        self.stop(book_id)
        source = Path(str(book["path"])).expanduser()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM reading_audio_links WHERE audiobook_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_bookmarks WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobooks WHERE id=?", (book_id,))
        if delete_files:
            try:
                if source.is_dir():
                    shutil.rmtree(source)
                elif source.is_file():
                    source.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True, "book_id": book_id, "files_kept": not bool(delete_files)}
