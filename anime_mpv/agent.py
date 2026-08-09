from __future__ import annotations

import argparse
import sys
import time

from .config import DEFAULT_CONFIG_PATH, load_config
from .branding import APP_AGENT_CLI, APP_NAME
from .manager import AnimeManager
from .logging_utils import configure_logging, timed_step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_AGENT_CLI)
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if not config.agent.enabled:
        print(f"{APP_NAME} Agent отключён")
        return 0
    logger = configure_logging()
    manager = AnimeManager(config)
    search_due = True
    anilist_due = False
    if args.scheduled:
        now = time.time()
        try:
            last_run = float(manager.db.get_state("agent_last_run", "0") or 0)
        except ValueError:
            last_run = 0
        search_due = now - last_run >= max(5, config.agent.poll_minutes) * 60
        anilist_due = manager.anilist_refresh_due(now=now)
        if not search_due and not anilist_due:
            print(f"{APP_NAME} Agent: ещё не наступило время следующей проверки")
            return 0
    try:
        with timed_step(logger, "agent.run", scheduled=args.scheduled):
            if search_due:
                stats = manager.run_once()
            else:
                stats = {"anilist": manager.refresh_anilist_if_due()}
    except Exception as exc:
        print(f"{APP_NAME} Agent: {exc}", file=sys.stderr)
        return 1
    print(f"{APP_NAME} Agent:", ", ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
