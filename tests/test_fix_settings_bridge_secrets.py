"""Saving Settings keeps running work; the page reaches only API methods; secrets stay off argv."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.audiobook_float import AudiobookFloatWindow
from pudge.config import AppConfig, load_config, write_config
from pudge.mpv_assistant import MpvAssistantWindow
from pudge.screen_text import ScreenTextPanel
from pudge.secrets_store import SecretStore
from pudge.web_app import WebAppApi
from tests.i18n_support import en


def _config_path(tmp_path: Path) -> Path:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    return cfg.config_path


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    instance = WebAppApi(_config_path(tmp_path))
    try:
        yield instance
    finally:
        instance.close()


def _spy_audiobooks(books, monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(books, "stop", lambda book_id: calls.append(f"stop:{book_id}"))
    monkeypatch.setattr(books, "stop_all", lambda: calls.append("stop_all"))
    monkeypatch.setattr(books, "close", lambda *, timeout=5.0: calls.append("close") or [])
    return calls


def test_saving_settings_keeps_the_playing_audiobook_running_jobs_and_services(api, tmp_path, monkeypatch):
    books = api.audiobooks
    calls = _spy_audiobooks(books, monkeypatch)
    manager, scheduler, jobs = api.manager, api.manager.work_scheduler, api.job_center
    light_novels, manga = api.light_novels, api.manga
    running = jobs.start("jiten-refresh", "Jiten")
    jobs.update(running, state="running")
    stopping = jobs.start("ocr", "OCR")
    jobs.update(stopping, state="running")
    assert jobs.request_cancel(stopping)
    watched = time.time() - 3600
    with manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO episodes(title, episode, video_path, state, watched_at, delete_after,"
            " updated_at) VALUES('Show', 1, ?, 'watched', ?, ?, ?)",
            (str(tmp_path / "Show 01.mkv"), watched, watched + 24 * 3600, watched),
        )

    result = api.save_settings(
        {
            "language": "ru",
            "power_manual_energy_saving": True,
            "delete_hours": 2,
            "library_root": str(tmp_path / "library2"),
        }
    )

    assert result["ok"] is True
    # Nothing was stopped, replaced or recovered as if the app had closed.
    assert calls == []
    assert api.audiobooks is books
    assert (api.manager, api.manager.work_scheduler, api.job_center) == (manager, scheduler, jobs)
    assert (api.light_novels, api.manga) == (light_novels, manga)
    assert jobs.get(running)["state"] == "running"
    assert jobs.get(stopping)["state"] == "cancel_requested"
    # ... and the running services follow the saved settings.
    assert api.config.ui.language == "ru" and manager.config is api.config
    assert scheduler.power_policy.manual_enabled is True
    assert Path(light_novels.root).resolve() == (tmp_path / "library2" / "Light Novels").resolve()
    assert Path(light_novels.root).is_dir()
    with manager.db.connect() as conn:
        delete_after = conn.execute("SELECT delete_after FROM episodes").fetchone()[0]
    assert delete_after == pytest.approx(watched + 2 * 3600)
    assert load_config(api.config_path).agent.delete_after_watched_hours == 2


def test_a_changed_stt_model_closes_the_old_audiobook_service_before_replacing_it(api, monkeypatch):
    books = api.audiobooks
    calls = _spy_audiobooks(books, monkeypatch)
    api.save_settings({"japanese_stt_model": "mlx-community/whisper-small"})
    assert calls == ["close"]
    assert api.audiobooks is not books
    assert api.audiobooks.stt_model == "mlx-community/whisper-small"


def test_the_page_cannot_make_mokuro_python_an_arbitrary_program(api, tmp_path):
    with pytest.raises(ValueError, match=en("error.settings.mokuroPythonInvalid").split("(")[0].strip()):
        api.save_settings({"manga_mokuro_python": "/bin/sh"})
    env = tmp_path / "mokuro-env"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    disguised = env / "bin" / "python3"
    disguised.symlink_to(Path("/bin/rm") if Path("/bin/rm").exists() else Path("/bin/sh"))
    with pytest.raises(ValueError):
        api.save_settings({"manga_mokuro_python": str(disguised)})
    assert api.config.tools.manga_mokuro_python == ""
    assert load_config(api.config_path).tools.manga_mokuro_python == ""

    disguised.unlink()
    disguised.symlink_to(Path(sys.executable).resolve())
    api.save_settings({"manga_mokuro_python": str(disguised)})
    assert api.manga.mokuro_python == str(disguised)
    assert load_config(api.config_path).tools.manga_mokuro_python == str(disguised)


class _Inline:
    """threading.Thread stand-in: pywebview's bridge call runs before js_bridge_call returns."""

    def __init__(self, target, args=(), **kwargs):
        self._run = lambda: target(*args)

    def start(self):
        self._run()


