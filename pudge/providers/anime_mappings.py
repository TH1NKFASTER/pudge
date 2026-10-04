"""Local index of external anime IDs and episode rules (PUDGE_IMPLEMENTATION task 2).

Sources (both public GitHub files, fetched at most once a day with
conditional requests):

* **Fribb/anime-lists** ``anime-list-full.json`` — one row per AniDB id with
  AniList/MAL/TVDB/TMDB ids, TVDB/TMDB season and ``episode_offset``.
  Used for id links.
* **Anime-Lists/anime-lists** ``anime-list-master.xml`` — per AniDB id the
  TheTVDB series (or a sentinel type such as ``movie``/``OVA``/``hentai``), its
  default season (``a`` = absolute numbering), an episode offset and explicit
  ``mapping-list`` rules (``;anidb-tvdb;…`` pairs and start/end/offset
  ranges).  Used for episode rules; Fribb alone does not carry them.

The derived index is a *rebuildable* SQLite file under
``cache_dir/anime-mappings/``.  It is not a second library: nothing here
reads or writes the Pudge database, manual/locked identities or AniList.
AniList ``media_id`` stays the canonical id; this module only answers "what
does AniList X map to elsewhere, and by which rule".

Builds go to a temporary file, are validated, then atomically replace the
index.  A broken download or parse keeps the last good index.  Without any
index every lookup returns nothing and the app behaves as before.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANIME_LISTS_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
INDEX_SCHEMA = 1
REFRESH_SECONDS = 24 * 3600
FAILED_INITIAL_REFRESH_SECONDS = 5 * 60
MIN_FRIBB_ROWS = 5000
MIN_ANIME_LISTS_ROWS = 5000
# anime-lists marks one-off titles by their AniDB type instead of a TVDB id.
TVDB_SENTINELS = {"movie", "ova", "hentai", "music video", "other", "tv special", "web", "unknown", ""}

Fetcher = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]


class AnimeMappingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class EpisodeRule:
    """One AniDB → TVDB/TMDB episode rule from anime-lists ``mapping-list``."""

    target: str  # "tvdb" | "tmdb"
    anidb_season: int
    target_season: int | None  # None when the target side is absolute ("a")
    start: int | None = None
    end: int | None = None
    offset: int | None = None
    pairs: tuple[tuple[int, tuple[int, ...]], ...] = ()  # anidb ep -> target eps (0 = none)


@dataclass(frozen=True)
class ExternalMapping:
    anilist_id: int
    anidb_id: int | None
    mal_id: int | None
    tvdb_id: int | None
    tvdb_season: int | None
    tvdb_absolute: bool
    tvdb_episode_offset: int
    tvdb_sentinel: str  # "" or the anime-lists type marker (movie, OVA, ...)
    tmdb_tv: int | None
    tmdb_season: int | None
    tmdb_episode_offset: int
    media_type: str
    rules: tuple[EpisodeRule, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- parsing


def _int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        return int(text) if text not in {"", "None"} else None
    except (TypeError, ValueError):
        return None


def parse_fribb(payload: bytes) -> list[dict[str, Any]]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, list):
        raise AnimeMappingsError("Fribb anime-list-full.json is not a list")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        season = item.get("season") if isinstance(item.get("season"), dict) else {}
        offset = item.get("episode_offset") if isinstance(item.get("episode_offset"), dict) else {}
        tmdb = item.get("themoviedb_id") if isinstance(item.get("themoviedb_id"), dict) else {}
        rows.append(
            {
                "anidb_id": _int(item.get("anidb_id")),
                "anilist_id": _int(item.get("anilist_id")),
                "mal_id": _int(item.get("mal_id")),
                "tvdb_id": _int(item.get("tvdb_id")),
                "tvdb_season": _int(season.get("tvdb")),
                "tvdb_offset": _int(offset.get("tvdb")) or 0,
                "tmdb_tv": _int(tmdb.get("tv")),
                "tmdb_season": _int(season.get("tmdb")),
                "tmdb_offset": _int(offset.get("tmdb")) or 0,
                "media_type": str(item.get("type") or ""),
            }
        )
    return rows


def _parse_pairs(text: str) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """``;1-4;2-6+7;9-0;`` → ((1,(4,)), (2,(6,7)), (9,(0,)))."""
    pairs = []
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part or "-" not in part:
            continue
        left, right = part.split("-", 1)
        source = _int(left)
        targets = tuple(t for t in (_int(v) for v in right.split("+")) if t is not None)
        if source is not None and targets:
            pairs.append((source, targets))
    return tuple(pairs)


def parse_anime_lists(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise AnimeMappingsError(f"anime-lists XML is invalid: {exc}") from exc
    rows = []
    for node in root.findall("anime"):
        anidb_id = _int(node.get("anidbid"))
        if anidb_id is None:
            continue
        raw_tvdb = str(node.get("tvdbid") or "").strip()
        tvdb_id = _int(raw_tvdb)
        sentinel = "" if tvdb_id is not None else raw_tvdb.casefold()
        if tvdb_id is None and sentinel not in TVDB_SENTINELS:
            sentinel = "unknown"
        season_raw = str(node.get("defaulttvdbseason") or "").strip().casefold()
        tmdb_season_raw = str(node.get("tmdbseason") or "").strip().casefold()
        rules = []
        for mapping in node.findall("mapping-list/mapping"):
            for target in ("tvdb", "tmdb"):
                season_attr = mapping.get(f"{target}season")
                if season_attr is None:
                    continue
                absolute = str(season_attr).strip().casefold() == "a"
                rules.append(
                    {
                        "target": target,
                        "anidb_season": _int(mapping.get("anidbseason")) or 0,
                        "target_season": None if absolute else _int(season_attr),
                        "start": _int(mapping.get("start")),
                        "end": _int(mapping.get("end")),
                        "offset": _int(mapping.get("offset")),
                        "pairs": _parse_pairs(mapping.text or ""),
                    }
                )
        rows.append(
            {
                "anidb_id": anidb_id,
                "tvdb_id": tvdb_id,
                "tvdb_sentinel": sentinel,
                "tvdb_season": None if season_raw in {"", "a"} else _int(season_raw),
                "tvdb_absolute": season_raw == "a",
                "tvdb_offset": _int(node.get("episodeoffset")) or 0,
                "tmdb_tv": _int(node.get("tmdbtv")),
                "tmdb_season": None if tmdb_season_raw in {"", "a"} else _int(tmdb_season_raw),
                "tmdb_offset": _int(node.get("tmdboffset")) or 0,
                "name": (node.findtext("name") or "").strip(),
                "rules": rules,
            }
        )
    return rows


# ---------------------------------------------------------------- index


def _http_fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
    import httpx

    response = httpx.get(url, headers={"User-Agent": "pudge", **headers}, timeout=60, follow_redirects=True)
    return response.status_code, response.content, {k.casefold(): v for k, v in response.headers.items()}


class AnimeMappings:
    def __init__(
        self,
        cache_dir: Path,
        *,
        fetch: Fetcher | None = None,
        now: Callable[[], float] = time.time,
        sources: dict[str, str] | None = None,
        min_rows: dict[str, int] | None = None,
    ) -> None:
        self.root = Path(cache_dir).expanduser() / "anime-mappings"
        self.index_path = self.root / "index.sqlite3"
        self.fetch = fetch or _http_fetch
        self.now = now
        self.sources = sources or {"fribb": FRIBB_URL, "anime_lists": ANIME_LISTS_URL}
        self.min_rows = {"fribb": MIN_FRIBB_ROWS, "anime_lists": MIN_ANIME_LISTS_ROWS, **(min_rows or {})}
        self._refresh_lock = threading.Lock()

    # ---- state
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", dir=str(self.root))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path())

    @contextlib.contextmanager
    def _process_lock(self) -> Iterator[bool]:
        """Cross-process: only one updater builds at a time; others skip."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "update.lock", "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @property
    def available(self) -> bool:
        return self.index_path.is_file()

    # ---- refresh
    def refresh_due(self) -> bool:
        state = self._state()
        checked = float(state.get("checked_at") or 0.0)
        if not self.available:
            return not state.get("last_error") or self.now() - checked >= FAILED_INITIAL_REFRESH_SECONDS
        return self.now() - checked >= REFRESH_SECONDS

    def refresh_if_due(self, *, force: bool = False) -> dict[str, Any]:
        if not force and not self.refresh_due():
            return {"status": "fresh"}
        if not self._refresh_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            with self._process_lock() as locked:
                if not locked:
                    return {"status": "busy"}
                return self._refresh()
        finally:
            self._refresh_lock.release()

    def _refresh(self) -> dict[str, Any]:
        state = self._state()
        state.setdefault("sources", {})
        try:
            payloads: dict[str, bytes] = {}
            changed = False
            for name in ("fribb", "anime_lists"):
                raw = self.root / "raw" / f"{name}.bin"
                meta = state["sources"].get(name) or {}
                headers = {}
                if raw.is_file():
                    if meta.get("etag"):
                        headers["If-None-Match"] = str(meta["etag"])
                    if meta.get("last_modified"):
                        headers["If-Modified-Since"] = str(meta["last_modified"])
                status, body, response_headers = self.fetch(self.sources[name], headers)
                if status == 304 and raw.is_file():
                    payloads[name] = raw.read_bytes()
                    continue
                if status != 200 or not body:
                    raise AnimeMappingsError(f"{name}: HTTP {status}")
                payloads[name] = body
                changed = True
                # Keep a completed fetch even if a later source or index build
                # fails, so retries can use conditional requests and rebuild it.
                raw.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=str(raw.parent))
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                os.replace(tmp, raw)
                state["sources"][name] = {
                    "etag": response_headers.get("etag", ""),
                    "last_modified": response_headers.get("last-modified", ""),
                    "fetched_at": self.now(),
                    "bytes": len(body),
                }
            if changed or not self.available or state.get("last_error"):
                stats = self._build(payloads)
                state["built_at"] = self.now()
                state["stats"] = stats
                result = {"status": "rebuilt", **stats}
            else:
                result = {"status": "not_modified"}
            state["checked_at"] = self.now()
            state.pop("last_error", None)
            self._save_state(state)
            return result
        except Exception as exc:  # noqa: BLE001 - a bad update must keep the good index
            state["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            state["checked_at"] = self.now()
            self._save_state(state)
            return {"status": "error", "error": state["last_error"], "kept_index": self.available}

    def _build(self, payloads: dict[str, bytes]) -> dict[str, Any]:
        fribb = parse_fribb(payloads["fribb"])
        lists = parse_anime_lists(payloads["anime_lists"])
        if len(fribb) < self.min_rows["fribb"] or len(lists) < self.min_rows["anime_lists"]:
            raise AnimeMappingsError(f"suspiciously small source: fribb={len(fribb)} anime_lists={len(lists)}")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".index.", suffix=".sqlite3", dir=str(self.root))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            conn = sqlite3.connect(tmp)
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE fribb(anidb_id INTEGER, anilist_id INTEGER, mal_id INTEGER, tvdb_id INTEGER,
                        tvdb_season INTEGER, tvdb_offset INTEGER, tmdb_tv INTEGER, tmdb_season INTEGER,
                        tmdb_offset INTEGER, media_type TEXT);
                    CREATE INDEX fribb_anilist ON fribb(anilist_id);
                    CREATE INDEX fribb_anidb ON fribb(anidb_id);
                    CREATE INDEX fribb_tvdb ON fribb(tvdb_id);
                    CREATE TABLE lists(anidb_id INTEGER PRIMARY KEY, tvdb_id INTEGER, tvdb_sentinel TEXT,
                        tvdb_season INTEGER, tvdb_absolute INTEGER, tvdb_offset INTEGER, tmdb_tv INTEGER,
                        tmdb_season INTEGER, tmdb_offset INTEGER, name TEXT, rules_json TEXT);
                    CREATE INDEX lists_tvdb ON lists(tvdb_id);
                    """
                )
                conn.executemany(
                    "INSERT INTO fribb VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        (r["anidb_id"], r["anilist_id"], r["mal_id"], r["tvdb_id"], r["tvdb_season"], r["tvdb_offset"],
                         r["tmdb_tv"], r["tmdb_season"], r["tmdb_offset"], r["media_type"])
                        for r in fribb
                    ],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO lists VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (r["anidb_id"], r["tvdb_id"], r["tvdb_sentinel"], r["tvdb_season"], int(r["tvdb_absolute"]),
                         r["tvdb_offset"], r["tmdb_tv"], r["tmdb_season"], r["tmdb_offset"], r["name"],
                         json.dumps(r["rules"]))
                        for r in lists
                    ],
                )
                conn.executemany(
                    "INSERT INTO meta VALUES(?,?)",
                    [("schema", str(INDEX_SCHEMA)), ("built_at", str(self.now())),
                     ("fribb_rows", str(len(fribb))), ("anime_lists_rows", str(len(lists)))],
                )
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            if check != "ok":
                raise AnimeMappingsError(f"index integrity check failed: {check}")
            os.replace(tmp, self.index_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"fribb_rows": len(fribb), "anime_lists_rows": len(lists)}

    # ---- lookups
    def _connect(self) -> sqlite3.Connection | None:
        if not self.available:
            return None
        try:
            conn = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def _mapping(self, conn: sqlite3.Connection, row: sqlite3.Row) -> ExternalMapping:
        lists = None
        if row["anidb_id"] is not None:
            lists = conn.execute("SELECT * FROM lists WHERE anidb_id=?", (row["anidb_id"],)).fetchone()
        rules: tuple[EpisodeRule, ...] = ()
        if lists is not None:
            rules = tuple(
                EpisodeRule(
                    target=item["target"],
                    anidb_season=int(item["anidb_season"]),
                    target_season=item["target_season"],
                    start=item["start"],
                    end=item["end"],
                    offset=item["offset"],
                    pairs=tuple((int(a), tuple(int(v) for v in b)) for a, b in item["pairs"]),
                )
                for item in json.loads(lists["rules_json"] or "[]")
            )
        state = self._state()
        tvdb_id = row["tvdb_id"] if row["tvdb_id"] is not None else (lists["tvdb_id"] if lists is not None else None)
        return ExternalMapping(
            anilist_id=int(row["anilist_id"]),
            anidb_id=row["anidb_id"],
            mal_id=row["mal_id"],
            tvdb_id=tvdb_id,
            tvdb_season=lists["tvdb_season"] if lists is not None else row["tvdb_season"],
            tvdb_absolute=bool(lists["tvdb_absolute"]) if lists is not None else False,
            tvdb_episode_offset=int(lists["tvdb_offset"] if lists is not None else row["tvdb_offset"] or 0),
            tvdb_sentinel=str(lists["tvdb_sentinel"] or "") if lists is not None else "",
            tmdb_tv=row["tmdb_tv"],
            tmdb_season=row["tmdb_season"],
            tmdb_episode_offset=int(row["tmdb_offset"] or 0),
            media_type=str(row["media_type"] or ""),
            rules=rules,
            provenance={
                "ids": "Fribb/anime-lists",
                "episodes": "Anime-Lists/anime-lists" if lists is not None else "",
                "built_at": state.get("built_at"),
            },
        )

    def lookup_anilist(self, media_id: int) -> list[ExternalMapping]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute("SELECT * FROM fribb WHERE anilist_id=? ORDER BY anidb_id", (int(media_id),)).fetchall()
            return [self._mapping(conn, row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def anilist_ids_for_tvdb(self, tvdb_id: int) -> list[int]:
        """All AniList ids on one TVDB series (1:N: seasons, OVAs, movies)."""
        return self._reverse("tvdb_id", tvdb_id)

    def anilist_ids_for_anidb(self, anidb_id: int) -> list[int]:
        return self._reverse("anidb_id", anidb_id)

    def _reverse(self, column: str, value: int) -> list[int]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                f"SELECT DISTINCT anilist_id FROM fribb WHERE {column}=? AND anilist_id IS NOT NULL ORDER BY anilist_id",
                (int(value),),
            ).fetchall()
            return [int(row[0]) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        state = self._state()
        return {
            "available": self.available,
            "checked_at": state.get("checked_at"),
            "built_at": state.get("built_at"),
            "stats": state.get("stats"),
            "last_error": state.get("last_error"),
        }


def refresh_in_background(mappings: AnimeMappings, *, logger: Any = None) -> threading.Thread | None:
    """Daily refresh off the interactive path; returns the started thread or None."""
    if os.environ.get("PUDGE_ANIME_MAPPINGS", "1").strip() == "0" or not mappings.refresh_due():
        return None

    def run() -> None:
        result = mappings.refresh_if_due()
        if logger is not None:
            logger.info("DONE step=anime_mappings.refresh result=%s", json.dumps(result, ensure_ascii=False)[:300])

    thread = threading.Thread(target=run, name="pudge-anime-mappings", daemon=True)
    thread.start()
    return thread


def episode_pairs(rules: Iterable[EpisodeRule], target: str = "tvdb") -> dict[tuple[int, int], tuple[int, int]]:
    """Explicit single-episode pairs: (anidb_season, ep) -> (target_season, ep).

    Only rules with a concrete target season and one non-zero target episode
    are returned; ranges/offsets are left to the numbering layer (task 3).
    """
    out: dict[tuple[int, int], tuple[int, int]] = {}
    for rule in rules:
        if rule.target != target or rule.target_season is None:
            continue
        for source, targets in rule.pairs:
            if len(targets) == 1 and targets[0] > 0:
                out[(rule.anidb_season, source)] = (rule.target_season, targets[0])
    return out
