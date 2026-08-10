from pudge.runtime import python_executable


def test_runtime_python_prefers_installer_supplied_venv(monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_PYTHON", "/tmp/pudge-python")
    assert python_executable() == "/tmp/pudge-python"
