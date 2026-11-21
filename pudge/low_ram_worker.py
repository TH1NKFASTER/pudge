"""Short-lived worker (normal and Low RAM mode): ``python -m pudge.low_ram_worker <task>``.

Reads one JSON request on stdin, answers one JSON line on stdout
(``{"ok": true, "result": …}`` or ``{"ok": false, "error": …}``) and exits, so the memory
the work needed goes with the process instead of staying in the window process.

Tasks:
  statistics_query   {"config_path", "scope", "limit", "offset"}  → ConsumptionStatistics.query
  statistics_export  {"config_path", "scope"}                     → ConsumptionStatistics.export_csv
  anilist_library    {"config_path", "endpoint", "access_token"}  → the list, compact rows only
  anilist_refresh    {"config_path", request modes}               → AnimeManager.refresh_anilist_cache
  anilist_periodic   {"config_path", "now", request modes}        → the periodic list refresh
  intro_pass         {"config_path", "incomplete_paths",          → intro_skipper.run_background_pass
                      "prefer_paths"?, "playback_video"?}
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


def _statistics(request: dict[str, Any]) -> Any:
    from .config import load_config
    from .consumption_statistics import ConsumptionStatistics
    from .database import Database
    from .database_storage import unpack_json
    from .light_novels import chapter_word_positions_from

    config = load_config(Path(request["config_path"]), resolve_secrets=False, apply_language=True)
    database = Database(config.library.database_path, initialize=False)

    def ln_words(text_hash: str) -> list[tuple[int, int, int]] | None:
        # LightNovelService.chapter_word_positions without the service: only the cached
        # Jiten parse (its cache key is the text hash), never a network call.
        key = str(text_hash or "").strip()
        if not key:
            return None
        with database.connect() as conn:
            row = conn.execute("SELECT text FROM ln_chapters WHERE text_hash=? LIMIT 1", (key,)).fetchone()
            if row is None:
                return None
            parsed_row = conn.execute(
                "SELECT parsed_json,used_at FROM ln_parse_cache WHERE text_hash=?", (key,)
            ).fetchone()
            if parsed_row is not None and float(parsed_row["used_at"] or 0.0) < time.time() - 86400:
                conn.execute("UPDATE ln_parse_cache SET used_at=? WHERE text_hash=?", (time.time(), key))
        if parsed_row is None:
            return None
        try:
            parsed = unpack_json(parsed_row["parsed_json"])
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        return chapter_word_positions_from(str(row["text"] or ""), parsed)

    # Pending word units are queued by the query; the window wakes its word job itself.
    return ConsumptionStatistics(database, ln_words=ln_words, count_words=True)


def statistics_query(request: dict[str, Any]) -> Any:
    limit = request.get("limit")
    return _statistics(request).query(
        request.get("scope") or {},
        journal_limit=None if limit is None else int(limit),
        journal_offset=int(request.get("offset") or 0),
    )


def statistics_export(request: dict[str, Any]) -> Any:
    output_dir = request.get("output_dir")
    return _statistics(request).export_csv(
        request.get("scope") or {}, output_dir=Path(output_dir) if output_dir else None
    )


def anilist_library(request: dict[str, Any]) -> Any:
    """AniListClient.library() here: the GraphQL answer (tens of MB parsed for a long list)
    stays in this process; the window gets the rows it keeps. An AniList refusal comes back
    as ``{"anilist_error": …}`` so the window handles it like its own (saved list), without
    asking AniList a second time."""
    from dataclasses import asdict

    from .config import load_config
    from .providers.anilist import AniListClient, AniListError, configure_rate_limit_dir

    config = load_config(Path(request["config_path"]), resolve_secrets=False, apply_language=True)
    configure_rate_limit_dir(config.paths.cache_dir)  # the window's cool-down applies here too
    client = AniListClient(
        str(request.get("endpoint") or ""), access_token=str(request.get("access_token") or "")
    )
    try:
        items = client.library()
    except AniListError as exc:
        return {"anilist_error": _anilist_error(exc)}
    finally:
        client.close()
    entries = getattr(client, "last_list_entries", None)
    media = getattr(client, "last_list_media", None)
    wanted = request.get("media_for")
    if isinstance(media, dict) and isinstance(wanted, list):
        # The window uses the media of placeholder rows only (their metadata).
        keep = {int(value) for value in wanted}
        media = {key: value for key, value in media.items() if int(key) in keep}
    return {
        "items": [asdict(item) for item in items],
        "entries": None if not isinstance(entries, dict) else {str(k): v for k, v in entries.items()},
        "media": None if not isinstance(media, dict) else {str(k): asdict(v) for k, v in media.items()},
        "incomplete": bool(getattr(client, "last_list_incomplete", False)),
    }


def _anilist_error(exc: Exception) -> dict[str, Any]:
    from .providers.anilist import AniListHTTPError

    return {
        "message": str(exc),
        "status_code": int(getattr(exc, "status_code", 0) or 0) if isinstance(exc, AniListHTTPError) else 0,
        "detail": str(getattr(exc, "detail", "") or ""),
        "refusal": bool(getattr(exc, "refusal", False)),
    }


def _anilist_manager(request: dict[str, Any]) -> Any:
    """The window's AnimeManager, here (same config and database; not the schema owner)."""
    from .config import load_config
    from .manager import AnimeManager
    from .providers.anilist import configure_rate_limit_dir

    config = load_config(Path(request["config_path"]), resolve_secrets=False, apply_language=True)
    # The Keychain is not read here: the window sends its AniList connection (stdin only).
    window = request.get("anilist") if isinstance(request.get("anilist"), dict) else {}
    if "enabled" in window:
        config.anilist.enabled = bool(window["enabled"])
    if window.get("endpoint"):
        config.anilist.endpoint = str(window["endpoint"])
    if "access_token" in window:
        config.anilist.access_token = str(window["access_token"] or "")
    configure_rate_limit_dir(config.paths.cache_dir)
    return AnimeManager(config)


