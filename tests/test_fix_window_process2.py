"""UIPROC2: Pudge.app's launcher started with --pudge-window-process runs only the window process."""

from pudge import app_entry, window_process


def test_the_window_process_flag_runs_only_the_window_process(monkeypatch):
    calls = []
    monkeypatch.setattr(window_process, "main", lambda: calls.append("window") or 7)
    import pudge.private_files as private_files

    monkeypatch.setattr(private_files, "harden_private_storage", lambda: calls.append("app start"))
    assert app_entry.main(["--pudge-window-process"]) == 7
    assert calls == ["window"]
    assert window_process.CHILD_ARG == "--pudge-window-process"
