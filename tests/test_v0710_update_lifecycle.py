from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_download_retries_transport_failures() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert "except httpx.TransportError as exc:" in updater
    assert "APP update download retry" in updater
    assert "target.unlink(missing_ok=True)" in updater


def test_updater_waits_for_old_managed_app_before_install() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")

    hard_kill = updater.index("/usr/bin/pkill -9 -f '[p]udge[.]app_entry'")
    verify = updater.index("/usr/bin/pgrep -f '[p]udge[.]app_entry'")
    installer = updater.index("/bin/zsh ./install.sh --update")
    reopen = updater.index("/usr/bin/open -n")

    assert hard_kill < verify < installer < reopen

def test_update_install_does_not_use_native_webview_confirm() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function installAppUpdate()")
    body = html[start : start + 500]
    assert "confirm(" not in body
    assert "pywebview.api.app_update_install()" in body
