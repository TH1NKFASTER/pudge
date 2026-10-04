"""Shared account-scoped lexical cards; mutable SRS state stays in live-state APIs."""

import copy
import json
import re
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

FIELDS = {
    "wordId",
    "readingIndex",
    "spelling",
    "wordText",
    "wordTextPlain",
    "reading",
    "readings",
    "meanings",
    "meaningsChunks",
    "partsOfSpeech",
    "partOfSpeech",
    "pitchAccent",
    "pitchAccents",
    "frequencyRank",
    "examples",
    "exampleSentences",
    "primary_reading",
    "all_readings",
    "dictionary_complete",
    "not_found",
    "pudgeContextImage",
    "mainReading",
    "alternativeReadings",
    "definitions",
}


def kana(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"([\u3400-\u9fff々〆ヵヶ]+)\[([^\]]+)\]", lambda m: m[2], text)
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text if not c.isspace())


def normalize_card(card):
    out = {key: copy.deepcopy(value) for key, value in card.items() if key in FIELDS}
    rows = list(card.get("readings") or [])
    main = card.get("mainReading")
    if isinstance(main, dict):
        rows.insert(0, main)
    rows.extend(card.get("alternativeReadings") or [])

    def reading(row):
        return (
            (row.get("reading") or row.get("rubyText") or row.get("text") or "")
            if isinstance(row, dict)
            else row
        )

    primary = next(
        (
            reading(row)
            for row in rows
            if isinstance(row, dict) and row.get("readingIndex") == card.get("readingIndex")
        ),
        "",
    )
    primary = kana(card.get("primary_reading") or card.get("reading") or primary)
    values = [primary] + list(card.get("all_readings") or []) + [reading(row) for row in rows]
    # WordDto alternatives contain both kana readings and bare kanji spellings.
    # Bare spellings are not pronunciations; ruby-marked forms are converted above.
    readings = list(
        dict.fromkeys(
            kana(value)
            for value in values
            if kana(value) and not re.search(r"[\u3400-\u9fff々〆ヵヶ]", kana(value))
        )
    )
    if rows:
        out["readings"] = rows
    out["primary_reading"] = primary or next(iter(readings), "")
    out["all_readings"] = readings
    return out


