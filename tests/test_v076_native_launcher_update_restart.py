from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_stops_execed_managed_app_process() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'pkill -f "pudge.app_entry"' in installer


def test_updater_stops_execed_managed_app_before_install() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert "/usr/bin/pkill -f 'pudge.app_entry'" in updater

    old_bundle_kill = updater.index("Contents' / 'MacOS' / APP_NAME")
    managed_kill = updater.index("/usr/bin/pkill -f 'pudge.app_entry'")
    installer = updater.index("./install.sh --update")

    assert old_bundle_kill < managed_kill < installer
