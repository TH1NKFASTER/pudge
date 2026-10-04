from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_download_retries_transport_failures() -> None:
    updater = (ROOT / "pudge" / "updater.py").read_text(encoding="utf-8")
    assert "except httpx.TransportError as exc:" in updater
    assert "APP update download retry" in updater
    assert "target.unlink(missing_ok=True)" in updater


def test_updater_waits_for_old_managed_app_before_install(tmp_path,monkeypatch) -> None:
    from test_v16_updater_script import run_update_scenario
    run_update_scenario(tmp_path,monkeypatch,source_exits=False)


def test_update_install_does_not_use_native_webview_confirm() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function installAppUpdate()")
    body = html[start : start + 500]
    assert "confirm(" not in body
    assert "pywebview.api.app_update_install()" in body
