"""Manual volume and series corrections for light novels, manga and audiobooks.

Volume and series are inferred from file names and titles. The heuristics are
good but not perfect, so a correction made by hand is stored here and wins over
every heuristic (including the automatic light-novel volume repair).
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Callable
from .i18n import err

KINDS = frozenset({"ln", "manga", "audiobook"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_overrides (
    kind TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    volume INTEGER,
    series_title TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY(kind, item_id)
);
"""


_KANJI_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_KANJI_VOLUME_RE = re.compile(r"([〇零一二三四五六七八九十百]{1,5})(?=\s*巻)")
# 第三部 ("part three"): only after 第, so ordinary words such as 一部 stay as they are.
_KANJI_PART_RE = re.compile(r"(?<=第)(\s*)([〇零一二三四五六七八九十百]{1,5})(?=\s*部)")


def kanji_number(token: str) -> int:
    total = 0
    number = 0
    for char in token:
        if char == "百":
            total += (number or 1) * 100
            number = 0
        elif char == "十":
            total += (number or 1) * 10
            number = 0
        else:
            number = number * 10 + _KANJI_DIGITS[char]
    return total + number


def kanji_volume_digits(value: str, *, parts: bool = False) -> str:
    """第十二巻 -> 第12巻 (kanji numerals only directly before 巻); with `parts` (light
    novels), also 第三部 -> 第3部."""
    text = _KANJI_VOLUME_RE.sub(lambda match: str(kanji_number(match.group(1))), str(value or ""))
    if parts:
        text = _KANJI_PART_RE.sub(lambda match: match.group(1) + str(kanji_number(match.group(2))), text)
    return text


def ensure_schema(conn: Any) -> None:
    conn.executescript(SCHEMA)


def _table_exists(conn: Any) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_overrides'").fetchone()
        is not None
    )


def load(conn: Any, kind: str) -> dict[int, dict[str, Any]]:
    """All overrides of one library kind, keyed by item id."""
    if not _table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT item_id,volume,series_title FROM library_overrides WHERE kind=?", (str(kind),)
    ).fetchall()
    return {
        int(row[0]): {
            "volume": int(row[1]) if row[1] is not None else None,
            "series_title": str(row[2] or ""),
        }
        for row in rows
    }


def store(conn: Any, kind: str, item_id: int, *, volume: int | None, series_title: str) -> dict[str, Any]:
    kind = str(kind)
    if kind not in KINDS:
        raise ValueError("Unknown library kind")
    if volume is not None:
        volume = int(volume)
        if not 1 <= volume <= 999:
            raise ValueError(err("library.volumeRange"))
    series_title = unicodedata.normalize("NFC", " ".join(str(series_title or "").split()))[:200]
    ensure_schema(conn)
    if volume is None and not series_title:
        conn.execute("DELETE FROM library_overrides WHERE kind=? AND item_id=?", (kind, int(item_id)))
    else:
        conn.execute(
            "INSERT INTO library_overrides(kind,item_id,volume,series_title,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(kind,item_id) DO UPDATE SET volume=excluded.volume,"
            "series_title=excluded.series_title,updated_at=excluded.updated_at",
            (kind, int(item_id), volume, series_title, time.time()),
        )
    return {"volume": volume, "series_title": series_title}


def forget(conn: Any, kind: str, item_ids: list[int]) -> None:
    if not _table_exists(conn):
        return
    conn.executemany(
        "DELETE FROM library_overrides WHERE kind=? AND item_id=?",
        [(str(kind), int(item_id)) for item_id in item_ids],
    )


def apply(
    item: dict[str, Any], override: dict[str, Any] | None, series_key: Callable[[str], str]
) -> dict[str, Any]:
    """Apply one override to a library payload (``volume``/``series_title``/``series_key``)."""
    if not override:
        return item
    if override.get("volume") is not None:
        item["volume"] = int(override["volume"])
    title = str(override.get("series_title") or "")
    if title:
        item["series_title"] = title
        item["series_key"] = series_key(title) or item.get("series_key") or f"book:{item.get('id')}"
    item["manual_metadata"] = True
    item["manual_volume"] = override.get("volume")
    item["manual_series_title"] = title
    return item


def apply_all(
    items: list[dict[str, Any]],
    overrides: dict[int, dict[str, Any]],
    series_key: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Apply overrides to a whole library list.

    A corrected series name joins the shelf that already carries that name,
    even when that shelf is grouped by its AniList id.
    """
    if not overrides:
        return items
    shelves: dict[str, str] = {}
    for item in items:
        if int(item.get("id") or 0) in overrides or not item.get("series_key"):
            continue
        key = series_key(str(item.get("series_title") or ""))
        if key:
            shelves.setdefault(key, str(item["series_key"]))
    for item in items:
        override = overrides.get(int(item.get("id") or 0))
        if not override:
            continue
        apply(item, override, series_key)
        key = series_key(str(override.get("series_title") or ""))
        if key and key in shelves:
            item["series_key"] = shelves[key]
    return items
