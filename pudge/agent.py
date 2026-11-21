from __future__ import annotations

import argparse
import signal
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
        if not app_session_active() or app_session_window_active():
            # Heavy work waits anyway (the window is in use, or Pudge is not open):
            # do not pay for the Keychain and the manager only to defer it. Only
            # agent_last_run used to answer here, and only a heavy run advances it.
            return False
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


def _exit_on_sigterm() -> object | None:
    """launchd stops the agent at quit with SIGTERM: leave through ``finally`` blocks.

    A subtitle prepare worker runs in its own process group, so it does not get the
    agent's signal; the poll loop's ``finally`` stops that group on the way out.
    Without a handler the agent died at once and left the worker (ffmpeg, alass,
    speech recognition) running. Returns the previous handler, None if not installed.
    """

    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + int(signum))

    try:
        return signal.signal(signal.SIGTERM, stop)
    except ValueError:  # not the main thread
        return None


# A pause the agent sees first counts from then (foreground.PAUSE_SETTLE_SECONDS): one more
# look a little later, so a pause shorter than the 5-minute launchd interval is used too.
PAUSED_FOLLOW_UP_SECONDS = 35.0


_paused_schedulers: dict[str, object] = {}


def _paused_scheduler(config):
    """The admission rules of the manager's scheduler (power settings included), without it."""
    from pathlib import Path

    from .work_scheduler import WorkScheduler

    cache_dir = Path(config.paths.cache_dir)
    key = str(cache_dir)
    if key not in _paused_schedulers:
        power = getattr(config, "power", None)
        _paused_schedulers[key] = WorkScheduler(
            cache_dir,
            power_manual_enabled=bool(getattr(power, "manual_energy_saving", False)),
            power_auto_enabled=bool(getattr(power, "auto_battery_energy_saving", True)),
        )
    return _paused_schedulers[key]


def _player_paused(config) -> bool:
    """The video mpv is paused and the OP/ED detection may run beside it now: AC power, no
    energy saving, no thermal limit (a pause too young to count still gets its follow-up).
    Cheap: the marker, one ~200 ms IPC question and the cached power state; on battery the
    agent exits early as before."""
    paths = getattr(config, "paths", None)
    if paths is None:
        return False
    try:
        from pathlib import Path

        from .foreground import foreground_active

        if not foreground_active(Path(paths.cache_dir)):
            return False
        reason = _paused_scheduler(config).background_wait_reason(foreground_paused_ok=True)
        return reason in {None, "paused_recently"}
    except Exception:  # noqa: BLE001 - an optional step
        return False


def _paused_intro_step(manager) -> int:
    """OP/ED detection beside a paused player, after the maintenance lock is released.

    Holds only the heavy-work lease (Refresh, Repair, Backup stay available). The agent
    lowers itself first: it exits after this step, and the player keeps the CPU it wants.
    """
    runner = getattr(manager, "run_paused_intro_detection", None)
    if not callable(runner):
        return 0
    from .process_priority import lower_current_process

    lower_current_process()
    result = runner() or {}
    if result.get("reason") == "paused_recently":
        time.sleep(PAUSED_FOLLOW_UP_SECONDS)
        result = runner() or {}
    return int(result.get("analysed") or 0)


def main(argv: list[str] | None = None) -> int:
    from .private_files import harden_private_storage

    harden_private_storage()
    previous = _exit_on_sigterm()
    try:
        return _main(argv)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _main(argv: list[str] | None = None) -> int:
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
        # The agent works only while Pudge is open. The window bootstraps it and
        # boots it out at quit; after a crash launchd keeps running it until
        # logout, and then it does nothing.
        if not app_session_active():
            print(f"{APP_NAME} Agent: {APP_NAME} is not open; nothing to do")
            return 0
        # Minimal/test configurations retain the older no-op path.
        if not hasattr(light, "paths") and app_session_window_active():
            return 0
        if (
            hasattr(light, "library")
            and not scheduled_work_due(light, token_lookup=_stored_anilist_token)
            and not _player_paused(light)
        ):
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
        from . import jiten_client

        jiten_client.configure(config.paths.cache_dir)
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
        if not search_due and not anilist_due and not subtitle_due and not _player_paused(config):
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
            # Outside run_once's maintenance lock: the only work beside a paused player.
            if _player_paused(config):
                stats["intro_paused"] = _paused_intro_step(manager)
    except Exception as exc:
        print(f"{APP_NAME} Agent: {exc}", file=sys.stderr)
        return 1
    stats["planned_release_discovery"] = discovery
    print(f"{APP_NAME} Agent:", ", ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