def anilist_refresh(request: dict[str, Any]) -> Any:
    """refresh_anilist_cache here: the list, the covers, one relation graph and the off-list
    metadata are written to the database by this process; the window gets the small stats.
    An AniList refusal without a saved list comes back as ``{"anilist_error": …}``."""
    from . import low_ram
    from .providers.anilist import AniListError, requests_in_modes

    manager = _anilist_manager(request)
    with low_ram.inline_only(), requests_in_modes(request):
        try:
            stats = manager.refresh_anilist_cache_inline()
        except AniListError as exc:
            return {"anilist_error": _anilist_error(exc)}
    return {"stats": json.loads(json.dumps(stats, default=str))}


def anilist_periodic(request: dict[str, Any]) -> Any:
    """The periodic list refresh (and its relation graph) here; failures are logged here."""
    from . import low_ram
    from .providers.anilist import requests_in_modes

    manager = _anilist_manager(request)
    now = request.get("now")
    with low_ram.inline_only(), requests_in_modes(request):
        count = manager.refresh_anilist_if_due_inline(now=None if now is None else float(now))
    return {"count": int(count or 0)}


def intro_pass(request: dict[str, Any]) -> Any:
    """One OP/ED detection step (numpy, audio fingerprints) outside the window process."""
    from .config import load_config
    from .database import Database
    from .intro_skipper import run_background_pass
    from .logging_utils import configure_logging
    from .work_scheduler import WorkScheduler

    config = load_config(Path(request["config_path"]), resolve_secrets=False, apply_language=True)
    logger = configure_logging()
    database = Database(config.library.database_path, initialize=False)
    scheduler = WorkScheduler(
        config.paths.cache_dir,
        logger=logger,
        power_manual_enabled=config.power.manual_energy_saving,
        power_auto_enabled=config.power.auto_battery_energy_saving,
    )
    result = run_background_pass(
        db=database,
        cache_dir=config.paths.cache_dir,
        ffmpeg=config.tools.ffmpeg,
        ffprobe=config.tools.ffprobe,
        scheduler=scheduler,
        logger=logger,
        incomplete_paths=[Path(value) for value in request.get("incomplete_paths") or []],
        enabled=bool(getattr(config.playback, "skip_segments_enabled", True)),
        paused_ok=False,
        prefer_paths=[Path(value) for value in request.get("prefer_paths") or []],
        playback_video=Path(request["playback_video"]) if request.get("playback_video") else None,
    )
    return json.loads(json.dumps(result, default=str))


TASKS = {
    "statistics_query": statistics_query,
    "statistics_export": statistics_export,
    "anilist_library": anilist_library,
    "anilist_refresh": anilist_refresh,
    "anilist_periodic": anilist_periodic,
    "intro_pass": intro_pass,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    task = TASKS.get(args[0] if args else "")
    if task is None:
        print(json.dumps({"ok": False, "error": f"unknown task {args[:1]}"}))
        return 2
    try:
        request = json.loads(sys.stdin.read() or "{}")
        result = task(request if isinstance(request, dict) else {})
        line = json.dumps({"ok": True, "result": result}, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:  # noqa: BLE001 - the window falls back to in-process work
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}))
        return 1
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
