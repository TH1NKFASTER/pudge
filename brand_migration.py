#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def split_legacy(value: str, current: str) -> list[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip() and item.strip() != current]


def merge_move(old: Path, new: Path) -> None:
    """Safely migrate one branded path without overwriting conflicts."""
    if not old.exists() or old.is_symlink() or old == new:
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    if not new.exists():
        try:
            old.rename(new)
        except OSError:
            shutil.move(str(old), str(new))
        return
    if not old.is_dir() or not new.is_dir():
        return
    for child in list(old.iterdir()):
        target = new / child.name
        if target.exists():
            continue
        try:
            child.rename(target)
        except OSError:
            shutil.move(str(child), str(target))
    try:
        old.rmdir()
    except OSError:
        pass


def migrate_paths(home: Path, *, app_name: str, app_slug: str, legacy_names: list[str], legacy_slugs: list[str]) -> None:
    home = home.expanduser()
    for old_slug in legacy_slugs:
        merge_move(home / ".config" / old_slug, home / ".config" / app_slug)
        merge_move(home / ".local" / "share" / old_slug, home / ".local" / "share" / app_slug)
        merge_move(home / "Library" / "Caches" / old_slug, home / "Library" / "Caches" / app_slug)
        for suffix in ("-energy.jsonl", "-runtime.log", "-agent.log", "-agent-error.log"):
            old_log = home / "Library" / "Logs" / f"{old_slug}{suffix}"
            new_log = home / "Library" / "Logs" / f"{app_slug}{suffix}"
            if old_log.is_file() and not new_log.exists():
                new_log.parent.mkdir(parents=True, exist_ok=True)
                old_log.rename(new_log)
    for old_name in legacy_names:
        merge_move(home / "Movies" / old_name, home / "Movies" / app_name)


def rewrite_config(path: Path, home: Path, *, app_name: str, app_slug: str, legacy_names: list[str], legacy_slugs: list[str]) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for old_name in legacy_names:
        text = text.replace(str(home / "Movies" / old_name), str(home / "Movies" / app_name))
    for old_slug in legacy_slugs:
        text = text.replace(str(home / "Library" / "Caches" / old_slug), str(home / "Library" / "Caches" / app_slug))
        text = text.replace(str(home / ".local" / "share" / old_slug), str(home / ".local" / "share" / app_slug))
        text = text.replace(f'category = "{old_slug}"', f'category = "{app_slug}"')
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate persisted paths after a product branding change.")
    parser.add_argument("mode", choices=("paths", "config"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--app-slug", required=True)
    parser.add_argument("--legacy-names", default="")
    parser.add_argument("--legacy-slugs", default="")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    legacy_names = split_legacy(args.legacy_names, args.app_name)
    legacy_slugs = split_legacy(args.legacy_slugs, args.app_slug)
    if args.mode == "paths":
        migrate_paths(args.home, app_name=args.app_name, app_slug=args.app_slug, legacy_names=legacy_names, legacy_slugs=legacy_slugs)
    else:
        if args.config is None:
            parser.error("--config is required in config mode")
        rewrite_config(args.config, args.home, app_name=args.app_name, app_slug=args.app_slug, legacy_names=legacy_names, legacy_slugs=legacy_slugs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