def test_main_window_page_reaches_public_api_methods_only(tmp_path, monkeypatch):
    webview = pytest.importorskip("webview")
    util = pytest.importorskip("webview.util")
    from pudge import logging_utils, single_instance

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(web_app, "_INSTANCE_LOCK", None)
    monkeypatch.setattr(logging_utils, "set_log_rotation_owner", lambda owner: None)
    monkeypatch.setattr(web_app, "_set_macos_runtime_identity", lambda: False)
    monkeypatch.setattr(
        single_instance, "acquire", lambda: SimpleNamespace(serve_focus_requests=lambda callback: None)
    )
    seen: dict = {}

    def asset_server(api):
        seen["api"] = api
        return SimpleNamespace(shutdown=lambda: None, server_close=lambda: None), "http://127.0.0.1:9"

    monkeypatch.setattr(web_app, "_start_asset_server", asset_server)
    monkeypatch.setattr(webview, "windows", [])
    monkeypatch.setattr(util, "Thread", _Inline)

    def start(on_started, **kwargs):
        window = webview.windows[-1]
        window.gui = SimpleNamespace(renderer="mshtml")  # no session token in this harness
        answers: list[str] = []
        window.evaluate_js = answers.append
        api_object = window._js_api
        seen["attributes"] = [name for name in dir(api_object) if not name.startswith("__")]
        seen["functions"] = dict(window._functions)
        for attempt in (
            "config.qbittorrent.__setattr__",
            "app_update_progress.__self__.config.qbittorrent.__setattr__",
            "app_update_progress.__closure__",
            "manager.db.path.unlink",
        ):
            util.js_bridge_call(window, attempt, ["pre_download_command", "touch /tmp/pwned"], "x")
        seen["hostile_answers"] = list(answers)
        util.js_bridge_call(window, "app_update_progress", [], "ok")
        seen["answers"] = answers[len(seen["hostile_answers"]) :]
        seen["hook"] = seen["api"].config.qbittorrent.pre_download_command
        seen["database"] = (tmp_path / "library.sqlite3").exists()

    monkeypatch.setattr(webview, "start", start)
    assert web_app.launch_web_app(_config_path(tmp_path)) == 0

    assert seen["attributes"] == []  # nothing besides what every object has
    names = set(seen["functions"])
    assert {"get_state", "save_settings", "app_update_progress"} <= names
    assert all(not name.startswith("_") and "." not in name for name in names)
    assert not {"config", "manager", "audiobooks", "job_center", "logger", "window"} & names
    assert all(callable(function) for function in seen["functions"].values())
    assert seen["hostile_answers"] == []
    assert seen["hook"] == "" and seen["database"] is True
    assert len(seen["answers"]) == 1 and "isError" not in seen["answers"][0]


class _FakeWindow:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.exposed: dict = {}
        self.events = SimpleNamespace(closed=_Handlers(), shown=_Handlers(), loaded=_Handlers())

    def expose(self, *functions):
        self.exposed.update({function.__name__: function for function in functions})


class _Handlers(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class _SmallApi:
    asset_base = "http://127.0.0.1:9"

    def __init__(self):
        self.config = SimpleNamespace(tools=SimpleNamespace(manga_mokuro_python=""))
        self.logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)

    def ping(self, value):
        return {"pong": value}

    def _private(self):
        raise AssertionError("never exposed")


