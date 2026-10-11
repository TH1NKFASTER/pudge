from __future__ import annotations

import argparse
import sys
import time

from .config import DEFAULT_CONFIG_PATH, load_config
from .app_session import app_session_active, app_session_window_active
from .branding import APP_AGENT_CLI, APP_NAME
from .logging_utils import configure_logging, timed_step

# The manager (and everything it imports) loads only when there is work; tests
# may replace this name.
AnimeManager = None


def _manager_class():
    if AnimeManager is not None:
        return AnimeManager
    from .manager import AnimeManager as manager_class

    return manager_class


def _config_without_secrets(path):
    """The cheap "is anything due?" check does not read the Keychain."""
    try:
        return load_config(path, resolve_secrets=False)
    except TypeError:  # a replacement loader that only takes the path
        return load_config(path)


def _config_unreadable() -> bool:
    """config.toml is not valid TOML: no background downloads on default settings."""
    from .config import CONFIG_LOAD_ERROR

    if not CONFIG_LOAD_ERROR:
        return False
    print(
        f"{APP_NAME} Agent: {CONFIG_LOAD_ERROR.get('path')} line {CONFIG_LOAD_ERROR.get('line')}: "
        f"{CONFIG_LOAD_ERROR.get('message')}; skipped until the file is fixed",
        file=sys.stderr,
    )
    return True


def _stored_anilist_token() -> str:
    from .config import _SECRET_STORE

    try:
        return _SECRET_STORE.get("anilist-access-token")
    except Exception:  # noqa: BLE001 - treat an unreadable Keychain as "maybe"
        return "unknown"


def scheduled_work_due(config, *, now=None, token_lookup=None):
    from .database import Database
    from .planned_release_discovery import discovery_work_due

    current = time.time() if now is None else float(now)
    if not config.library.database_path.is_file():
        return True
    try:
        db = Database(config.library.database_path, initialize=False)
        if discovery_work_due(db, now=current):
            return True
        with db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM subtitle_jobs WHERE state IN ('pending','processing') AND COALESCE(next_check,0)<=? LIMIT 1",
                (current,),
            ).fetchone():
                return True
            last_run = float(db.get_state("agent_last_run", "0") or 0)
            if current - last_run >= max(5, config.agent.poll_minutes) * 60:
                return True
            if config.anilist.enabled:
                last = max(
                    float(db.get_state("anilist_synced_at", "0") or 0),
                    float(db.get_state("anilist_last_attempt_at", "0") or 0),
                )
                if current - last < max(5, config.agent.anilist_refresh_minutes) * 60:
                    return False
                token = config.anilist.access_token.strip()
                if not token and token_lookup is not None:
                    token = str(token_lookup() or "").strip()
                return bool(token)
        return False
    except (ValueError, OSError):
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_AGENT_CLI)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args(argv)
    if args.scheduled:
        # Decide from the database first; secrets, the manager and its imports
        # are only paid for when something is due.
        light = _config_without_secrets(args.config)
        if _config_unreadable():
            return 0
        if not light.agent.enabled:
            print(f"{APP_NAME} Agent is disabled")
            return 0
        # Lightweight discovery is available to installed configurations even when
        # the GUI is closed. Minimal/test configurations retain the older no-op path.
        if not hasattr(light, "paths") and not app_session_active():
            return 0
        if not hasattr(light, "paths") and app_session_window_active():
            return 0
        if hasattr(light, "library") and not scheduled_work_due(light, token_lookup=_stored_anilist_token):
            print(f"{APP_NAME} Agent: no work due")
            return 0
    config = load_config(args.config)
    if _config_unreadable():
        return 0
    if not config.agent.enabled:
        print(f"{APP_NAME} Agent is disabled")
        return 0
    logger = configure_logging()
    if hasattr(config, "paths"):
        from .providers.anilist import configure_rate_limit_dir

        configure_rate_limit_dir(config.paths.cache_dir)
    manager = _manager_class()(config)
    discovery_engine = getattr(manager, "planned_release_discovery", None)
    discovery = (
        discovery_engine().tick(max_searches=2)
        if callable(discovery_engine)
        else {"armed": 0, "searched": 0, "found": 0}
    )
    if args.scheduled and (not app_session_active() or app_session_window_active()):
        print(f"{APP_NAME} Agent: release discovery {discovery}; heavy work deferred")
        return 0
    search_due = True
    anilist_due = False
    subtitle_due = False
    if args.scheduled:
        now = time.time()
        try:
            last_run = float(manager.db.get_state("agent_last_run", "0") or 0)
        except ValueError:
            last_run = 0
        search_due = now - last_run >= max(5, config.agent.poll_minutes) * 60
        anilist_due = manager.anilist_refresh_due(now=now)
        # Subtitle jobs carry their own next_check timestamps.  They must be
        # allowed to wake the launch-agent independently of the general Nyaa
        # poll, otherwise a larger agent.poll_minutes can silently stretch a
        # requested 30-minute Jimaku retry into hours.
        subtitle_due = any(
            str(row["state"] or "") in {"pending", "processing"} and float(row["next_check"] or 0.0) <= now
            for row in manager.db.subtitle_jobs()
        )
        if not search_due and not anilist_due and not subtitle_due:
            print(f"{APP_NAME} Agent: the next check is not due yet")
            return 0
        # The user can bring Pudge to the foreground after this scheduled
        # process passed the cheap pre-manager check. Recheck immediately before
        # heavy maintenance to close that race without changing normal cadence.
        if app_session_window_active():
            print(f"{APP_NAME} Agent: the window became active; heavy background work deferred")
            return 0
    try:
        with timed_step(logger, "agent.run", scheduled=args.scheduled):
            if search_due:
                stats = manager.run_once()
            else:
                stats = {}
                if subtitle_due:
                    stats["subs"] = manager.process_subtitle_jobs(limit=8)
                if anilist_due:
                    stats["anilist"] = manager.refresh_anilist_if_due()
    except Exception as exc:
        print(f"{APP_NAME} Agent: {exc}", file=sys.stderr)
        return 1
    stats["planned_release_discovery"] = discovery
    print(f"{APP_NAME} Agent:", ", ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
