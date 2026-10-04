# Regression contract updated: common cleanup replaced the old forced SIGKILL.
from test_v16_updater_script import run_update_scenario


def test_release_updater_preserves_unrelated_processes(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=True)


def test_release_updater_refuses_install_before_source_exit(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=False)
