from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_stops_execed_managed_app_process() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'pkill -f "pudge.app_entry"' in installer


def test_updater_stops_execed_managed_app_before_install() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")

    capture = updater.index("source_pid = os.getpid()")
    hard_kill = updater.index("/bin/kill -9 {source_pid}")
    verify = updater.index("/bin/kill -0 {source_pid}")
    installer = updater.index("/bin/zsh ./install.sh --update")
    reopen = updater.index("/usr/bin/open -n")

    assert capture < hard_kill < verify < installer < reopen