def test_secondary_windows_expose_only_api_methods(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    created: list[_FakeWindow] = []

    def create_window(title, **kwargs):
        created.append(_FakeWindow(kwargs))
        return created[-1]

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window))
    api = _SmallApi()
    assert AudiobookFloatWindow(api).show()["ok"]
    assert MpvAssistantWindow(api).show()["ok"]
    ScreenTextPanel(api)._create_window(position=(0, 0), size=(300, 200), game_pid=0, source=None)

    assert len(created) == 3
    for window in created:
        assert window.kwargs.get("js_api") is None
        assert set(window.exposed) == {"ping"}
        assert window.exposed["ping"]("hi") == {"pong": "hi"}


def test_page_declares_a_policy_without_remote_scripts_or_plugins(page):
    for name in ("index.html", "audiobook_float.html", "screen_text.html", "mpv_assistant.html"):
        result = page.run(
            "return document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]')?.getAttribute('content') || ''",
            name=name,
            allow_errors=True,
        )
        directives = {
            part.split()[0]: part.split()[1:] for part in str(result.value).split(";") if part.strip()
        }
        assert directives, name
        scripts = directives.get("script-src", directives.get("default-src"))
        assert scripts and set(scripts) <= {"'self'", "'unsafe-inline'", "'unsafe-eval'"}, name
        assert directives["object-src"] == ["'none'"], name
        assert set(directives.get("default-src", [])) <= {"'self'"}, name
        assert "*" in directives["img-src"], name  # AniList and other remote covers keep loading


class _Security:
    """The macOS `security` tool for generic passwords: argv commands and -i (commands on stdin)."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.argv: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.argv.append([str(arg) for arg in args])
        if list(args[1:]) == ["-i"]:
            for line in str(kwargs.get("input") or "").splitlines():
                self._command(line.split())
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return self._command(list(args[1:]))

    def _command(self, words):
        operation, options = words[0], words[1:]

        def option(flag):
            return options[options.index(flag) + 1] if flag in options else None

        key = (option("-s"), option("-a"))
        if operation == "add-generic-password":
            value = bytes.fromhex(option("-X")).decode("utf-8") if "-X" in options else option("-w")
            if key in self.items and "-U" not in options:
                return SimpleNamespace(returncode=45, stdout="")
            self.items[key] = value
            return SimpleNamespace(returncode=0, stdout="")
        if operation == "find-generic-password":
            if key not in self.items:
                return SimpleNamespace(returncode=44, stdout="")
            value = self.items[key]
            # Like the real tool, -w prints a value that is not printable ASCII as hex.
            shown = value if value.isascii() and value.isprintable() else value.encode("utf-8").hex()
            return SimpleNamespace(returncode=0, stdout=shown + "\n")
        if operation == "delete-generic-password":
            return SimpleNamespace(returncode=0 if self.items.pop(key, None) is not None else 44)
        return SimpleNamespace(returncode=1, stdout="")


def test_keychain_secrets_never_appear_on_a_command_line(tmp_path, monkeypatch):
    from pudge import config as config_module

    security = _Security()
    monkeypatch.setattr(SecretStore, "available", property(lambda self: True))
    monkeypatch.setattr("pudge.secrets_store.subprocess.run", security)
    store = SecretStore()
    monkeypatch.setattr(config_module, "_SECRET_STORE", store)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")  # the app's own file

    assert store.set("qbittorrent-password", 'p@ss "word" with spaces \\ -w') is True
    assert store.get("qbittorrent-password") == 'p@ss "word" with spaces \\ -w'
    # A value the Keychain would hand back changed (as hex) is not stored there.
    assert store.set("llm-api-key", "пароль") is False
    cfg = AppConfig()
    cfg.qbittorrent.password = "synthetic-qbt-password"
    cfg.llm.api_key = "synthetic-llm-key"
    write_config(cfg, tmp_path / "config.toml")
    loaded = load_config(tmp_path / "config.toml")
    assert (loaded.qbittorrent.password, loaded.llm.api_key) == (
        "synthetic-qbt-password",
        "synthetic-llm-key",
    )
    assert "synthetic-qbt-password" not in (tmp_path / "config.toml").read_text(encoding="utf-8")

    secrets = ['p@ss "word" with spaces \\ -w', "пароль", "synthetic-qbt-password", "synthetic-llm-key"]
    command_lines = " ".join(" ".join(argv) for argv in security.argv)
    for secret in secrets:
        assert secret not in command_lines
        assert secret.encode("utf-8").hex() not in command_lines
