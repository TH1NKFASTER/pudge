from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebAppApi:
    monkeypatch.setattr(web_app_module, "DATA_DIR", tmp_path / "data")
    config = _config(tmp_path)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def test_v89_ui_has_managed_irodori_install_button() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="installIrodoriTts"' in html
    assert 'id="irodoriInstallDetail"' in html
    assert "update_available" in html
    assert "install_irodori_tts" in html
    assert "irodori_install_status" in html
    assert "reveal_irodori_install_log" in html


def test_irodori_managed_install_is_optional_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    status = api.irodori_install_status()
    assert status["state"] == "not_installed"
    assert status["installed"] is False
    assert Path(status["install_dir"]) == tmp_path / "data" / "optional" / "irodori-tts-server"


def test_irodori_managed_install_uses_official_repo_and_cpu_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    root = api._irodori_install_root()
    fake_uv = root / "fake-uv"
    fake_uv.parent.mkdir(parents=True, exist_ok=True)
    fake_uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: fake_uv)
    monkeypatch.setattr(web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)

    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(command, **kwargs):
        command = [str(part) for part in command]
        cwd = Path(kwargs["cwd"]) if kwargs.get("cwd") else None
        calls.append((command, cwd))
        if command[:2] == ["/usr/bin/git", "clone"]:
            repo = Path(command[-1])
            (repo / ".git").mkdir(parents=True)
            (repo / ".env.example").write_text("# env\n", encoding="utf-8")
        if command and command[0] == str(fake_uv):
            python = api._irodori_managed_python()
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("python", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_app_module.subprocess, "run", fake_run)
    api._run_irodori_install()

    status = api.irodori_install_status()
    assert status["state"] == "ready"
    assert status["installed"] is True
    clone = next(command for command, _ in calls if "clone" in command)
    assert "https://github.com/Aratako/Irodori-TTS-Server.git" in clone
    sync = next(command for command, _ in calls if command and command[0] == str(fake_uv))
    assert sync == [str(fake_uv), "sync", "--locked", "--no-dev", "--extra", "cpu"]
    assert (api._irodori_repo_dir() / ".env").is_file()


def test_irodori_managed_update_reuses_existing_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    repo = api._irodori_repo_dir()
    (repo / ".git").mkdir(parents=True)
    python = api._irodori_managed_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python", encoding="utf-8")
    fake_uv = api._irodori_install_root() / "fake-uv"
    fake_uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: fake_uv)
    monkeypatch.setattr(web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        web_app_module.subprocess,
        "run",
        lambda command, **_kwargs: calls.append([str(x) for x in command]) or SimpleNamespace(returncode=0),
    )
    api._run_irodori_install()

    assert any(command[3:6] == ["fetch", "--depth", "1"] for command in calls if len(command) >= 6)
    assert any("origin/main" in command for command in calls)
    assert not any("clone" in command for command in calls)


def test_test_irodori_autostarts_managed_local_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    python = api._irodori_managed_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python", encoding="utf-8")

    attempts = {"count": 0}

    class Response:
        def json(self):
            return {"status": "ok"}

    def fake_health(_url: str, _key: str, timeout: float = 5.0):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("not running")
        return Response()

    starts: list[str] = []
    monkeypatch.setattr(api, "_irodori_health", fake_health)
    monkeypatch.setattr(api, "_start_managed_irodori_server", lambda url: starts.append(url) or True)
    monkeypatch.setattr(web_app_module.time, "sleep", lambda _seconds: None)

    result = api.test_irodori_tts({"url": "http://127.0.0.1:8088"})
    assert starts == ["http://127.0.0.1:8088"]
    assert result["ok"] is True
    assert result["managed"] is True
