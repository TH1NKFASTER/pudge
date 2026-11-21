"""Shared account-scoped lexical cards; mutable SRS state stays in live-state APIs."""

import copy
import re
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

from .database_storage import pack_json, unpack_json

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
    # Dictionary entries (vocabulary/{w}/{r}/info) hold no user state and hardly change.
    TTL = 30 * 86400
    NOT_FOUND_TTL = 86400
    RETENTION_SECONDS = 60 * 86400
    MAX_PERSISTENT_ENTRIES = 20000
    # Background refreshes (a batch of the iPhone's words, stale or incomplete cards) go
    # through a paced queue: a token bucket of BACKGROUND_RATE requests a second (bursts of
    # BACKGROUND_BURST), at most BACKGROUND_QUEUE_LIMIT waiting. A word the user opened
    # (foreground) is taken before any of them and is not paced. A refusal with a
    # Retry-After (429, Jiten budget, outage pause) pauses the whole background queue.
    # 1.5/s keeps well inside jiten_client's background share (120 a minute) for parse-ahead.
    BACKGROUND_RATE = 1.5
    BACKGROUND_BURST = 5.0
    BACKGROUND_QUEUE_LIMIT = 300
    # How long a waiting worker sleeps at most before it looks again (shutdown, new work).
    WAIT_SLICE_SECONDS = 0.2

    def __init__(self, db_path, logger=None, clock=None):
        self.path = db_path
        self.logger = logger
        self.lock = threading.RLock()
        self._wake = threading.Condition(self.lock)
        self.clock = clock or time.monotonic
        self.memory = OrderedDict()
        # Last look-up or store: an idle window drops ``memory`` (SQLite still holds every entry).
        self._memory_used_at = time.monotonic()
        self.flights = set()
        self.retry_after = {}
        self._closed = False
        # key -> fetch job; foreground and background queues hold keys in arrival order.
        self._jobs = {}
        self._foreground = deque()
        self._background = deque()
        self._tokens = self.BACKGROUND_BURST
        self._tokens_at = self.clock()
        self._paused_until = 0.0
        self._dropped = 0
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
            self._memory_used_at = time.monotonic()
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
                    data = unpack_json(row[0])
                except ValueError:
                    return None, 0
                self.memory[key] = (data, row[1])
                while len(self.memory) > 2048:
                    self.memory.popitem(last=False)
                self.metric("persistent_hit" if time.time() - row[1] < self.TTL else "stale_hit")
                return copy.deepcopy(data), row[1]
            return None, 0

    def get_many(self, account, pairs):
        """Cached cards of many (wordId, readingIndex) with one query per 400 words; no
        fetch, no per-word log line (a word list asks for hundreds at once)."""
        wanted = {f"id:{int(word)}:{int(reading)}": (int(word), int(reading)) for word, reading in pairs}
        found = {}
        keys = list(wanted)
        with self.lock:
            if self._closed:
                raise RuntimeError("Jiten word repository is closed")
            with sqlite3.connect(self.path, timeout=5) as conn:
                for start in range(0, len(keys), 400):
                    chunk = keys[start : start + 400]
                    rows = conn.execute(
                        "SELECT key,payload FROM jiten_word_cache WHERE account=? AND schema=? "
                        f"AND key IN ({','.join('?' * len(chunk))})",
                        (account, self.SCHEMA, *chunk),
                    ).fetchall()
                    for key, payload in rows:
                        try:
                            data = unpack_json(payload)
                        except ValueError:
                            continue
                        if isinstance(data, dict):
                            found[wanted[key]] = data
        return found

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
                                hit = (unpack_json(row[0]), row[1])
                            except ValueError:
                                pass
                    previous, previous_updated = hit or ({}, 0)
                    current = normalize_card(card)
                    merged = {**previous, **{k: v for k, v in current.items() if v not in (None, "", [])}}
                    merged["all_readings"] = [
                        v
                        for v in dict.fromkeys(
                            [merged.get("primary_reading", "")]
                            + current.get("all_readings", [])
                            + previous.get("all_readings", [])
                        )
                        if v
                    ]
                    updated = (
                        previous_updated
                        if previous.get("dictionary_complete") and not card.get("dictionary_complete")
                        else now
                    )
                    staged[key] = (merged, updated)
                    conn.execute(
                        "INSERT OR REPLACE INTO jiten_word_cache VALUES(?,?,?,?,?)",
                        (*key, self.SCHEMA, pack_json(merged), updated),
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
            self._memory_used_at = time.monotonic()
            for key, hit in staged.items():
                self.memory[key] = hit
                self.memory.move_to_end(key)
            while len(self.memory) > 2048:
                self.memory.popitem(last=False)
            return results

    MEMORY_IDLE_SECONDS = 1800.0

    def release_idle(self, force=False):
        """Forget the in-memory copies after half an hour without a look-up (SQLite keeps them)."""
        if not self.lock.acquire(blocking=False):
            return 0  # in use right now: not idle
        try:
            idle = time.monotonic() - self._memory_used_at >= self.MEMORY_IDLE_SECONDS
            if not self.memory or not (force or idle):
                return 0
            dropped = len(self.memory)
            self.memory = OrderedDict()
            return dropped
        finally:
            self.lock.release()

    def close(self, timeout=5.0):
        """Drain SQLite users and reject late refresh results before DB replacement."""
        if not self.lock.acquire(timeout=max(0.0, float(timeout))):
            raise TimeoutError("Jiten word cache still has an active database operation")
        try:
            self._closed = True
            self._foreground.clear()
            self._background.clear()
            self._jobs.clear()
            self._wake.notify_all()
            self.pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self.lock.release()

    # ------------------------------------------------------------ refresh queue
    def _refill(self, now):
        elapsed = max(0.0, now - self._tokens_at)
        self._tokens = min(self.BACKGROUND_BURST, self._tokens + elapsed * self.BACKGROUND_RATE)
        self._tokens_at = now

    def _next_job(self, now):
        """(key, job) to run now, or (None, seconds to wait). Caller holds the lock."""
        while self._foreground:
            key = self._foreground.popleft()
            job = self._jobs.pop(key, None)
            if job is not None:
                return key, job
        while self._background and self._background[0] not in self._jobs:
            self._background.popleft()  # promoted to the foreground and already run
        if not self._background:
            return None, None
        if now < self._paused_until:
            return None, self._paused_until - now
        self._refill(now)
        if self._tokens < 1.0:
            return None, (1.0 - self._tokens) / self.BACKGROUND_RATE
        self._tokens -= 1.0
        key = self._background.popleft()
        return key, self._jobs.pop(key)

    def _stopping(self):
        return self._closed or bool(getattr(self.pool, "_shutdown", False))

    def _drain(self):
        """One pool task per queued job: run the best job available (foreground first)."""
        with self.lock:
            while True:
                if self._closed:
                    return
                key, job = self._next_job(self.clock())
                if key is not None:
                    break
                if job is None and not self._foreground and not self._background:
                    return
                if self._stopping():
                    return  # shutting down: run what is ready now, never wait for pacing
                self._wake.wait(min(self.WAIT_SLICE_SECONDS, max(0.01, float(job or 0.0))))
        job()

    def _pause(self, seconds, error):
        """A refused background request: hold the background queue back (logged once)."""
        with self.lock:
            until = self.clock() + max(1.0, float(seconds))
            if until <= self._paused_until:
                return
            first = self._paused_until <= self.clock()
            self._paused_until = until
        if first and self.logger:
            self.logger.warning(
                "PAUSE jiten.word_refresh retry_after_s=%.0f queued=%s error=%s",
                float(seconds),
                len(self._background),
                str(error)[:200],
            )

    def _enqueue(self, key, job, background):
        """Queue a fetch; False when the background queue is full (nothing queued)."""
        if background:
            if len(self._background) >= self.BACKGROUND_QUEUE_LIMIT:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    self.metric("refresh_dropped")
                return False
            self._background.append(key)
        else:
            self._foreground.append(key)
        self._jobs[key] = job
        self._wake.notify_all()
        self.pool.submit(self._drain)
        return True

    def resolve(self, account, card, fetch=None, background=False):
        """The cached (or lexical) card now; a missing/stale entry is fetched on the queue.

        `background`: a refresh nobody is waiting for (paced, dropped when the queue is
        full, paused on a rate limit). A failure there never reaches the user: the usable
        card is kept.
        """
        cached, updated = self.get(account, card)
        complete = bool(cached and cached.get("dictionary_complete"))
        ttl = self.NOT_FOUND_TTL if cached and cached.get("not_found") else self.TTL
        if fetch and (not complete or time.time() - updated >= ttl):
            key = (account, self.key(card))
            with self.lock:
                if time.time() < self.retry_after.get(key, 0):
                    return {**card, **(cached or normalize_card(card))}
                if key not in self.flights:

                    def worker(as_background=background):
                        requeued = False
                        try:
                            self.metric("network_fetch")
                            if as_background:
                                from . import jiten_client

                                # Never wait for the Jiten budget on a worker the reader needs:
                                # a refusal pauses this queue instead.
                                with jiten_client.background_requests(), jiten_client.wait_limit(0.0):
                                    result = fetch()
                            else:
                                result = fetch()
                            if result:
                                self.put(account, result)
                        except Exception as exc:  # noqa: BLE001 - isolated refresh must preserve the usable card
                            wait = float(getattr(exc, "retry_after", 0.0) or 0.0)
                            if as_background and wait > 0:
                                # Rate limited / Jiten paused: retry this word after the pause,
                                # do not fail every queued word one by one.
                                self.metric("refresh_paused")
                                self._pause(wait, exc)
                                with self.lock:
                                    if not self._stopping() and key not in self._jobs:
                                        self._background.appendleft(key)
                                        self._jobs[key] = worker
                                        self.pool.submit(self._drain)
                                        requeued = True
                                return
                            # A timeout/5xx never replaces the last usable card.
                            self.metric("refresh_error")
                            if self.logger:
                                self.logger.warning("FAIL jiten.word_cache key=%s error=%s", key[1], str(exc))
                            with self.lock:
                                self.retry_after[key] = time.time() + 30
                        finally:
                            if not requeued:
                                with self.lock:
                                    self.flights.discard(key)

                    if self._enqueue(key, worker, background):
                        self.flights.add(key)
                elif not background and key in self._jobs and key in self._background:
                    # The user opened a word that waits in the background queue: take it next.
                    job = self._jobs[key]
                    self._jobs[key] = lambda job=job: job(False)
                    self._foreground.append(key)
                    self._wake.notify_all()
                    self.metric("refresh_promoted")
                else:
                    self.metric("inflight_join")
        return {**card, **(cached or normalize_card(card))}
