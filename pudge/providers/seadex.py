"""Release recommendations (PUDGE_IMPLEMENTATION task 4), from the API set in
Settings → Sources ("recommendations"); off without an address.

The API is a PocketBase app. One request per AniList title:

    GET <recommendations>/api/collections/entries/records
        ?filter=alID=<anilist id>&expand=trs&perPage=500&skipTotal=true

Each entry expands ``trs`` (torrents) with ``tracker`` (the public tracker or a
private one), ``infoHash`` (``<redacted>`` for private trackers), ``url``,
``releaseGroup`` and ``isBest``. The test fixture is a recorded response.

Only torrents of the public tracker with a real info hash or a ``/view/<id>``
link can match a Pudge search result.  ``isBest`` → "preferred", other listed
torrents → "alternative".  A group name alone never matches.

Cache (``cache_dir/seadex/<id>.json``): a found entry is fresh for 24 h, a
confirmed "no entry" for 1 h.  A network error is *not* "no entry": the last
good snapshot is used (marked stale, with its age); without one the result is
"unavailable" and search continues unchanged.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..fsutil import atomic_write_json

ENTRIES_PATH = "/api/collections/entries/records"
FOUND_TTL_SECONDS = 24 * 3600
MISSING_TTL_SECONDS = 3600
PREFERRED_BONUS = 60.0
ALTERNATIVE_BONUS = 40.0
MAX_REDIRECTS = 3
# A release page link of the RSS search format, on any host (mirrors differ).
_NYAA_VIEW_RE = re.compile(r"^https?://[^/\s]+/view/(\d+)", re.IGNORECASE)
_HASH_RE = re.compile(r"^[0-9a-f]{40}$")

Fetcher = Callable[[str, dict[str, Any], float], tuple[int, Any]]


@dataclass(frozen=True)
class SeaDexTorrent:
    """One public-tracker torrent of an entry: enough to offer it without a search hit."""

    info_hash: str = ""
    url: str = ""
    release_group: str = ""
    best: bool = False
    # (file name, length in bytes) as SeaDex lists them; empty when unknown.
    files: tuple[tuple[str, int], ...] = ()
    grouped_url: str = ""


@dataclass(frozen=True)
class SeaDexRecommendation:
    anilist_id: int
    status: str  # "found" | "missing" | "unavailable"
    preferred_hashes: frozenset[str] = frozenset()
    alternative_hashes: frozenset[str] = frozenset()
    preferred_nyaa_ids: frozenset[str] = frozenset()
    alternative_nyaa_ids: frozenset[str] = frozenset()
    best_groups: tuple[str, ...] = ()
    notes: str = ""
    entry_url: str = ""
    fetched_at: float = 0.0
    stale: bool = False
    error: str = ""
    raw_counts: dict[str, int] = field(default_factory=dict)
    torrents: tuple[SeaDexTorrent, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == "found"

    def match(self, info_hash: str, link: str = "") -> str:
        """ "preferred" | "alternative" | "" for one release (exact hash / Nyaa id)."""
        digest = str(info_hash or "").strip().casefold()
        view = _nyaa_view_id(link)
        if digest and digest in self.preferred_hashes or view and view in self.preferred_nyaa_ids:
            return "preferred"
        if digest and digest in self.alternative_hashes or view and view in self.alternative_nyaa_ids:
            return "alternative"
        return ""


def _nyaa_view_id(url: str) -> str:
    match = _NYAA_VIEW_RE.search(str(url or ""))
    return match.group(1) if match else ""


def parse_entries(
    anilist_id: int, payload: Any, *, fetched_at: float, base_url: str = ""
) -> SeaDexRecommendation:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("recommendations response has no items list")
    entries = [
        item for item in items if isinstance(item, dict) and int(item.get("alID") or 0) == int(anilist_id)
    ]
    if not entries:
        return SeaDexRecommendation(anilist_id=int(anilist_id), status="missing", fetched_at=fetched_at)
    sets = {"ph": set(), "ah": set(), "pn": set(), "an": set()}
    groups: list[str] = []
    counts = {"torrents": 0, "nyaa": 0, "private": 0}
    notes = []
    entry_id = ""
    torrent_rows: list[SeaDexTorrent] = []
    for entry in entries:
        entry_id = entry_id or str(entry.get("id") or "")
        if entry.get("notes"):
            notes.append(str(entry["notes"]))
        torrents = ((entry.get("expand") or {}).get("trs")) or []
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            counts["torrents"] += 1
            best = bool(torrent.get("isBest"))
            group = str(torrent.get("releaseGroup") or "").strip()
            if best and group and group not in groups:
                groups.append(group)
            if str(torrent.get("tracker") or "").casefold() != "nyaa":
                counts["private"] += 1
                continue  # private trackers cannot match a public Nyaa result
            counts["nyaa"] += 1
            digest = str(torrent.get("infoHash") or "").strip().casefold()
            view = _nyaa_view_id(str(torrent.get("url") or ""))
            if _HASH_RE.match(digest):
                sets["ph" if best else "ah"].add(digest)
            if view:
                sets["pn" if best else "an"].add(view)
            files = tuple(
                (str(item.get("name") or ""), max(0, int(item.get("length") or 0)))
                for item in (torrent.get("files") or [])
                if isinstance(item, dict) and item.get("name")
            )
            torrent_rows.append(
                SeaDexTorrent(
                    info_hash=digest if _HASH_RE.match(digest) else "",
                    url=str(torrent.get("url") or ""),
                    release_group=group,
                    best=best,
                    files=files,
                    grouped_url=str(torrent.get("groupedUrl") or ""),
                )
            )
    # A torrent listed as best is never also an "alternative".
    sets["ah"] -= sets["ph"]
    sets["an"] -= sets["pn"]
    return SeaDexRecommendation(
        anilist_id=int(anilist_id),
        status="found",
        preferred_hashes=frozenset(sets["ph"]),
        alternative_hashes=frozenset(sets["ah"]),
        preferred_nyaa_ids=frozenset(sets["pn"]),
        alternative_nyaa_ids=frozenset(sets["an"]),
        best_groups=tuple(groups),
        notes="\n".join(notes)[:2000],
        entry_url=f"{base_url.rstrip('/')}/{int(anilist_id)}/" if entry_id and base_url else "",
        fetched_at=fetched_at,
        raw_counts=counts,
        torrents=tuple(torrent_rows),
    )


def _to_json(rec: SeaDexRecommendation) -> dict[str, Any]:
    return {
        "anilist_id": rec.anilist_id,
        "status": rec.status,
        "preferred_hashes": sorted(rec.preferred_hashes),
        "alternative_hashes": sorted(rec.alternative_hashes),
        "preferred_nyaa_ids": sorted(rec.preferred_nyaa_ids),
        "alternative_nyaa_ids": sorted(rec.alternative_nyaa_ids),
        "best_groups": list(rec.best_groups),
        "notes": rec.notes,
        "entry_url": rec.entry_url,
        "fetched_at": rec.fetched_at,
        "raw_counts": rec.raw_counts,
        "torrents": [
            {
                "info_hash": item.info_hash,
                "url": item.url,
                "release_group": item.release_group,
                "best": item.best,
                "files": [[name, length] for name, length in item.files],
                "grouped_url": item.grouped_url,
            }
            for item in rec.torrents
        ],
    }


def _from_json(data: dict[str, Any]) -> SeaDexRecommendation:
    return SeaDexRecommendation(
        anilist_id=int(data["anilist_id"]),
        status=str(data["status"]),
        preferred_hashes=frozenset(data.get("preferred_hashes") or []),
        alternative_hashes=frozenset(data.get("alternative_hashes") or []),
        preferred_nyaa_ids=frozenset(data.get("preferred_nyaa_ids") or []),
        alternative_nyaa_ids=frozenset(data.get("alternative_nyaa_ids") or []),
        best_groups=tuple(data.get("best_groups") or []),
        notes=str(data.get("notes") or ""),
        entry_url=str(data.get("entry_url") or ""),
        fetched_at=float(data.get("fetched_at") or 0.0),
        raw_counts=dict(data.get("raw_counts") or {}),
        torrents=tuple(
            SeaDexTorrent(
                info_hash=str(item.get("info_hash") or ""),
                url=str(item.get("url") or ""),
                release_group=str(item.get("release_group") or ""),
                best=bool(item.get("best")),
                files=tuple((str(name), int(length)) for name, length in item.get("files") or []),
                grouped_url=str(item.get("grouped_url") or ""),
            )
            for item in data.get("torrents") or []
            if isinstance(item, dict)
        ),
    )


def _http_fetch(
    url: str, params: dict[str, Any], timeout: float, *, transport: Any = None
) -> tuple[int, Any]:
    import httpx

    from ..branding import APP_SLUG

    headers = {"User-Agent": APP_SLUG, "Accept": "application/json"}
    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        response = client.get(url, params=params)
        # The address from Settings → Sources can answer with a redirect: every
        # lookup used to end "HTTP 301" and the recommendations never applied.
        # Follow a few hops, each to https only, so a redirect can never move
        # the request to plain http.
        for _hop in range(MAX_REDIRECTS):
            if not response.is_redirect:
                break
            target = response.url.join(response.headers.get("location", ""))
            if target.scheme != "https":
                raise RuntimeError(f"HTTP {response.status_code} redirect to {target.scheme or 'unknown'}")
            response = client.get(target)
        if response.is_redirect:
            raise RuntimeError(f"HTTP {response.status_code} too many redirects")
    return response.status_code, (response.json() if response.status_code == 200 else None)


class SeaDexClient:
    def __init__(
        self,
        cache_dir: Path,
        *,
        base_url: str | Callable[[], str] = "",
        fetch: Fetcher | None = None,
        now: Callable[[], float] = time.time,
        found_ttl_seconds: float = FOUND_TTL_SECONDS,
    ) -> None:
        self._base_url = base_url
        self.root = Path(cache_dir).expanduser() / "seadex"
        self.fetch = fetch or _http_fetch
        self.now = now
        self.found_ttl = float(found_ttl_seconds)

    @property
    def base_url(self) -> str:
        value = self._base_url() if callable(self._base_url) else self._base_url
        return str(value or "").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _path(self, anilist_id: int) -> Path:
        return self.root / f"{int(anilist_id)}.json"

    def _read(self, anilist_id: int) -> SeaDexRecommendation | None:
        try:
            data = json.loads(self._path(anilist_id).read_text(encoding="utf-8"))
            rec = _from_json(data)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if rec.status == "found" and "torrents" not in data:
            from dataclasses import replace

            # Written before the torrents were kept: it cannot offer a release
            # as a candidate, so the next interactive search fetches it again.
            return replace(rec, stale=True)
        return rec

    def _write(self, rec: SeaDexRecommendation) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path(rec.anilist_id), _to_json(rec), ensure_ascii=False)

    def cached(self, anilist_id: int) -> SeaDexRecommendation | None:
        """Cache only: used by automatic paths; returns stale snapshots too."""
        rec = self._read(anilist_id)
        if rec is None:
            return None
        ttl = self.found_ttl if rec.status == "found" else MISSING_TTL_SECONDS
        if self.now() - rec.fetched_at > ttl:
            from dataclasses import replace

            return replace(rec, stale=True)
        return rec

    def recommendations(
        self, anilist_id: int, *, allow_network: bool = True, timeout: float = 3.0
    ) -> SeaDexRecommendation:
        anilist_id = int(anilist_id)
        base_url = self.base_url
        if not base_url:
            # Off without an address: not even an old cached answer is used.
            return SeaDexRecommendation(anilist_id=anilist_id, status="unavailable", error="not_configured")
        cached = self.cached(anilist_id)
        if cached is not None and not cached.stale:
            return cached
        if not allow_network:
            return cached or SeaDexRecommendation(
                anilist_id=anilist_id, status="unavailable", error="not_cached"
            )
        params = {"filter": f"alID={anilist_id}", "expand": "trs", "perPage": 500, "skipTotal": "true"}
        try:
            status, payload = self.fetch(base_url + ENTRIES_PATH, params, float(timeout))
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            rec = parse_entries(anilist_id, payload, fetched_at=self.now(), base_url=base_url)
        except Exception as exc:  # noqa: BLE001 - 403/429/timeout keep search working
            from dataclasses import replace

            error = f"{type(exc).__name__}: {exc}"[:200]
            if cached is not None:
                return replace(cached, stale=True, error=error)
            return SeaDexRecommendation(anilist_id=anilist_id, status="unavailable", error=error)
        self._write(rec)
        return rec


def bonus_for(rec: SeaDexRecommendation | None, info_hash: str, link: str = "") -> tuple[float, list[str]]:
    """Additive score and stable reasons for one release (pure)."""
    if rec is None or not rec.usable:
        return 0.0, []
    kind = rec.match(info_hash, link)
    if kind == "preferred":
        return PREFERRED_BONUS, ["seadex-exact", "seadex-preferred", f"seadex-bonus={int(PREFERRED_BONUS)}"]
    if kind == "alternative":
        return ALTERNATIVE_BONUS, [
            "seadex-exact",
            "seadex-alternative",
            f"seadex-bonus={int(ALTERNATIVE_BONUS)}",
        ]
    return 0.0, []
