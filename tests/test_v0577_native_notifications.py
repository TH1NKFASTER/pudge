from __future__ import annotations

import subprocess
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.notifications import maybe_handle_notification_helper, send_native_notification
from pudge.web_app import WebAppApi


def test_notification_uses_installed_app_bundle_helper(monkeypatch, tmp_path: Path) -> None:
    helper = tmp_path / "pudge"
    helper.write_bytes(b"binary")
    helper.chmod(0o755)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("pudge.notifications.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pudge.notifications._notification_helper_path", lambda: helper)
    monkeypatch.setattr("pudge.notifications.subprocess.run", fake_run)

    assert send_native_notification("Серия готова", "Название — серия 6") is True
    command, kwargs = calls[0]
    assert command == [
        str(helper),
        "--pudge-native-notification",
        "Серия готова",
        "Название — серия 6",
    ]
    assert kwargs["env"]["PUDGE_NOTIFICATION_HELPER_ACTIVE"] == "1"


def test_notification_helper_failure_is_not_reported_as_delivered(monkeypatch, tmp_path: Path) -> None:
    helper = tmp_path / "pudge"
    helper.write_bytes(b"binary")
    helper.chmod(0o755)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr("pudge.notifications.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pudge.notifications._notification_helper_path", lambda: helper)
    monkeypatch.setattr("pudge.notifications.subprocess.run", fake_run)

    assert send_native_notification("Episode ready", "Example") is False
    assert len(calls) == 1
    assert calls[0][0] == str(helper)


def test_hidden_notification_mode_calls_native_framework(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.notifications.send_native_notification_direct",
        lambda subtitle, message: calls.append((subtitle, message)) or True,
    )

    assert maybe_handle_notification_helper(
        ["--pudge-native-notification", "Episode ready", "Example"]
    ) == 0
    assert calls == [("Episode ready", "Example")]
    assert maybe_handle_notification_helper([]) is None


def test_failed_ready_notification_is_not_marked_delivered(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Example", status="CURRENT", episodes=12, format="TV")
    )
    video = tmp_path / "Example - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=77, title="Example", episode=1, video_path=video, state="ready")
    )
    monkeypatch.setattr("pudge.manager.send_native_notification", lambda *_args: False)

    manager._notify_ready_episode(video=video, media_id=77, episode=1)

    assert manager.db.get_state("ready_notification:episode:77:1", "") == ""


def test_test_notification_api_requests_permission_and_sends(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    from pudge.config import write_config

    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    monkeypatch.setattr(
        "pudge.web_app.request_notification_permission",
        lambda timeout=4.0: {"supported": True, "granted": True, "error": ""},
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.web_app.send_native_notification",
        lambda subtitle, message: sent.append((subtitle, message)) or True,
    )

    result = api.test_notification()

    assert result["delivered"] is True
    assert sent


def test_installer_routes_helper_mode_through_app_bundle() -> None:
    install = Path("install.sh").read_text(encoding="utf-8")
    assert 'strcmp(argv[1], "--pudge-native-notification")' in install
    assert "return send_notification(argv[2], argv[3]);" in install
    assert "UNUserNotificationCenter" in install
    assert "Py_InitializeFromConfig(&config)" in install
    assert (
        install.index('strcmp(argv[1], "--pudge-native-notification")')
        < install.index("Py_InitializeFromConfig(&config)")
    )


def test_settings_does_not_contain_notification_test_button() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert 'id="testNotification"' not in html
    assert "pywebview.api.test_notification()" not in html
