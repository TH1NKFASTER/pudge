"""Exact-path app alias cleanup, Dock pin preservation, and failure safety."""
import copy
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import tempfile
import unittest

from pudge.mac_app_aliases import LSREGISTER, cleanup_aliases, migrate_dock_tiles


class Preferences:
    def __init__(self, tiles=()):
        self.values = {"persistent-apps": list(tiles), "persistent-others": []}
        self.sync_result = True

    def read(self, key):
        return self.values.get(key)

    def write(self, key, value):
        self.values[key] = value

    def synchronize(self):
        return self.sync_result

    def serialize(self, values):
        return plistlib.dumps(values)


def tile(path):
    return {"GUID": 123, "tile-type": "file-tile", "tile-data": {
        "file-data": {"_CFURLString": path.as_uri() + "/", "_CFURLStringType": 15},
        "file-label": "Anime MPV", "book": b"old bookmark", "extra": "keep"}}


def execute(app, backup, prefs, names=("Anime MPV",), callback=None):
    calls = []

    def run(arguments, **kwargs):
        assert kwargs["check"] is False and kwargs["timeout"] == 15
        calls.append(arguments)
        if callback:
            callback(arguments)
        return SimpleNamespace(returncode=0)

    result = cleanup_aliases(app, "com.pudge.app", names, backup, prefs, run=run)
    return result, calls


class AppAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pudge-alias-test-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)
        self.app = self.tmp_path / "Applications" / "pudge.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleIdentifier": "com.pudge.app"}))

    def test_removes_only_current_alias_and_preserves_dock_tiles_absolute(self):
        app, tmp_path = self.app, self.tmp_path
        relative = False
        alias = app.parent / "Anime MPV.app"
        alias.symlink_to(app.name if relative else app)
        other = tmp_path / "Other.app"
        original = [tile(other), tile(alias)]
        prefs = Preferences(copy.deepcopy(original))
        backup, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert not alias.exists() and not alias.is_symlink()
        assert app.is_dir()
        assert prefs.values["persistent-apps"][0] == original[0]
        changed = prefs.values["persistent-apps"][1]
        assert changed["GUID"] == 123
        assert changed["tile-data"]["extra"] == "keep"
        assert changed["tile-data"]["file-data"]["_CFURLString"] == app.as_uri() + "/"
        assert changed["tile-data"]["bundle-identifier"] == "com.pudge.app"
        assert "book" not in changed["tile-data"]
        assert plistlib.loads((backup / "dock-before.plist").read_bytes())["persistent-apps"] == original
        assert json.loads((backup / "aliases.json").read_text()) == {str(alias): app.name if relative else str(app)}
        assert [LSREGISTER, "-u", str(alias)] in calls
        assert [LSREGISTER, "-f", str(app)] in calls
        assert ["/usr/bin/mdimport", str(app)] in calls
        assert ["/usr/bin/killall", "Dock"] in calls

    def test_removes_only_current_alias_and_preserves_dock_tiles_relative(self):
        app, tmp_path = self.app, self.tmp_path
        relative = True
        alias = app.parent / "Anime MPV.app"
        alias.symlink_to(app.name if relative else app)
        other = tmp_path / "Other.app"
        original = [tile(other), tile(alias)]
        prefs = Preferences(copy.deepcopy(original))
        backup, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert not alias.exists() and not alias.is_symlink()
        assert app.is_dir()
        assert prefs.values["persistent-apps"][0] == original[0]
        changed = prefs.values["persistent-apps"][1]
        assert changed["GUID"] == 123
        assert changed["tile-data"]["extra"] == "keep"
        assert changed["tile-data"]["file-data"]["_CFURLString"] == app.as_uri() + "/"
        assert changed["tile-data"]["bundle-identifier"] == "com.pudge.app"
        assert "book" not in changed["tile-data"]
        assert plistlib.loads((backup / "dock-before.plist").read_bytes())["persistent-apps"] == original
        assert json.loads((backup / "aliases.json").read_text()) == {str(alias): app.name if relative else str(app)}
        assert [LSREGISTER, "-u", str(alias)] in calls
        assert [LSREGISTER, "-f", str(app)] in calls
        assert ["/usr/bin/mdimport", str(app)] in calls
        assert ["/usr/bin/killall", "Dock"] in calls

    def test_preserves_real_or_unrelated_legacy_path_directory(self):
        app, tmp_path = self.app, self.tmp_path
        kind = 'directory'
        alias = app.parent / "Anime MPV.app"
        if kind == "directory":
            alias.mkdir()
            (alias / "keep.txt").write_text("user app")
        else:
            target = tmp_path / "other"
            if kind == "foreign-symlink":
                target.mkdir()
            alias.symlink_to(target)
        prefs = Preferences([tile(alias)])
        original = copy.deepcopy(prefs.values)
        backup, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert backup is None and (alias.exists() or alias.is_symlink())
        assert prefs.values == original
        assert not (tmp_path / "backup.noindex").exists()
        assert not any("-u" in command for command in calls)
        assert ["/usr/bin/killall", "Dock"] not in calls

    def test_preserves_real_or_unrelated_legacy_path_foreign_symlink(self):
        app, tmp_path = self.app, self.tmp_path
        kind = 'foreign-symlink'
        alias = app.parent / "Anime MPV.app"
        if kind == "directory":
            alias.mkdir()
            (alias / "keep.txt").write_text("user app")
        else:
            target = tmp_path / "other"
            if kind == "foreign-symlink":
                target.mkdir()
            alias.symlink_to(target)
        prefs = Preferences([tile(alias)])
        original = copy.deepcopy(prefs.values)
        backup, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert backup is None and (alias.exists() or alias.is_symlink())
        assert prefs.values == original
        assert not (tmp_path / "backup.noindex").exists()
        assert not any("-u" in command for command in calls)
        assert ["/usr/bin/killall", "Dock"] not in calls

    def test_preserves_real_or_unrelated_legacy_path_broken_symlink(self):
        app, tmp_path = self.app, self.tmp_path
        kind = 'broken-symlink'
        alias = app.parent / "Anime MPV.app"
        if kind == "directory":
            alias.mkdir()
            (alias / "keep.txt").write_text("user app")
        else:
            target = tmp_path / "other"
            if kind == "foreign-symlink":
                target.mkdir()
            alias.symlink_to(target)
        prefs = Preferences([tile(alias)])
        original = copy.deepcopy(prefs.values)
        backup, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert backup is None and (alias.exists() or alias.is_symlink())
        assert prefs.values == original
        assert not (tmp_path / "backup.noindex").exists()
        assert not any("-u" in command for command in calls)
        assert ["/usr/bin/killall", "Dock"] not in calls

    def test_missing_legacy_bundle_still_migrates_old_dock_pin(self):
        app, tmp_path = self.app, self.tmp_path
        alias = app.parent / "Anime MPV.app"
        prefs = Preferences([tile(alias)])
        backup, _ = execute(app, tmp_path / "backup.noindex", prefs)
        assert backup and not alias.exists()
        assert prefs.values["persistent-apps"][0]["tile-data"]["file-data"]["_CFURLString"] == app.as_uri() + "/"

    def test_repeat_does_not_create_extra_backups_or_restart_dock(self):
        app, tmp_path = self.app, self.tmp_path
        alias = app.parent / "Anime MPV.app"
        alias.symlink_to(app)
        prefs = Preferences([tile(alias)])
        first, _ = execute(app, tmp_path / "backup.noindex", prefs)
        second, calls = execute(app, tmp_path / "backup.noindex", prefs)
        assert second is None
        assert list((tmp_path / "backup.noindex").iterdir()) == [first]
        assert ["/usr/bin/killall", "Dock"] not in calls

    def test_failed_dock_write_restores_pins_and_keeps_alias(self):
        app, tmp_path = self.app, self.tmp_path
        alias = app.parent / "Anime MPV.app"
        alias.symlink_to(app)
        prefs = Preferences([tile(alias)])
        prefs.sync_result = False
        original = copy.deepcopy(prefs.values)
        with self.assertRaisesRegex(RuntimeError, "Could not save Dock"):
            execute(app, tmp_path / "backup.noindex", prefs)
        assert alias.is_symlink() and app.is_dir()
        assert prefs.values == original

    def test_wrong_bundle_identity_stops_before_changes(self):
        app, tmp_path = self.app, self.tmp_path
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "other.app"}))
        alias = app.parent / "Anime MPV.app"
        alias.symlink_to(app)
        with self.assertRaisesRegex(RuntimeError, "Unexpected app identity"):
            execute(app, tmp_path / "backup.noindex", Preferences())
        assert alias.is_symlink()
        assert not (tmp_path / "backup.noindex").exists()

    def test_tile_transform_ignores_remote_urls_and_malformed_tiles(self):
        app, tmp_path = self.app, self.tmp_path
        alias = tmp_path / "Anime MPV.app"
        data = [None, "invalid", tile(alias)]
        data[2]["tile-data"]["file-data"]["_CFURLString"] = "file://remote" + str(alias)
        original = copy.deepcopy(data)
        result, changed = migrate_dock_tiles(data, {alias}, tmp_path / "pudge.app", "com.pudge.app")
        assert not changed and result == original and data == original


if __name__ == "__main__":
    unittest.main()
