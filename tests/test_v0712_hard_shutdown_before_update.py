from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_updater_hard_kills_all_managed_app_instances() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert "/usr/bin/pkill -9 -f '[p]udge[.]app_entry'" in updater
    assert "old Pudge process is still running after SIGKILL" in updater


def test_release_updater_verifies_shutdown_before_install_and_reopen() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")

    hard_kill = updater.index("/usr/bin/pkill -9 -f '[p]udge[.]app_entry'")
    verify = updater.index("/usr/bin/pgrep -f '[p]udge[.]app_entry'")
    installer = updater.index("/bin/zsh ./install.sh --update")
    reopen = updater.index("/usr/bin/open -n")

    assert hard_kill < verify < installer < reopen
