"""What each paired iPhone/iPad has downloaded, for the icons on the Mac's library cards.

The phone reports its whole inventory (``POST /api/v2/inventory``) after connecting or
refreshing and after every finished download or deletion: the ids of the phone API
(``/api/v2/media/episodes/<id>``, ``ln/<id>``, ``manga/<id>``, audiobook ``assetId``) and its
idiom (``phone`` or ``pad``). The Mac keeps the last report of each paired device and replaces
it with the next one; revoking the device drops it, and only devices still paired are shown.
A report older than a week is still shown, marked "as of <date>" by the window.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .iphone_service import identifier

# A report carries every downloaded item: thousands of ids at most (a 256 GB iPad holds a
# few hundred episodes). The request body is limited before it is parsed.
BODY_LIMIT = 512 * 1024
ITEM_LIMIT = 5000
# After this the window says "as of <date>" instead of presenting the report as current.
STALE_MS = 7 * 86_400_000
IDIOMS = ("phone", "pad")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS iphone_inventory(device TEXT PRIMARY KEY,idiom TEXT NOT NULL,"
    "body TEXT NOT NULL,reported_at INTEGER NOT NULL)"
)

_NUMBERED = ("episodes", "lightNovels", "manga")


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_report(payload: Any) -> dict[str, Any]:
    """The checked report: sorted unique ids per kind. Ids below 1 are the phone's own numbers
    for downloads of a previous Mac (never this Mac's items) and are left out."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid inventory")
    idiom = payload.get("idiom")
    if idiom not in IDIOMS:
        raise ValueError("Invalid device idiom")
    report: dict[str, Any] = {"idiom": idiom}
    for key in _NUMBERED:
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > ITEM_LIMIT:
            raise ValueError(f"Invalid {key}")
        ids = set()
        for value in values:
            if type(value) is not int or abs(value) > 2**53 - 1:
                raise ValueError(f"Invalid {key}")
            if value > 0:
                ids.add(value)
        report[key] = sorted(ids)
    assets = payload.get("audiobooks", [])
    if not isinstance(assets, list) or len(assets) > ITEM_LIMIT:
        raise ValueError("Invalid audiobooks")
    report["audiobooks"] = sorted({identifier(value) for value in assets})
    return report


def ensure_table(conn: Any) -> None:
    conn.execute(SCHEMA)


def record(db: Any, device: str, payload: Any, *, now: int | None = None) -> dict[str, Any]:
    """Store ``device``'s report in place of its previous one.

    Returns the receipt for the phone plus ``changed`` (the window should redraw: other items,
    another idiom, or the previous report was old enough to be shown "as of")."""
    report = parse_report(payload)
    moment = _now_ms() if now is None else int(now)
    body = json.dumps({key: report[key] for key in (*_NUMBERED, "audiobooks")}, separators=(",", ":"))
    with db.connect() as conn:
        ensure_table(conn)
        previous = conn.execute(
            "SELECT idiom,body,reported_at FROM iphone_inventory WHERE device=?", (device,)
        ).fetchone()
        # Only for a device still paired: a request authenticated just before revoke() must not
        # write its report after the revocation dropped it (the id comes back on re-pairing).
        written = conn.execute(
            "INSERT INTO iphone_inventory(device,idiom,body,reported_at) SELECT ?,?,?,? "
            "WHERE EXISTS (SELECT 1 FROM iphone_devices WHERE id=? AND revoked=0) "
            "ON CONFLICT(device) DO UPDATE SET idiom=excluded.idiom,body=excluded.body,"
            "reported_at=excluded.reported_at",
            (device, report["idiom"], body, moment, device),
        ).rowcount
        if not written:
            raise PermissionError("Device access revoked or invalid")
    changed = (
        previous is None
        or previous["idiom"] != report["idiom"]
        or previous["body"] != body
        or moment - int(previous["reported_at"]) >= STALE_MS
    )
    counts = {key: len(report[key]) for key in (*_NUMBERED, "audiobooks")}
    return {"ok": True, "receivedAt": moment, "counts": counts, "changed": changed}


def forget(conn: Any, device: str) -> None:
    """A revoked device's report goes with its access (and a re-paired one starts empty)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='iphone_inventory'").fetchone():
        conn.execute("DELETE FROM iphone_inventory WHERE device=?", (device,))


def _table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _unique_names(rows: list[Any]) -> dict[str, str]:
    """Device names as paired; two devices of the same name become "iPhone" and "iPhone 2"."""
    seen: dict[str, int] = {}
    names: dict[str, str] = {}
    for row in rows:
        base = str(row["name"] or "").strip() or ("iPad" if row["idiom"] == "pad" else "iPhone")
        count = seen.get(base.casefold(), 0) + 1
        seen[base.casefold()] = count
        names[str(row["device"])] = base if count == 1 else f"{base} {count}"
    return names


def summary(db: Any, *, now: int | None = None) -> dict[str, Any]:
    """What the window shows: the paired devices with a report, and per library item the
    devices that have it.

    ``anime``: media id -> device -> episode numbers (one entry per downloaded episode; 0 when
    the episode has no number), ``ln`` / ``manga`` / ``audiobooks``: the window's item id
    (light-novel volume, manga volume, audiobook) -> devices."""
    moment = _now_ms() if now is None else int(now)
    empty: dict[str, Any] = {"devices": [], "anime": {}, "ln": {}, "manga": {}, "audiobooks": {}}
    with db.connect() as conn:
        if not _table_exists(conn, "iphone_inventory") or not _table_exists(conn, "iphone_devices"):
            return empty
        rows = conn.execute(
            "SELECT i.device,i.idiom,i.body,i.reported_at,d.name FROM iphone_inventory i "
            "JOIN iphone_devices d ON d.id=i.device WHERE d.revoked=0 "
            "ORDER BY COALESCE(d.paired_at,0),i.device"
        ).fetchall()
        if not rows:
            return empty
        reports: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                reports[str(row["device"])] = json.loads(row["body"])
            except (TypeError, ValueError):
                continue
        episode_ids = sorted({int(x) for body in reports.values() for x in body.get("episodes") or []})
        episodes: dict[int, tuple[int, int]] = {}
        if episode_ids and _table_exists(conn, "episodes"):
            for start in range(0, len(episode_ids), 500):
                chunk = episode_ids[start : start + 500]
                for found in conn.execute(
                    "SELECT id,media_id,COALESCE(media_episode,episode) AS number FROM episodes "
                    f"WHERE id IN ({','.join('?' * len(chunk))}) AND media_id IS NOT NULL",
                    chunk,
                ):
                    episodes[int(found["id"])] = (int(found["media_id"]), int(found["number"] or 0))
        asset_ids = sorted({str(x) for body in reports.values() for x in body.get("audiobooks") or []})
        books: dict[str, int] = {}
        if asset_ids and _table_exists(conn, "iphone_assets"):
            for start in range(0, len(asset_ids), 500):
                chunk = asset_ids[start : start + 500]
                for found in conn.execute(
                    f"SELECT id,book_id FROM iphone_assets WHERE id IN ({','.join('?' * len(chunk))}) "
                    "AND book_id IS NOT NULL",
                    chunk,
                ):
                    books[str(found["id"])] = int(found["book_id"])
    names = _unique_names(rows)
    result = empty
    for row in rows:
        device = str(row["device"])
        if device not in reports:
            continue
        reported = int(row["reported_at"])
        result["devices"].append(
            {
                "id": device,
                "name": names[device],
                "idiom": row["idiom"] if row["idiom"] in IDIOMS else "phone",
                "reportedAt": reported,
                "stale": moment - reported >= STALE_MS,
            }
        )
        body = reports[device]
        for episode in body.get("episodes") or []:
            found = episodes.get(int(episode))
            if found is not None:
                media, number = found
                result["anime"].setdefault(str(media), {}).setdefault(device, []).append(number)
        for key, kind in (("lightNovels", "ln"), ("manga", "manga")):
            for item in body.get(key) or []:
                result[kind].setdefault(str(int(item)), []).append(device)
        for asset in body.get("audiobooks") or []:
            book = books.get(str(asset))
            if book is not None and device not in result["audiobooks"].setdefault(str(book), []):
                result["audiobooks"][str(book)].append(device)
    for per_device in result["anime"].values():
        for numbers in per_device.values():
            numbers.sort()
    return result
