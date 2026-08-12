from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_updater_targets_the_exact_source_app_pid() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert "source_pid = os.getpid()" in updater
    assert "/bin/kill -9 {source_pid}" in updater
    assert "/bin/kill -0 {source_pid}" in updater
    assert "Updater: stopping source Pudge PID" in updater


def test_release_updater_verifies_source_exit_before_install_and_reopen() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")

    capture = updater.index("source_pid = os.getpid()")
    hard_kill = updater.index("/bin/kill -9 {source_pid}")
    verify = updater.index("/bin/kill -0 {source_pid}")
    installer = updater.index("/bin/zsh ./install.sh --update")
    reopen = updater.index("/usr/bin/open -n")

    assert capture < hard_kill < verify < installer < reopen
