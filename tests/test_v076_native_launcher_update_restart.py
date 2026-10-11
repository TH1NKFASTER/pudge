from test_v16_updater_script import run_update_scenario

# The installer quitting a running Pudge through the native app (like Cmd+Q) is tested in
# test_v16_updater_script.py::test_update_asks_each_pudge_app_once_to_quit_and_a_finished_session_does_not_block.


def test_updater_waits_for_managed_app_before_install(tmp_path, monkeypatch):
    run_update_scenario(tmp_path, monkeypatch, source_exits=True)
