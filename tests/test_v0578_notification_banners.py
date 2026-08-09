from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import anime_mpv.notifications as notifications
from anime_mpv.notifications import (
    _foreground_presentation_options,
    _install_foreground_notification_delegate,
)
from anime_mpv.web_app import _request_notification_permission_after_launch


def test_presentation_options_include_banner_list_and_sound() -> None:
    framework = SimpleNamespace(
        UNNotificationPresentationOptionSound=1,
        UNNotificationPresentationOptionBanner=2,
        UNNotificationPresentationOptionList=4,
        UNNotificationPresentationOptionAlert=8,
    )

    assert _foreground_presentation_options(framework) == 7


def test_presentation_options_fall_back_to_alert_on_older_macos() -> None:
    framework = SimpleNamespace(
        UNNotificationPresentationOptionSound=1,
        UNNotificationPresentationOptionAlert=8,
    )

    assert _foreground_presentation_options(framework) == 9


def test_foreground_delegate_explicitly_presents_banner(monkeypatch) -> None:
    class FakeNSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    fake_framework = SimpleNamespace(
        UNNotificationPresentationOptionSound=1,
        UNNotificationPresentationOptionBanner=2,
        UNNotificationPresentationOptionList=4,
    )
    fake_foundation = SimpleNamespace(NSObject=FakeNSObject)
    monkeypatch.setitem(sys.modules, "UserNotifications", fake_framework)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setattr(notifications, "_NOTIFICATION_DELEGATE", None)

    class FakeCenter:
        delegate = None

        def setDelegate_(self, delegate) -> None:
            self.delegate = delegate

    center = FakeCenter()
    delegate = _install_foreground_notification_delegate(center)
    presented: list[int] = []

    assert delegate is center.delegate
    delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
        center, object(), presented.append
    )
    assert presented == [7]


def test_permission_is_requested_when_regular_app_opens(monkeypatch, tmp_path: Path) -> None:
    calls: list[float] = []
    logs: list[tuple[str, tuple[object, ...]]] = []

    monkeypatch.setattr(
        "anime_mpv.web_app.request_notification_permission",
        lambda timeout=12.0: calls.append(timeout)
        or {"supported": True, "granted": True, "error": ""},
    )
    api = SimpleNamespace(
        logger=SimpleNamespace(info=lambda message, *args: logs.append((message, args)))
    )

    _request_notification_permission_after_launch(api)

    assert calls == [12.0]
    assert logs
    assert "notification.permission_startup" in logs[0][0]


def test_installer_keeps_usernotifications_framework_and_new_version() -> None:
    install = Path("install.sh").read_text(encoding="utf-8")
    assert "--hidden-import UserNotifications" in install
