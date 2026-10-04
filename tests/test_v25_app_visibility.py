"""Synthetic macOS flags and exact-path WebKit registration repair."""
import json
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pudge.mac_app_aliases import (
    LSREGISTER, cleanup_aliases, unregister_webkit_data_folders,
)


class Preferences:
    def read(self, key):
        return []


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="pudge-visibility-", suffix=".noindex"
        )
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.app = self.home / "Applications/pudge.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.pudge.app"})
        )
        self.backups = self.home / "backups.noindex"
        self.calls = []
        self.flags = stat.UF_HIDDEN | stat.UF_NODUMP
        self.chflags_exit = 0
        self.apply_chflags = True
        original_stat = Path.stat

        def synthetic_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == self.app:
                fields = {
                    name: getattr(value, name)
                    for name in dir(value) if name.startswith("st_")
                }
                fields["st_flags"] = self.flags
                return SimpleNamespace(**fields)
            return value

        self.stat_patch = patch.object(Path, "stat", synthetic_stat)
        self.stat_patch.start()
        self.addCleanup(self.stat_patch.stop)

    def run_command(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[0] == "/usr/bin/chflags":
            if self.apply_chflags and not self.chflags_exit:
                self.flags &= ~stat.UF_HIDDEN
            return SimpleNamespace(returncode=self.chflags_exit)
        return SimpleNamespace(returncode=0)

    def cleanup(self, names=(), webkit=False):
        return cleanup_aliases(
            self.app, "com.pudge.app", names, self.backups, Preferences(),
            run=self.run_command, webkit_home=self.home if webkit else None,
        )

    def test_hidden_bundle_is_visible_other_flags_and_original_backup_preserved(self):
        original = self.flags
        backup = self.cleanup()
        self.assertEqual(self.flags, stat.UF_NODUMP)
        self.assertEqual(
            json.loads((backup / "app-visibility.json").read_text()),
            {"path": str(self.app), "bsd_flags_before": original},
        )
        self.assertIn(["/usr/bin/chflags", "nohidden", str(self.app)], self.calls)
        self.assertIn([LSREGISTER, "-u", str(self.app)], self.calls)
        self.assertIn([LSREGISTER, "-f", str(self.app)], self.calls)
        self.assertEqual(self.calls[-1], ["/usr/bin/mdimport", str(self.app)])
        self.calls.clear()
        self.assertIsNone(self.cleanup())
        self.assertEqual(list(self.backups.iterdir()), [backup])

    def test_wrong_identity_prevents_all_commands_and_backups(self):
        (self.app / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "foreign.app"})
        )
        with self.assertRaisesRegex(RuntimeError, "Unexpected app identity"):
            self.cleanup()
        self.assertFalse(self.calls)
        self.assertFalse(self.backups.exists())

    def test_canonical_symlink_is_rejected(self):
        alias = self.home / "canonical.app"
        alias.symlink_to(self.app)
        with self.assertRaisesRegex(RuntimeError, "Canonical application is a symlink"):
            cleanup_aliases(
                alias, "com.pudge.app", (), self.backups, Preferences(),
                run=self.run_command,
            )
        self.assertFalse(self.calls)

    def test_chflags_failure_preserves_alias_and_webkit_registration(self):
        alias = self.app.parent / "Anime MPV.app"
        alias.symlink_to(self.app)
        (self.home / "Library/WebKit/com.pudge.app").mkdir(parents=True)
        self.chflags_exit = 1
        with self.assertRaisesRegex(RuntimeError, "Could not restore"):
            self.cleanup(names=("Anime MPV",), webkit=True)
        self.assertTrue(alias.is_symlink())
        self.assertFalse(any("-u" in call for call in self.calls))
        self.assertEqual(self.calls[-1], [LSREGISTER, "-f", str(self.app)])

    def test_success_status_without_flag_change_is_detected(self):
        self.apply_chflags = False
        with self.assertRaisesRegex(RuntimeError, "Canonical app remains hidden"):
            self.cleanup()
        self.assertFalse(any("-u" in call for call in self.calls))

    def test_webkit_data_records_retired_without_changing_data(self):
        root = self.home / "Library/WebKit"
        for identifier in ("com.pudge.app", "com.anime-mpv.app"):
            path = root / identifier
            path.mkdir(parents=True)
            (path / "synthetic-cookie.txt").write_text("preserve")
        self.cleanup(webkit=True)
        for identifier in ("com.pudge.app", "com.anime-mpv.app"):
            path = root / identifier
            self.assertIn([LSREGISTER, "-u", str(path)], self.calls)
            self.assertEqual((path / "synthetic-cookie.txt").read_text(), "preserve")
        final_registration = self.calls.index([LSREGISTER, "-f", str(self.app)])
        self.assertTrue(all(
            index < final_registration
            for index, call in enumerate(self.calls) if "-u" in call
        ))

    def test_app_contents_and_foreign_data_symlinks_are_preserved(self):
        root = self.home / "Library/WebKit"
        real = root / "com.pudge.app/Contents"
        real.mkdir(parents=True)
        (real / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "foreign"}))
        (root / "com.anime-mpv.app").symlink_to(self.app)
        unregister_webkit_data_folders(self.home, self.run_command)
        self.assertFalse(self.calls)
        self.assertTrue((real / "Info.plist").exists())
        self.assertTrue((root / "com.anime-mpv.app").is_symlink())

    def test_webkit_parent_symlink_prevents_registry_changes(self):
        target = self.home / "external"
        (target / "com.pudge.app").mkdir(parents=True)
        (self.home / "Library").mkdir()
        (self.home / "Library/WebKit").symlink_to(target)
        unregister_webkit_data_folders(self.home, self.run_command)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
