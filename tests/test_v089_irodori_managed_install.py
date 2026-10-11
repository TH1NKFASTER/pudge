"""The optional managed Irodori TTS server: Settings, install, update and server start
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
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


IRODORI_SETTINGS = r"""
setPage('settings'); await __tick(100);
await __click('[data-settings-category="advanced"]'); await __tick(10);
const view = () => ({button: __visible('#installIrodoriTts') ? __text('#installIrodoriTts') : null,
                     disabled: $('installIrodoriTts').disabled, detail: __visible('#irodoriInstallDetail') ? __text('#irodoriInstallDetail') : null,
                     log: __visible('#openIrodoriInstallLog')});
const texts = {install: t('ui.installIrodori'), installing: t('ui.installing'), update: t('ui.updateIrodori'), started: t('ui.installationStarted')};
"""
IRODORI_API = r"""
__api.irodori_install_status = async () => window.STATUS.length > 1 ? window.STATUS.shift() : window.STATUS[0];
__api.install_irodori_tts = async () => ({ok: true, started: true});
__api.reveal_irodori_install_log = async () => ({ok: true});
"""
IRODORI_ON = {"settings": {"light_novels": {"audiobook_generation_provider": "irodori"}}}


def test_settings_install_the_managed_irodori_server_and_show_its_log_on_failure(page) -> None:
    result = page.run(
        IRODORI_SETTINGS
        + r"""
        const out = {texts, missing: view()};
        await __click('#installIrodoriTts'); await __tick(10); out.running = view(); out.toasts = __toasts();
        await __tick(1300); out.failed = view();
        await __click('#openIrodoriInstallLog'); await __tick(10);
        return out;
        """,
        state=IRODORI_ON,
        before=r"""
        window.STATUS = [
          {state: 'not_installed', installed: false, detail: 'Not installed'},
          {state: 'installing', installed: false, running: true, detail: 'Cloning…'},
          {state: 'failed', installed: false, detail: 'uv sync failed', log_path: '/tmp/irodori-install.log'},
        ];
        """
        + IRODORI_API,
    )
    out, texts = result.value, result.value["texts"]
    assert out["missing"] == {
        "button": texts["install"],
        "disabled": False,
        "detail": "Not installed",
        "log": False,
    }
    assert result.calls_of("install_irodori_tts") == [[]] and texts["started"] in out["toasts"]
    # While it installs the button is locked and the status is polled; a failure offers its log.
    assert out["running"] == {
        "button": texts["installing"],
        "disabled": True,
        "detail": "Cloning…",
        "log": False,
    }
    assert out["failed"] == {
        "button": texts["install"],
        "disabled": False,
        "detail": "uv sync failed",
        "log": True,
    }
    assert result.calls_of("reveal_irodori_install_log") == [[]]


@pytest.mark.parametrize("update_available", [True, False])
def test_settings_offer_an_irodori_update_whenever_it_is_installed(page, update_available: bool) -> None:
    # Update checks GitHub's main itself and says when there is nothing new.
    status = {
        "state": "ready",
        "installed": True,
        "update_available": update_available,
        "detail": "Update available" if update_available else "",
    }
    result = page.run(
        IRODORI_SETTINGS + "return {texts, view: view()};",
        state=IRODORI_ON,
        before=f"window.STATUS = [{json.dumps(status)}];" + IRODORI_API,
    )
    out = result.value
    assert out["view"] == {
        "button": out["texts"]["update"],
        "disabled": False,
        "detail": "Update available" if update_available else None,
        "log": False,
    }


def test_irodori_managed_install_is_optional_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    status = api.irodori_install_status()
    assert status["state"] == "not_installed"
    assert status["installed"] is False
    assert Path(status["install_dir"]) == tmp_path / "data" / "optional" / "irodori-tts-server"


def test_irodori_managed_install_uses_official_repo_and_cpu_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    root = api._irodori_install_root()
    fake_uv = root / "fake-uv"
    fake_uv.parent.mkdir(parents=True, exist_ok=True)
    fake_uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: fake_uv)
    monkeypatch.setattr(
        web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )

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


def test_irodori_managed_update_reuses_existing_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_app_module, "IRODORI_TTS_COMMIT", "")  # no pin: main
    api = _api(tmp_path, monkeypatch)
    repo = api._irodori_repo_dir()
    (repo / ".git").mkdir(parents=True)
    python = api._irodori_managed_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python", encoding="utf-8")
    fake_uv = api._irodori_install_root() / "fake-uv"
    fake_uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: fake_uv)
    monkeypatch.setattr(
        web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )

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


def test_test_irodori_autostarts_managed_local_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
