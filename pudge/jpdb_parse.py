"""Convert jpdb.io ``/parse`` responses into Pudge study tokens.

Contract (official jpdb OpenAPI, verified against the live API 2026-09-27):

* request ``text`` may be a list; ``tokens`` then has one row per string;
* each token is ``[vocabulary_index, position, length, furigana]`` in the
  requested ``position_length_encoding``;
* ``furigana`` is ``null`` or a list of plain strings and ``[base, reading]``
  pairs that together spell the token surface;
* punctuation and unparsed spans get no token (gaps are normal);
* ``card_state`` is ``null`` for vocabulary outside the user's decks, else a
  list such as ``["known"]`` or ``["redundant", "known"]``.

Pudge readers index text with JavaScript (UTF-16) offsets, the same as Jiten
``start``/``end``, so Pudge requests ``utf16`` and keeps those offsets.
Identity is jpdb-native ``(vid, sid)``; it is exposed through the shared
``wordId``/``readingIndex`` keys only together with ``idNamespace="jpdb"``.
"""

from __future__ import annotations

from typing import Any

JPDB_PARSER_SCHEMA = "jpdb-v1"
JPDB_POSITION_ENCODING = "utf16"
JPDB_TOKEN_FIELDS = ["vocabulary_index", "position", "length", "furigana"]
JPDB_VOCABULARY_FIELDS = [
    "vid",
    "sid",
    "rid",
    "spelling",
    "reading",
    "frequency_rank",
    "meanings",
    "card_state",
    "card_level",
    "due_at",
    "part_of_speech",
]


class JpdbParseError(ValueError):
    pass


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text: str, start: int, end: int) -> str:
    raw = text.encode("utf-16-le")
    if start < 0 or end < start or end * 2 > len(raw):
        raise JpdbParseError("token range is outside the paragraph")
    try:
        return raw[start * 2 : end * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise JpdbParseError("token range splits a surrogate pair") from exc


def _meanings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, list):
            out.append("; ".join(str(part) for part in item if isinstance(part, str)))
    return [item for item in out if item]


def _states(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).casefold() for item in value if isinstance(item, str) and item]


def vocabulary_entry(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, list) or len(row) < len(JPDB_VOCABULARY_FIELDS):
        return None
    fields = dict(zip(JPDB_VOCABULARY_FIELDS, row))
    try:
        vid = int(fields["vid"])
        sid = int(fields["sid"])
    except (TypeError, ValueError):
        return None
    states = _states(fields["card_state"])
    frequency = fields["frequency_rank"]
    return {
        "wordId": vid,
        "readingIndex": sid,
        "vid": vid,
        "sid": sid,
        "rid": fields["rid"],
        "spelling": str(fields["spelling"] or ""),
        "word": str(fields["spelling"] or ""),
        "reading": str(fields["reading"] or ""),
        "frequencyRank": int(frequency) if isinstance(frequency, int) else None,
        "meanings": _meanings(fields["meanings"]),
        "cardState": states,
        "knownState": states,
        "inDeck": fields["card_state"] is not None,
        "cardLevel": fields["card_level"],
        "dueAt": fields["due_at"],
        "partOfSpeech": [str(x) for x in fields["part_of_speech"] or [] if isinstance(x, str)],
        "idNamespace": "jpdb",
        "sourceProvider": "jpdb",
    }


def _rubies(furigana: Any, start: int, length: int) -> list[dict[str, Any]]:
    if not isinstance(furigana, list):
        return []
    rubies: list[dict[str, Any]] = []
    position = start
    for part in furigana:
        if isinstance(part, str):
            position += utf16_len(part)
        elif (
            isinstance(part, list)
            and len(part) == 2
            and isinstance(part[0], str)
            and isinstance(part[1], str)
        ):
            size = utf16_len(part[0])
            rubies.append({"start": position, "end": position + size, "text": part[1]})
            position += size
        else:
            return []
    # Furigana must spell exactly the token surface; otherwise drop it rather
    # than paint readings over the wrong characters.
    return rubies if position == start + length else []


def convert_parse_result(paragraphs: list[str], result: Any) -> dict[str, Any]:
    """Return ``{"tokens": [...per paragraph...], "vocabulary": [...]}``."""
    if not isinstance(result, dict):
        raise JpdbParseError("jpdb parse returned a non-object response")
    rows = result.get("tokens")
    raw_vocabulary = result.get("vocabulary")
    if not isinstance(rows, list) or not isinstance(raw_vocabulary, list):
        raise JpdbParseError("jpdb parse response lacks tokens/vocabulary")
    if len(rows) != len(paragraphs):
        raise JpdbParseError("jpdb parse returned a token-row count that does not match the request")
    vocabulary = [vocabulary_entry(row) for row in raw_vocabulary]
    token_groups: list[list[dict[str, Any]]] = []
    for paragraph, row in zip(paragraphs, rows):
        if not isinstance(row, list):
            raise JpdbParseError("jpdb parse returned a malformed token row")
        group: list[dict[str, Any]] = []
        for raw in row:
            if not isinstance(raw, list) or len(raw) < 4:
                raise JpdbParseError("jpdb parse returned a malformed token")
            index, start, length, furigana = raw[0], raw[1], raw[2], raw[3]
            if not all(isinstance(value, int) for value in (index, start, length)):
                raise JpdbParseError("jpdb parse returned non-integer token fields")
            if not 0 <= index < len(vocabulary) or length <= 0:
                raise JpdbParseError("jpdb parse returned an invalid token")
            card = vocabulary[index]
            if card is None:
                raise JpdbParseError("jpdb parse token points at malformed vocabulary")
            end = start + length
            surface = utf16_slice(paragraph, start, end)
            group.append(
                {
                    "wordId": card["vid"],
                    "readingIndex": card["sid"],
                    "start": start,
                    "end": end,
                    "length": length,
                    "surface": surface,
                    "text": surface,
                    "reading": card["reading"],
                    "rubies": _rubies(furigana, start, length),
                    "card": dict(card),
                    "idNamespace": "jpdb",
                    "sourceProvider": "jpdb",
                }
            )
        token_groups.append(group)
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for card in vocabulary:
        if card is not None:
            unique.setdefault((card["vid"], card["sid"]), card)
    return {"tokens": token_groups, "vocabulary": list(unique.values())}


def normalized_jpdb_state(states: list[str], in_deck: bool) -> str:
    lowered = {str(state).casefold() for state in states}
    if not in_deck and not lowered:
        return "new"
    if "blacklisted" in lowered:
        return "blacklisted"
    if lowered & {"due", "failed"}:
        return "due"
    if lowered & {"known", "never-forget"}:
        return "known"
    if "learning" in lowered:
        return "learning"
    if "new" in lowered:
        return "new"
    # suspended / locked / unrecognized states are not guessed into new/known.
    return "unknown"
