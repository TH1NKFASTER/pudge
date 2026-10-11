"""Per-title OP/ED fingerprint library (kept when episode files are deleted).

``intro_skipper`` finds an opening/ending as audio shared by two episodes on
disk.  Watched episodes are deleted, so a newly downloaded episode often has
no partner on disk.  For every title whose episodes are still expected
(``episodes_expected``) this library keeps compact fingerprint data — never
audio — under ``cache_dir/intro-skipper/library/<media_id>/``:

* ``v-<id>.npz`` — fingerprint of one OP/ED *variant* (only the segment,
  ~4 KB per 90 s); several variants per kind, because the OP/ED can change
  within a season;
* ``e<episode>-head|tail.npz`` — the episode's head/tail fingerprint while
  its region is not explained by a confirmed variant (needed for
  cross-episode detection of a new variant); ~15 KB per region;
* ``library.json`` — variants, the episodes each was heard in, and which
  variant explains which episode region.

A variant heard in ``CONFIRM_EPISODES`` consecutive episodes is confirmed:
the raw regions it explains are dropped.  Raw regions are bounded per title
(``MAX_RAW_EPISODES``), variants per kind (``MAX_VARIANTS``), the whole
library by ``LIBRARY_MAX_BYTES``.  The folder is not registered in the cache
quota; ``prune_libraries`` deletes it when the title is Completed/Dropped
and no more episodes are expected, or when the title leaves the library.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .audio_fingerprints import (
    ENGINE_VERSION,
    HOP_SECONDS,
    HOP_SIZE,
    SAMPLE_RATE,
    AudioFingerprint,
    best_shared_run,
    load_fingerprint,
    save_fingerprint,
)

LIBRARY_VERSION = 1
CONFIRM_EPISODES = 2
MAX_RAW_EPISODES = 6
MAX_VARIANTS = 4
LIBRARY_MAX_BYTES = 32 * 1024 * 1024
MIN_VARIANT_SHARE = 0.5
KEEP_STATUSES = {"CURRENT", "PLANNING", "REPEATING", "PAUSED"}
AIRING_STATUSES = {"RELEASING", "NOT_YET_RELEASED", "HIATUS"}
REGIONS = (("intro", "head", 0), ("outro", "tail", 1))
KIND_REGION = {kind: region for kind, region, _index in REGIONS}

FingerprintSet = tuple[AudioFingerprint, AudioFingerprint, float]


def episodes_expected(anime: Any) -> bool:
    """Watching/Planning/Repeating/Paused, or a title that is still airing (unless Dropped)."""
    status = str(getattr(anime, "status", "") or "").upper()
    if status == "DROPPED":
        return False
    if status in KEEP_STATUSES:
        return True
    airing = str(getattr(anime, "media_status", "") or "").upper() in AIRING_STATUSES
    if status == "COMPLETED":
        return airing
    return airing or str(getattr(anime, "media_status", "") or "").upper() != "FINISHED"


def empty_fingerprint(start_seconds: float = 0.0) -> AudioFingerprint:
    import numpy as np

    return AudioFingerprint(np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=bool), float(start_seconds), 0.0)


def slice_fingerprint(fp: AudioFingerprint, start: float, end: float) -> AudioFingerprint:
    first = max(0, fp.index_at(start))
    last = min(fp.size, fp.index_at(end) + 1)
    if last <= first:
        return empty_fingerprint(start)
    return AudioFingerprint(
        fp.codes[first:last].copy(),
        fp.silent[first:last].copy(),
        fp.start_seconds + first * HOP_SIZE / SAMPLE_RATE,
        (last - first) * HOP_SECONDS,
    )


def match_variant(
    region: AudioFingerprint,
    variant: AudioFingerprint,
    *,
    min_seconds: float,
    max_seconds: float,
    max_bit_error: float,
) -> Any:
    """Run of ``region`` carrying ``variant`` (None when it covers too little of it)."""
    if region.size < 8 or variant.size < 8:
        return None
    run = best_shared_run(
        region, variant, min_seconds=min_seconds, max_seconds=max_seconds, max_bit_error=max_bit_error
    )
    if run is None or run.duration < MIN_VARIANT_SHARE * variant.duration_seconds:
        return None
    return run


def _consecutive(episodes: Iterable[int], count: int) -> bool:
    values = sorted(set(int(e) for e in episodes))
    streak = 0
    previous: int | None = None
    for value in values:
        streak = streak + 1 if previous is not None and value == previous + 1 else 1
        previous = value
        if streak >= count:
            return True
    return False


@dataclass(frozen=True)
class VariantRef:
    id: str
    kind: str
    fingerprint: AudioFingerprint
    confirmed: bool
    episodes: int


class TitleLibrary:
    def __init__(self, intro_root: Path, media_id: int) -> None:
        self.media_id = int(media_id)
        self.dir = Path(intro_root) / "library" / str(self.media_id)
        self.path = self.dir / "library.json"
        self.changed = False
        self._fps: dict[str, AudioFingerprint | None] = {}
        self.data = self._load()

    # ------------------------------------------------------------ storage ---

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if (
            not isinstance(data, dict)
            or data.get("version") != LIBRARY_VERSION
            or data.get("engine") != ENGINE_VERSION
        ):
            if data is not None:
                shutil.rmtree(self.dir, ignore_errors=True)  # stale engine: codes are not comparable
            data = {}
        data.setdefault("version", LIBRARY_VERSION)
        data.setdefault("engine", ENGINE_VERSION)
        data.setdefault("media_id", self.media_id)
        data.setdefault("migrated", False)
        data.setdefault("next_variant", 1)
        data.setdefault("variants", {})
        data.setdefault("episodes", {})
        return data

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def migrated(self) -> bool:
        return bool(self.data.get("migrated"))

    def mark_migrated(self) -> None:
        if not self.data.get("migrated"):
            self.data["migrated"] = True
            self.changed = True

    def save(self) -> None:
        if not self.changed:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = time.time()
        fd, tmp = tempfile.mkstemp(prefix=".library.", dir=str(self.dir))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.path)
        self.changed = False

    def _raw_path(self, episode: int, region: str) -> Path:
        return self.dir / f"e{int(episode)}-{region}.npz"

    def _variant_path(self, variant_id: str) -> Path:
        return self.dir / f"v-{variant_id}.npz"

    def _load_fp(self, path: Path) -> AudioFingerprint | None:
        key = str(path)
        if key not in self._fps:
            loaded = load_fingerprint(path) if path.is_file() else None
            self._fps[key] = loaded[0] if loaded else None
        return self._fps[key]

    def _save_fp(self, path: Path, fp: AudioFingerprint, meta: dict[str, Any]) -> None:
        save_fingerprint(path, fp, meta={**meta, "media_id": self.media_id})
        self._fps[str(path)] = fp

    def _drop_raw(self, episode: int, region: str) -> None:
        entry = self.data["episodes"].get(str(int(episode)))
        path = self._raw_path(episode, region)
        path.unlink(missing_ok=True)
        self._fps.pop(str(path), None)
        if entry and entry.get(region, {}).get("raw"):
            entry[region]["raw"] = False
            self.changed = True

    def size_bytes(self) -> int:
        return directory_size(self.dir)

    # ----------------------------------------------------------- episodes ---

    def has_episode(self, episode: int, identity: str) -> bool:
        entry = self.data["episodes"].get(str(int(episode)))
        return bool(entry and entry.get("identity") == identity)

    def observed(self, episode: int, identity: str) -> bool:
        entry = self.data["episodes"].get(str(int(episode)))
        return bool(entry and entry.get("identity") == identity and entry.get("observed"))

    def add_episode(self, episode: int, identity: str, fps: FingerprintSet) -> None:
        """Keep the head/tail of one episode unless a confirmed variant already explains it."""
        key = str(int(episode))
        entry = self.data["episodes"].get(key)
        if entry is None or entry.get("identity") != identity:
            if entry is not None:
                for _kind, region, _index in REGIONS:
                    self._drop_raw(episode, region)
            entry = {
                "identity": identity,
                "duration": float(fps[2]),
                "head": {"variant": None, "raw": False},
                "tail": {"variant": None, "raw": False},
            }
            self.data["episodes"][key] = entry
            self.changed = True
        for kind, region, index in REGIONS:
            slot = entry[region]
            if slot.get("raw") or self._is_confirmed(slot.get("variant")):
                continue
            self._save_fp(
                self._raw_path(episode, region), fps[index], {"identity": identity, "region": region}
            )
            slot["raw"] = True
            self.changed = True

    def stored_ids(self, exclude_episodes: set[int]) -> list[tuple[int, str]]:
        """(episode, identity) of episodes kept as raw data, without loading it."""
        return [
            (int(key), str(entry["identity"]))
            for key, entry in sorted(self.data["episodes"].items(), key=lambda pair: int(pair[0]))
            if int(key) not in exclude_episodes and any(entry[r].get("raw") for _k, r, _i in REGIONS)
        ]

    def stored_partners(self, exclude_episodes: set[int]) -> list[tuple[int, str, FingerprintSet]]:
        """(episode, identity, fps) of episodes kept as raw data (missing region = empty)."""
        result = []
        for key, entry in sorted(self.data["episodes"].items(), key=lambda pair: int(pair[0])):
            episode = int(key)
            if episode in exclude_episodes:
                continue
            regions: list[AudioFingerprint] = []
            for _kind, region, _index in REGIONS:
                fp = self._load_fp(self._raw_path(episode, region)) if entry[region].get("raw") else None
                regions.append(fp if fp is not None else empty_fingerprint())
            if all(fp.size == 0 for fp in regions):
                continue
            result.append(
                (episode, str(entry["identity"]), (regions[0], regions[1], float(entry["duration"])))
            )
        return result

    # ----------------------------------------------------------- variants ---

    def _is_confirmed(self, variant_id: Any) -> bool:
        row = self.data["variants"].get(str(variant_id)) if variant_id else None
        return bool(row and row.get("confirmed"))

    def variant_ids(self) -> set[str]:
        return {f"variant:{vid}" for vid in self.data["variants"]}

    def has_reference(self, exclude_episodes: Iterable[int] = ()) -> bool:
        """Anything a lone episode on disk could be compared with."""
        if self.data["variants"]:
            return True
        skip = {str(int(e)) for e in exclude_episodes}
        return any(
            entry[r].get("raw")
            for key, entry in self.data["episodes"].items()
            if key not in skip
            for _k, r, _i in REGIONS
        )

    def variants(self, kind: str) -> list[VariantRef]:
        refs = []
        rows = sorted(
            self.data["variants"].items(),
            key=lambda pair: (not pair[1].get("confirmed"), -max(pair[1].get("episodes") or [0])),
        )
        for vid, row in rows:
            if row.get("kind") != kind:
                continue
            fp = self._load_fp(self._variant_path(vid))
            if fp is None:
                continue
            refs.append(VariantRef(vid, kind, fp, bool(row.get("confirmed")), len(row.get("episodes") or [])))
        return refs

    def _credit(self, variant_id: str, episode: int) -> None:
        row = self.data["variants"][variant_id]
        episodes = set(int(e) for e in row.get("episodes") or [])
        if int(episode) not in episodes:
            episodes.add(int(episode))
            row["episodes"] = sorted(episodes)
            self.changed = True
        if not row.get("confirmed") and _consecutive(episodes, CONFIRM_EPISODES):
            row["confirmed"] = True
            self.changed = True

    def _set_explained(self, episode: int, region: str, variant_id: str) -> None:
        entry = self.data["episodes"].get(str(int(episode)))
        if entry is not None and entry[region].get("variant") != variant_id:
            entry[region]["variant"] = variant_id
            self.changed = True

    def _new_variant(self, kind: str, fp: AudioFingerprint, episode: int) -> str:
        vid = str(int(self.data.get("next_variant") or 1))
        self.data["next_variant"] = int(vid) + 1
        self._save_fp(self._variant_path(vid), fp, {"variant": vid, "kind": kind})
        self.data["variants"][vid] = {
            "kind": kind,
            "episodes": [],
            "confirmed": False,
            "duration": round(fp.duration_seconds, 2),
            "created_at": time.time(),
        }
        self.changed = True
        self._credit(vid, episode)
        return vid

    def observe(
        self,
        episode: int,
        identity: str,
        fps: FingerprintSet,
        segments: Iterable[tuple[str, float, float, str | None]],
        settings: Any,
    ) -> None:
        """Record (kind, start, end, matched variant id or None) found in one episode."""
        for kind, start, end, variant_id in segments:
            region = KIND_REGION.get(kind)
            if region is None:
                continue
            if variant_id and variant_id in self.data["variants"]:
                self._credit(variant_id, episode)
                self._set_explained(episode, region, variant_id)
                continue
            piece = slice_fingerprint(fps[0 if kind == "intro" else 1], start, end)
            if piece.size < 8:
                continue
            matched = None
            for ref in self.variants(kind):
                if match_variant(
                    piece,
                    ref.fingerprint,
                    min_seconds=min(settings.min_seconds, piece.duration_seconds * 0.8),
                    max_seconds=settings.max_seconds * 1.5,
                    max_bit_error=settings.max_bit_error,
                ):
                    matched = ref.id
                    break
            matched = matched or self._new_variant(kind, piece, episode)
            self._credit(matched, episode)
            self._set_explained(episode, region, matched)
        entry = self.data["episodes"].get(str(int(episode)))
        if entry is not None and entry.get("identity") == identity and not entry.get("observed"):
            entry["observed"] = True
            self.changed = True

    def reconcile(self, settings: Any, log: logging.Logger | None = None) -> None:
        """Credit kept raw regions to variants, drop what confirmed variants explain, bound the rest."""
        for key, entry in list(self.data["episodes"].items()):
            episode = int(key)
            for kind, region, _index in REGIONS:
                slot = entry[region]
                if not slot.get("raw") or slot.get("variant"):
                    continue
                fp = self._load_fp(self._raw_path(episode, region))
                if fp is None:
                    slot["raw"] = False
                    self.changed = True
                    continue
                for ref in self.variants(kind):
                    if match_variant(
                        fp,
                        ref.fingerprint,
                        min_seconds=settings.min_seconds,
                        max_seconds=settings.max_seconds,
                        max_bit_error=settings.max_bit_error,
                    ):
                        self._credit(ref.id, episode)
                        self._set_explained(episode, region, ref.id)
                        break
        for key, entry in list(self.data["episodes"].items()):
            for _kind, region, _index in REGIONS:
                if entry[region].get("raw") and self._is_confirmed(entry[region].get("variant")):
                    self._drop_raw(int(key), region)
        for _kind, region, _index in REGIONS:
            kept = sorted(
                (int(key) for key, entry in self.data["episodes"].items() if entry[region].get("raw")),
                reverse=True,
            )
            for episode in kept[MAX_RAW_EPISODES:]:
                self._drop_raw(episode, region)
        for kind, _region, _index in REGIONS:
            rows = [(vid, row) for vid, row in self.data["variants"].items() if row.get("kind") == kind]
            rows.sort(key=lambda pair: (bool(pair[1].get("confirmed")), max(pair[1].get("episodes") or [0])))
            for vid, _row in rows[: max(0, len(rows) - MAX_VARIANTS)]:
                self._remove_variant(vid)
        if log is not None and self.changed:
            log.info(
                "RESULT step=intro_skipper.library media_id=%s variants=%s raw_regions=%s",
                self.media_id,
                ",".join(
                    f"{row['kind']}#{vid}:{'confirmed' if row.get('confirmed') else 'seen'}"
                    f"@{'/'.join(str(e) for e in row.get('episodes') or [])}"
                    for vid, row in sorted(self.data["variants"].items(), key=lambda p: int(p[0]))
                )
                or "-",
                sum(
                    1
                    for entry in self.data["episodes"].values()
                    for _k, r, _i in REGIONS
                    if entry[r].get("raw")
                ),
            )

    def _remove_variant(self, variant_id: str) -> None:
        self.data["variants"].pop(variant_id, None)
        path = self._variant_path(variant_id)
        path.unlink(missing_ok=True)
        self._fps.pop(str(path), None)
        for entry in self.data["episodes"].values():
            for _kind, region, _index in REGIONS:
                if entry[region].get("variant") == variant_id:
                    entry[region]["variant"] = None
        self.changed = True

    def drop_all_raw(self) -> None:
        for key in list(self.data["episodes"]):
            for _kind, region, _index in REGIONS:
                self._drop_raw(int(key), region)


def directory_size(path: Path) -> int:
    total = 0
    try:
        for child in Path(path).iterdir():
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def prune_libraries(
    intro_root: Path,
    animes: Iterable[Any],
    *,
    logger: logging.Logger | None = None,
    max_bytes: int | None = None,
) -> dict[str, int]:
    """Delete libraries of titles without expected episodes; keep the rest within the size cap."""
    log = logger or logging.getLogger(__name__)
    root = Path(intro_root) / "library"
    if not root.is_dir():
        return {"removed": 0, "trimmed": 0}
    keep = {int(anime.media_id) for anime in animes if episodes_expected(anime)}
    removed = trimmed = 0
    alive: list[tuple[float, int, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            media_id = int(child.name)
        except ValueError:
            continue
        if media_id not in keep:
            size = directory_size(child)
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            log.info(
                "CLEANUP step=intro_skipper.library media_id=%s reason=no_episodes_expected bytes=%s",
                media_id,
                size,
            )
            continue
        try:
            stamp = (child / "library.json").stat().st_mtime
        except OSError:
            stamp = 0.0
        alive.append((stamp, media_id, child))
    limit = LIBRARY_MAX_BYTES if max_bytes is None else int(max_bytes)
    total = sum(directory_size(path) for _stamp, _mid, path in alive)
    if total > limit:
        alive.sort()
        # Raw episode data goes first (oldest titles first); variant data only as a last resort.
        for _stamp, media_id, path in alive:
            if total <= limit:
                break
            library = TitleLibrary(path.parent.parent, media_id)
            before = directory_size(path)
            library.drop_all_raw()
            library.save()
            total -= before - directory_size(path)
            trimmed += 1
        for _stamp, media_id, path in alive:
            if total <= limit:
                break
            total -= directory_size(path)
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        log.info(
            "CLEANUP step=intro_skipper.library reason=size_cap bytes=%s limit=%s trimmed=%s",
            total,
            limit,
            trimmed,
        )
    return {"removed": removed, "trimmed": trimmed}
