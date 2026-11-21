"""What the words of parsed texts are called: spelling, reading, a meaning, parts of speech.

Word units (consumption_words) keep only (wordId, readingIndex). The word lists built from
them ("Episode words", "Met often, not in deck") have to name the words without asking
Jiten for each one, so the vocabulary of every Jiten parse Pudge reads is kept here once:
a small table in the light-novel database, next to the parse cache it is taken from.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

Pair = tuple[int, int]

# JMdict/JMnedict tags of proper names (Jiten sends them as parts of speech).
NAME_TAGS = frozenset(
    {
        "name",
        "n-pr",
        "place",
        "person",
        "surname",
        "given",
        "fem",
        "masc",
        "company",
        "organization",
        "product",
        "station",
        "work",
        "char",
        "group",
        "unclass",
        "proper noun",
    }
)
_KANJI_RUBY = re.compile(r"[㐀-鿿々〆ヵヶ]+\[([^\]]*)\]")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS word_lexicon("
    "word_id INTEGER NOT NULL, reading_index INTEGER NOT NULL, spelling TEXT NOT NULL DEFAULT '',"
    "reading TEXT NOT NULL DEFAULT '', meaning TEXT NOT NULL DEFAULT '', pos TEXT NOT NULL DEFAULT '',"
    "frequency_rank INTEGER, updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(word_id, reading_index))",
    "CREATE TABLE IF NOT EXISTS word_lexicon_sources(digest TEXT PRIMARY KEY, updated_at REAL NOT NULL DEFAULT 0)",
)


def _first_meaning(card: dict[str, Any]) -> str:
    for name in ("meanings", "meaningsChunks", "definitions"):
        value: Any = card.get(name)
        # Lists of strings, of lists of strings, or of {"meanings": [...]} / {"text": ...}.
        for _depth in range(4):
            if isinstance(value, list):
                value = value[0] if value else ""
            elif isinstance(value, dict):
                value = value.get("text") or value.get("meaning") or value.get("meanings") or ""
            else:
                break
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return ""


def _parts_of_speech(card: dict[str, Any]) -> list[str]:
    values = card.get("partsOfSpeech")
    if not isinstance(values, list):
        values = [card.get("partOfSpeech")] if card.get("partOfSpeech") else []
        for row in card.get("definitions") or []:
            if isinstance(row, dict) and isinstance(row.get("partsOfSpeech"), list):
                values = [*values, *row["partsOfSpeech"]]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def is_proper_name(parts_of_speech: Iterable[Any]) -> bool:
    return any(str(value or "").strip().casefold() in NAME_TAGS for value in parts_of_speech or [])


def word_info(card: Any) -> dict[str, Any]:
    """The few fields a word list shows, from a parse's vocabulary item or a dictionary card."""
    card = card if isinstance(card, dict) else {}
    spelling = str(card.get("spelling") or card.get("wordTextPlain") or card.get("wordText") or "")
    spelling = re.sub(r"\[[^\]]*\]", "", spelling).replace(" ", "")
    reading = str(card.get("reading") or card.get("primary_reading") or "")
    # Bracket notation 漢字[かんじ]: keep only the kana.
    reading = _KANJI_RUBY.sub(r"\1", reading).replace(" ", "")
    pos = _parts_of_speech(card)
    rank = card.get("frequencyRank", card.get("frequency_rank"))
    try:
        rank = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None
    return {
        "spelling": spelling,
        "reading": reading,
        "meaning": _first_meaning(card) or str(card.get("meaning") or "")[:120],
        "pos": pos,
        "is_name": is_proper_name(pos),
        "frequency_rank": rank,
    }


def ensure_schema(conn: Any) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def remember(conn: Any, vocabulary: Iterable[Any], *, digest: str = "") -> int:
    """Keep the vocabulary of one parse. ``digest`` (the parse's cache key) makes a parse
    read again cost one look-up: its words are written once."""
    ensure_schema(conn)
    if digest and conn.execute("SELECT 1 FROM word_lexicon_sources WHERE digest=?", (digest,)).fetchone():
        return 0
    now = time.time()
    rows = []
    for item in vocabulary or []:
        if not isinstance(item, dict):
            continue
        try:
            pair = (int(item.get("wordId")), int(item.get("readingIndex")))
        except (TypeError, ValueError):
            continue
        info = word_info(item)
        if pair[0] <= 0 or pair[1] < 0 or not info["spelling"]:
            continue
        rows.append(
            (
                *pair,
                info["spelling"],
                info["reading"],
                info["meaning"],
                "\n".join(info["pos"]),
                info["frequency_rank"],
                now,
            )
        )
    if rows:
        # A later parse fills what an earlier one lacked (a meaning, parts of speech).
        conn.executemany(
            "INSERT INTO word_lexicon(word_id,reading_index,spelling,reading,meaning,pos,frequency_rank,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(word_id,reading_index) DO UPDATE SET "
            "spelling=excluded.spelling,"
            "reading=CASE WHEN excluded.reading<>'' THEN excluded.reading ELSE word_lexicon.reading END,"
            "meaning=CASE WHEN excluded.meaning<>'' THEN excluded.meaning ELSE word_lexicon.meaning END,"
            "pos=CASE WHEN excluded.pos<>'' THEN excluded.pos ELSE word_lexicon.pos END,"
            "frequency_rank=COALESCE(excluded.frequency_rank,word_lexicon.frequency_rank),"
            "updated_at=excluded.updated_at",
            rows,
        )
    if digest:
        conn.execute(
            "INSERT OR REPLACE INTO word_lexicon_sources(digest,updated_at) VALUES(?,?)", (digest, now)
        )
    return len(rows)


def lookup(conn: Any, pairs: Iterable[Pair]) -> dict[Pair, dict[str, Any]]:
    """Kept words among ``pairs`` (a few queries however many words)."""
    ensure_schema(conn)
    wanted = list(dict.fromkeys((int(word), int(reading)) for word, reading in pairs or []))
    found: dict[Pair, dict[str, Any]] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start : start + 400]
        marks = ",".join("(?,?)" for _ in chunk)
        rows = conn.execute(
            "SELECT word_id,reading_index,spelling,reading,meaning,pos,frequency_rank FROM word_lexicon "
            f"WHERE (word_id,reading_index) IN (VALUES {marks})",
            [value for pair in chunk for value in pair],
        ).fetchall()
        for row in rows:
            pos = [value for value in str(row[5] or "").split("\n") if value]
            found[(int(row[0]), int(row[1]))] = {
                "spelling": str(row[2] or ""),
                "reading": str(row[3] or ""),
                "meaning": str(row[4] or ""),
                "pos": pos,
                "is_name": is_proper_name(pos),
                "frequency_rank": row[6],
            }
    return found
