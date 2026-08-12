from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_updater_does_not_rely_on_pgrep_for_source_gui_shutdown() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    start = updater.index("def _launch_script(")
    body = updater[start : start + 5000]

    assert "source_pid = os.getpid()" in body
    assert 'f"/bin/kill -9 {source_pid}' in body
    assert "pkill -9 -f '[p]udge[.]app_entry'" not in body
