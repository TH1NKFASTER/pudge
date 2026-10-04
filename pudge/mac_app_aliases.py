"""Retire Pudge compatibility symlinks after updating any matching Dock pins."""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
DOCK_KEYS = ("persistent-apps", "persistent-others")
WEBKIT_BUNDLE_IDS = ("com.pudge.app", "com.anime-mpv.app")


def unregister_webkit_data_folders(home: Path, run=subprocess.run):
    """Retire only app-shaped WebKit data directories from LaunchServices."""
    root = home / "Library/WebKit"
    if root.is_symlink() or root.parent.is_symlink():
        return
    for identifier in WEBKIT_BUNDLE_IDS:
        directory = root / identifier
        if not directory.is_dir() or directory.is_symlink():
            continue
        contents = directory / "Contents"
        if contents.exists() or contents.is_symlink():
            continue
        result = run(
            [LSREGISTER, "-u", str(directory)], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if result.returncode:
            print(f"Could not unregister WebKit data folder: {directory}")
        else:
            print(f"Retired WebKit data registration (files retained): {directory}")


def migrate_dock_tiles(tiles, aliases: set[Path], app: Path, bundle_id: str):
    """Copy only tiles whose file URL names an exact compatibility link."""
    result = list(tiles or [])
    changed = False
    for index, entry in enumerate(result):
        try:
            tile = dict(entry)
            data = dict(tile.get("tile-data") or {})
            file_data = dict(data.get("file-data") or {})
            url = urlsplit(str(file_data.get("_CFURLString") or ""))
            path = Path(os.path.abspath(unquote(url.path)))
        except (TypeError, ValueError):
            continue
        if url.scheme != "file" or url.netloc not in ("", "localhost") or path not in aliases:
            continue
        file_data.update(_CFURLString=app.as_uri() + "/", _CFURLStringType=15)
        data.update({"file-data": file_data, "file-label": app.stem, "bundle-identifier": bundle_id})
        # The old bookmark can override the new URL. Dock recreates it for app.
        data.pop("book", None)
        tile["tile-data"] = data
        result[index] = tile
        changed = True
    return result, changed


class DockPreferences:
    def __init__(self):
        import CoreFoundation
        import Foundation

        self.cf = CoreFoundation
        self.foundation = Foundation

    def read(self, key):
        cf = self.cf
        return cf.CFPreferencesCopyValue(
            key, "com.apple.dock", cf.kCFPreferencesCurrentUser, cf.kCFPreferencesAnyHost
        )

    def write(self, key, value):
        cf = self.cf
        cf.CFPreferencesSetValue(
            key, value, "com.apple.dock", cf.kCFPreferencesCurrentUser, cf.kCFPreferencesAnyHost
        )

    def synchronize(self):
        cf = self.cf
        return cf.CFPreferencesSynchronize(
            "com.apple.dock", cf.kCFPreferencesCurrentUser, cf.kCFPreferencesAnyHost
        )

    def serialize(self, values):
        foundation = self.foundation
        data, error = foundation.NSPropertyListSerialization.dataWithPropertyList_format_options_error_(
            values, foundation.NSPropertyListXMLFormat_v1_0, 0, None
        )
        if data is None:
            raise RuntimeError(f"Could not back up Dock pins: {error}")
        return bytes(data)


def cleanup_aliases(app: Path, bundle_id: str, names, backup_root: Path, preferences,
                    run=subprocess.run, webkit_home: Path | None = None):
    app = Path(os.path.abspath(app.expanduser()))
    if app.is_symlink():
        raise RuntimeError("Canonical application is a symlink; no app flags changed.")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != bundle_id:
        raise RuntimeError("Unexpected app identity; no aliases changed.")
    app_flags = getattr(app.stat(), "st_flags", None)
    hidden = bool(app_flags is not None and app_flags & stat.UF_HIDDEN)

    aliases = {}
    dock_paths = set()
    for name in names:
        if not name or name == app.stem:
            continue
        if Path(name).name != name or name in (".", ".."):
            raise RuntimeError("Invalid compatibility name; no aliases changed.")
        alias = app.parent / (name + ".app")
        if alias.is_symlink() and alias.resolve() == app.resolve():
            aliases[alias] = (alias.lstat(), os.readlink(alias))
            dock_paths.add(alias)
        elif not alias.exists() and not alias.is_symlink():
            # A fresh native rebuild may already have retired the old bundle.
            dock_paths.add(alias)

    def command(arguments):
        return run(arguments, check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)

    originals, replacements = {}, {}
    for key in DOCK_KEYS if dock_paths else ():
        original = preferences.read(key)
        replacement, changed = migrate_dock_tiles(original, dock_paths, app, bundle_id)
        if changed:
            originals[key] = original
            replacements[key] = replacement

    backup = None
    if aliases or replacements or hidden:
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(mode=0o700)
        (backup / "aliases.json").write_text(json.dumps(
            {str(path): target for path, (_, target) in aliases.items()},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if hidden:
            (backup / "app-visibility.json").write_text(json.dumps(
                {"path": str(app), "bsd_flags_before": app_flags}, indent=2
            ) + "\n", encoding="utf-8")
        if replacements:
            (backup / "dock-before.plist").write_bytes(preferences.serialize(originals))
            try:
                for key, value in replacements.items():
                    preferences.write(key, value)
                if not preferences.synchronize():
                    raise RuntimeError("Could not save Dock pins; aliases retained.")
            except Exception:
                for key, value in originals.items():
                    preferences.write(key, value)
                preferences.synchronize()
                raise

    try:
        # Old installers hid a compatibility symlink without chflags -h.
        # That hid the real bundle. Removing the link did not undo its flag.
        result = command(["/usr/bin/chflags", "nohidden", str(app)])
        if result.returncode:
            raise RuntimeError("Could not restore canonical app visibility; aliases retained.")
        after_flags = getattr(app.stat(), "st_flags", None)
        if after_flags is not None and after_flags & stat.UF_HIDDEN:
            raise RuntimeError("Canonical app remains hidden; aliases retained.")
        print(f"Canonical application visibility restored: {app}")
        if hidden:
            command([LSREGISTER, "-u", str(app)])
        if webkit_home is not None:
            unregister_webkit_data_folders(webkit_home, run)
        for alias, (before, _) in aliases.items():
            current = alias.lstat()
            if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    or not alias.is_symlink() or alias.resolve() != app.resolve()):
                raise RuntimeError(f"Compatibility path changed; retained: {alias}")
            command([LSREGISTER, "-u", str(alias)])
            # Unlink only this verified symlink; never recursively remove an app.
            alias.unlink()
            print(f"Removed compatibility link: {alias}")
    finally:
        command([LSREGISTER, "-f", str(app)])
    command(["/usr/bin/mdimport", str(app)])
    if replacements:
        command(["/usr/bin/killall", "Dock"])
    print(f"Canonical application: {app}")
    if backup:
        print(f"Alias/Dock backup: {backup}")
    return backup


def main():
    if sys.platform != "darwin":
        raise RuntimeError("This cleanup is for macOS.")
    if len(sys.argv) < 3:
        raise RuntimeError("Usage: cleanup_macos_app_aliases.py APP_PATH BUNDLE_ID [LEGACY_NAME ...]")
    cleanup_aliases(
        Path(sys.argv[1]), sys.argv[2], sys.argv[3:],
        Path.home() / ".local/share/pudge/app-alias-backups.noindex", DockPreferences(),
        webkit_home=Path.home(),
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f"App alias cleanup stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
