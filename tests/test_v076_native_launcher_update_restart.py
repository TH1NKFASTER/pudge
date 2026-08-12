from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_stops_execed_managed_app_process() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'pkill -f "pudge.app_entry"' in installer


def test_updater_stops_execed_managed_app_before_install() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")

    capture = updater.index("PUDGE_OLD_PIDS=")
    term = updater.index("/bin/kill -TERM")
    wait = updater.index("/bin/kill -0")
    force = updater.index("/bin/kill -KILL")
    installer = updater.index("/bin/zsh ./install.sh --update")

    assert capture < term < wait < force < installer
