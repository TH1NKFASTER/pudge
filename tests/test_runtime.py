from anime_mpv.runtime import python_executable


def test_runtime_python_prefers_installer_supplied_venv(monkeypatch) -> None:
    monkeypatch.setenv("ANIME_MPV_PYTHON", "/tmp/anime-mpv-python")
    assert python_executable() == "/tmp/anime-mpv-python"
