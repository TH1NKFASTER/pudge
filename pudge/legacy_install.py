#!/usr/bin/env python3
"""One-time migration from legacy Anime MPV defaults to Pudge.

This intentionally is not a generic product renamer.  It only migrates known
legacy default locations and the matching config values.  Conflicting source
files are left in place; config paths are rewritten only when the referenced
legacy path no longer exists and the corresponding Pudge path does exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class MigrationReport:
    moved: list[str] = field(default_factory=list)
    deduplicated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped_symlinks: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "moved": self.moved,
            "deduplicated": self.deduplicated,
            "conflicts": self.conflicts,
            "skipped_symlinks": self.skipped_symlinks,
            "rewritten": self.rewritten,
        }


def split_legacy(value: str, current: str) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").split("|")
        if item.strip() and item.strip() != current
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _sha256(left) == _sha256(right)
    except OSError:
        return False


def _move_file_verified(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
        return
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".migration", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp, follow_symlinks=False)
        if not _same_file(source, tmp):
            raise OSError(f"verification failed while copying {source}")
        os.replace(tmp, target)
        source.unlink()
    finally:
        tmp.unlink(missing_ok=True)


def merge_move(old: Path, new: Path, report: MigrationReport | None = None) -> MigrationReport:
    """Merge *old* into *new* without overwriting or following symlinks."""
    report = report or MigrationReport()
    if not old.exists() and not old.is_symlink():
        return report
    if old == new:
        return report
    if old.is_symlink():
        report.skipped_symlinks.append(str(old))
        return report

    if old.is_file():
        if new.exists() or new.is_symlink():
            if new.is_file() and not new.is_symlink() and _same_file(old, new):
                old.unlink()
                report.deduplicated.append(str(old))
            else:
                report.conflicts.append(str(old))
            return report
        _move_file_verified(old, new)
        report.moved.append(str(old))
        return report

    if not old.is_dir():
        report.conflicts.append(str(old))
        return report
    if new.exists() and (new.is_symlink() or not new.is_dir()):
        report.conflicts.append(str(old))
        return report
    new.mkdir(parents=True, exist_ok=True)
    for child in list(old.iterdir()):
        merge_move(child, new / child.name, report)
    try:
        old.rmdir()
    except OSError:
        pass
    return report


def migrate_paths(
    home: Path,
    *,
    app_name: str,
    app_slug: str,
    legacy_names: list[str],
    legacy_slugs: list[str],
) -> MigrationReport:
    home = home.expanduser()
    report = MigrationReport()
    for old_slug in legacy_slugs:
        merge_move(home / ".config" / old_slug, home / ".config" / app_slug, report)
        merge_move(home / ".local" / "share" / old_slug, home / ".local" / "share" / app_slug, report)
        merge_move(home / "Library" / "Caches" / old_slug, home / "Library" / "Caches" / app_slug, report)
        for suffix in ("-energy.jsonl", "-runtime.log", "-agent.log", "-agent-error.log"):
            merge_move(
                home / "Library" / "Logs" / f"{old_slug}{suffix}",
                home / "Library" / "Logs" / f"{app_slug}{suffix}",
                report,
            )
    for old_name in legacy_names:
        merge_move(home / "Movies" / old_name, home / "Movies" / app_name, report)
    return report


def _expand_home(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _display_path(path: Path, original: str, home: Path) -> str:
    if original == "~" or original.startswith("~/"):
        try:
            rel = path.relative_to(home)
        except ValueError:
            return str(path)
        return "~" if str(rel) == "." else f"~/{rel}"
    return str(path)


def _mapped_path(
    value: str,
    home: Path,
    *,
    app_name: str,
    app_slug: str,
    legacy_names: Iterable[str],
    legacy_slugs: Iterable[str],
) -> str | None:
    current = _expand_home(value, home)
    mappings: list[tuple[Path, Path]] = []
    for old_name in legacy_names:
        mappings.append((home / "Movies" / old_name, home / "Movies" / app_name))
    for old_slug in legacy_slugs:
        mappings.extend(
            [
                (home / "Library" / "Caches" / old_slug, home / "Library" / "Caches" / app_slug),
                (home / ".local" / "share" / old_slug, home / ".local" / "share" / app_slug),
            ]
        )
    for old_base, new_base in mappings:
        try:
            rel = current.relative_to(old_base)
        except ValueError:
            continue
        replacement = new_base / rel
        # Conservative publication rule: only point config at the new location
        # after this exact old path has disappeared and its replacement exists.
        if current.exists() or current.is_symlink() or not replacement.exists():
            return None
        return _display_path(replacement, value, home)
    return None


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_STRING_ASSIGN_RE = re.compile(
    r"^(?P<prefix>\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)(?P<suffix>\s*(?:#.*)?)$"
)
_PATH_KEYS = {
    ("paths", "cache_dir"),
    ("library", "root_dir"),
    ("library", "database_path"),
    ("library", "cover_cache_dir"),
}


def _atomic_write_with_backup(path: Path, text: str) -> None:
    backup = path.with_name(path.name + ".pre-pudge-brand-migration.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, path.stat().st_mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def rewrite_config(
    path: Path,
    home: Path,
    *,
    app_name: str,
    app_slug: str,
    legacy_names: list[str],
    legacy_slugs: list[str],
) -> MigrationReport:
    report = MigrationReport()
    if not path.is_file():
        return report
    text = path.read_text(encoding="utf-8")
    # Never mutate malformed TOML.  The original file is the rollback source.
    tomllib.loads(text)

    section = ""
    changed: list[str] = []
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        section_match = _SECTION_RE.match(body)
        if section_match:
            section = section_match.group(1).strip()
            output.append(line)
            continue
        match = _STRING_ASSIGN_RE.match(body)
        if not match:
            output.append(line)
            continue
        key = match.group("key")
        value = match.group("value")
        replacement: str | None = None
        if (section, key) in _PATH_KEYS:
            replacement = _mapped_path(
                value,
                home,
                app_name=app_name,
                app_slug=app_slug,
                legacy_names=legacy_names,
                legacy_slugs=legacy_slugs,
            )
        elif section == "qbittorrent" and key == "category" and value in legacy_slugs:
            replacement = app_slug
        if replacement is None or replacement == value:
            output.append(line)
            continue
        changed.append(f"{section}.{key}")
        output.append(
            f"{match.group('prefix')}{match.group('quote')}{replacement}{match.group('quote')}{match.group('suffix')}{newline}"
        )

    if not changed:
        return report
    updated = "".join(output)
    tomllib.loads(updated)
    _atomic_write_with_backup(path, updated)
    report.rewritten.extend(changed)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Anime MPV defaults to Pudge.")
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
        report = migrate_paths(
            args.home,
            app_name=args.app_name,
            app_slug=args.app_slug,
            legacy_names=legacy_names,
            legacy_slugs=legacy_slugs,
        )
    else:
        if args.config is None:
            parser.error("--config is required in config mode")
        report = rewrite_config(
            args.config,
            args.home,
            app_name=args.app_name,
            app_slug=args.app_slug,
            legacy_names=legacy_names,
            legacy_slugs=legacy_slugs,
        )
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
