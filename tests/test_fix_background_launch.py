"""An installer relaunch (open -g … --args --pudge-background) does not take the screen."""

from pudge import app_entry, window_process


def test_the_background_flag_is_removed_and_remembered_for_the_window_process(monkeypatch):
    monkeypatch.delenv("PUDGE_LAUNCH_BACKGROUND", raising=False)
    seen = []
    import pudge.private_files as private_files

    def stop():
        seen.append("started")
        raise SystemExit(0)

    monkeypatch.setattr(private_files, "harden_private_storage", stop)
    try:
        app_entry.main(["--pudge-background"])
    except SystemExit:
        pass
    import os

    assert seen == ["started"]
    assert os.environ.get("PUDGE_LAUNCH_BACKGROUND") == "1"
    # Not monkeypatch.delenv: that would put "1" back after the test and leak it into others.
    os.environ.pop("PUDGE_LAUNCH_BACKGROUND", None)


def test_only_the_first_activation_after_a_background_launch_is_skipped(monkeypatch):
    monkeypatch.setenv("PUDGE_LAUNCH_BACKGROUND", "1")
    assert window_process.background_launch_once() is True
    assert window_process.background_launch_once() is False
