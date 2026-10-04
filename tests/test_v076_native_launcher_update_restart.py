from test_v16_updater_script import run_native_quit_scenario, run_update_scenario


def test_installer_stops_managed_app_through_native_quit(monkeypatch):
    run_native_quit_scenario(monkeypatch,stuck=False)


def test_updater_waits_for_managed_app_before_install(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=True)
