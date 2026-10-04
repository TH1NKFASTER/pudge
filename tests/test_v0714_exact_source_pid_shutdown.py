from test_v16_updater_script import run_update_scenario


def test_updater_never_uses_a_broad_process_kill(tmp_path,monkeypatch):
    run_update_scenario(tmp_path,monkeypatch,source_exits=True)
