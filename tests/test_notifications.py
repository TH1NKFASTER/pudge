import subprocess

from pudge.notifications import send_native_notification


def test_native_notification_fallback_passes_text_as_osascript_arguments(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("pudge.notifications.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pudge.notifications._notification_helper_path", lambda: None)
    monkeypatch.setattr("pudge.notifications.subprocess.run", fake_run)

    assert send_native_notification("Episode ready", 'Title "quoted"') is True
    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/osascript"
    assert command[-2:] == ["Episode ready", 'Title "quoted"']
    assert kwargs["check"] is False