class JitenWordRepository:
    SCHEMA = 2
    TTL = 86400
    RETENTION_SECONDS = 30 * 86400
    MAX_PERSISTENT_ENTRIES = 20000

    def __init__(self, db_path, logger=None):
        self.path = db_path
        self.logger = logger
        self.lock = threading.RLock()
        self.memory = OrderedDict()
        self.flights = set()
        self.retry_after = {}
        self._closed = False
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="jiten-word")
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jiten_word_cache (account TEXT, key TEXT, schema INTEGER, payload TEXT, updated REAL, PRIMARY KEY(account,key))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS jiten_word_cache_updated ON jiten_word_cache(updated)")

    def key(self, card):
        word = card.get("wordId")
        if word:
            return f"id:{word}:{card.get('readingIndex', 0)}"
        return (
            "text:"
            + str(card.get("spelling") or card.get("wordTextPlain") or card.get("wordText") or "").strip()
            + ":"
            + kana(card.get("reading"))
        )

    def metric(self, source):
        if self.logger:
            self.logger.info("EVENT jiten.word_cache source=%s", source)

    def get(self, account, card):
        key = (account, self.key(card))
        with self.lock:
            if self._closed:
                raise RuntimeError("Jiten word repository is closed")
            hit = self.memory.get(key)
            if hit:
                self.memory.move_to_end(key)
                self.metric("memory_hit" if time.time() - hit[1] < self.TTL else "stale_hit")
                return copy.deepcopy(hit[0]), hit[1]
            with sqlite3.connect(self.path, timeout=5) as conn:
                row = conn.execute(
                    "SELECT payload,updated FROM jiten_word_cache WHERE account=? AND key=? AND schema=?",
                    (*key, self.SCHEMA),
                ).fetchone()
            if row:
                try:
                    data = json.loads(row[0])
                except ValueError:
                    return None, 0
                self.memory[key] = (data, row[1])
                while len(self.memory) > 2048:
                    self.memory.popitem(last=False)
                self.metric("persistent_hit" if time.time() - row[1] < self.TTL else "stale_hit")
                return copy.deepcopy(data), row[1]
            return None, 0

    def put(self, account, card):
        return self.put_many(account, [card])[0]

    def put_many(self, account, cards):
        """Persist one due-card response atomically without caching live SRS state."""
        if not cards:
            return []
        with self.lock:
            if self._closed:
                raise RuntimeError("Jiten word repository is closed")
            staged = {}
            results = []
            now = time.time()
            with sqlite3.connect(self.path, timeout=5) as conn:
                for card in cards:
                    key = (account, self.key(card))
                    hit = staged.get(key) or self.memory.get(key)
                    if hit is None:
                        row = conn.execute(
                            "SELECT payload,updated FROM jiten_word_cache WHERE account=? AND key=? AND schema=?",
                            (*key, self.SCHEMA),
                        ).fetchone()
                        if row:
                            try:
                                hit = (json.loads(row[0]), row[1])
                            except ValueError:
                                pass
                    previous, previous_updated = hit or ({}, 0)
                    current = normalize_card(card)
                    merged = {**previous, **{k: v for k, v in current.items() if v not in (None, "", [])}}
                    merged["all_readings"] = [v for v in dict.fromkeys(
                        [merged.get("primary_reading", "")]
                        + current.get("all_readings", [])
                        + previous.get("all_readings", [])
                    ) if v]
                    updated = (previous_updated if previous.get("dictionary_complete")
                               and not card.get("dictionary_complete") else now)
                    staged[key] = (merged, updated)
                    conn.execute(
                        "INSERT OR REPLACE INTO jiten_word_cache VALUES(?,?,?,?,?)",
                        (*key, self.SCHEMA, json.dumps(merged, ensure_ascii=False), updated),
                    )
                    results.append(copy.deepcopy(merged))
                conn.execute(
                    "DELETE FROM jiten_word_cache WHERE schema<>? OR updated<?",
                    (self.SCHEMA, now - self.RETENTION_SECONDS),
                )
                conn.execute(
                    "DELETE FROM jiten_word_cache WHERE rowid IN "
                    "(SELECT rowid FROM jiten_word_cache ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET ?)",
                    (self.MAX_PERSISTENT_ENTRIES,),
                )
            # Publish memory entries only after the whole transaction commits.
            for key, hit in staged.items():
                self.memory[key] = hit
                self.memory.move_to_end(key)
            while len(self.memory) > 2048:
                self.memory.popitem(last=False)
            return results

    def close(self, timeout=5.0):
        """Drain SQLite users and reject late refresh results before DB replacement."""
        if not self.lock.acquire(timeout=max(0.0, float(timeout))):
            raise TimeoutError("Jiten word cache still has an active database operation")
        try:
            self._closed = True
            self.pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self.lock.release()

    def resolve(self, account, card, fetch=None):
        cached, updated = self.get(account, card)
        complete = bool(cached and cached.get("dictionary_complete"))
        ttl = 60 if cached and cached.get("not_found") else self.TTL
        if fetch and (not complete or time.time() - updated >= ttl):
            key = (account, self.key(card))
            with self.lock:
                if time.time() < self.retry_after.get(key, 0):
                    return {**card, **(cached or normalize_card(card))}
                if key not in self.flights:
                    self.flights.add(key)

                    def worker():
                        try:
                            self.metric("network_fetch")
                            result = fetch()
                            if result:
                                self.put(account, result)
                        except Exception as exc:  # noqa: BLE001 - isolated refresh must preserve the usable card
                            # A timeout/5xx never replaces the last usable card.
                            self.metric("refresh_error")
                            if self.logger:
                                self.logger.warning("FAIL jiten.word_cache key=%s error=%s", key[1], str(exc))
                            with self.lock:
                                self.retry_after[key] = time.time() + 30
                        finally:
                            with self.lock:
                                self.flights.discard(key)

                    self.pool.submit(worker)
                else:
                    self.metric("inflight_join")
        return {**card, **(cached or normalize_card(card))}
