"""Mokuro and Irodori install tested versions and can still be updated (behaviour).

Mokuro: Install and repair put the tested set on the Mac (`pip install mokuro==<tested> -c
<set>`); Update Mokuro installs the newest Mokuro into a separate environment, switches to it
only when it passes its check, and records its set for later repairs. Irodori: the installed
commit is recorded as last good; repair reinstalls it; Update follows main, starts the new
server once, and goes back to the last good commit when it does not answer. pip, venv, the
Mokuro checks, git's network side and the Irodori server are stand-ins; git itself is real.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from test_v089_irodori_managed_install import IRODORI_API, IRODORI_ON, IRODORI_SETTINGS, _api

REAL_RUN = subprocess.run
NEWER = {
    "mokuro": "0.2.6",
    "manga-ocr": "0.1.17",
    "torch": "2.15.0",
    "torchvision": "0.30.0",
    "numpy": "2.6.0",
}


# ---------------------------------------------------------------------------------------
# Mokuro
class MokuroStandIn:
    """venv, pip and Python of Mokuro environments; each environment folder keeps its packages."""

    def __init__(self, api, newest: dict[str, str]) -> None:
        self.api = api
        self.newest = newest  # what `pip install mokuro manga-ocr` puts into a new environment
        # The package index's newest releases (`pip index versions`), by default those of
        # `newest`; None: the index cannot be reached.
        self.index: dict[str, str] | None = {}
        self.calls: list[list[str]] = []
        self.check = lambda env, code: (0, "ok\n")  # (exit code, output) of `python -c`
        self.checked: list[tuple[Path, dict[str, str]]] = []  # (environment, packages in use then)

    @staticmethod
    def packages(env: Path) -> dict[str, str]:
        path = env / "packages.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def run(self, command, *args, **kwargs):
        command = [str(part) for part in command]
        self.calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        env = Path(command[0]).parent.parent
        if command[1:5] == ["-m", "pip", "index", "versions"]:
            name = command[5]
            if self.index is None:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr=f"WARNING: Retrying after connection broken\nERROR: No matching distribution found for {name}\n",
                )
            latest = self.index.get(name) or self.newest[name]
            lines = [f"{name} ({latest})", f"Available versions: {latest}, 0.1.0"]
            if self.packages(env).get(name):
                lines += [f"  INSTALLED: {self.packages(env)[name]}", f"  LATEST:    {latest}"]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")
        if command[1:4] == ["-m", "pip", "install"]:
            if "-c" in command:
                constraints, tested = self.api._mokuro_tested_set()
                assert command[command.index("-c") + 1] == str(constraints)
                installed = dict(tested)
            else:
                installed = dict(self.newest)
            (env / "packages.json").write_text(json.dumps(installed), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1:4] == ["-m", "pip", "freeze"]:
            lines = [f"{name}=={version}" for name, version in sorted(self.packages(env).items())]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")
        if command[1:2] == ["-c"]:
            in_use = self.packages(self.api._mokuro_ocr_root() / "venv")
            self.checked.append((env, in_use))
            code, output = self.check(env, command[2])
            return SimpleNamespace(returncode=code, stdout=output, stderr="")
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def mokuro(tmp_path, monkeypatch, caplog):
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(web_app_module, "DEFAULT_LOG_PATH", tmp_path / "logs" / "pudge.log")
    logging.getLogger("pudge").propagate = True  # WebAppApi detached the app log from caplog
    caplog.set_level(logging.INFO)
    stand_in = MokuroStandIn(api, NEWER)
    monkeypatch.setattr(web_app_module.subprocess, "run", stand_in.run)
    api._run_mokuro_ocr_install()
    assert api.manga_ocr_status()["state"] == "ready"
    stand_in.calls.clear()
    stand_in.checked.clear()
    return api, stand_in


def _update(api) -> dict:
    api.update_mokuro()
    api._manga_ocr_install_thread.join(30)
    return api.manga_ocr_status()


def test_mokuro_installs_the_tested_set(mokuro) -> None:
    api, stand_in = mokuro
    constraints, tested = api._mokuro_tested_set()
    # The set of the user's working Mokuro environment, exactly.
    assert {
        name: tested[name] for name in ("mokuro", "manga-ocr", "torch", "torchvision", "transformers")
    } == {
        "mokuro": "0.2.5",
        "manga-ocr": "0.1.16",
        "torch": "2.14.1",
        "torchvision": "0.29.1",
        "transformers": "5.18.0",
    }
    assert len(tested) == 55  # every package of the freeze
    env = api._mokuro_ocr_root() / "venv"
    assert stand_in.packages(env) == tested
    status = api.manga_ocr_status()
    assert (status["mokuro_version"], status["manga_ocr_version"], status["updatable"]) == (
        "0.2.5",
        "0.1.16",
        True,
    )
    assert (status["last_checked_at"], status["last_updated_at"]) == (0.0, 0.0)
    assert not (web_app_module.DATA_DIR / "optional" / "mokuro-ocr" / "working-set.txt").exists()


def test_update_switches_to_the_newest_mokuro_only_after_it_passed_its_check(mokuro, caplog) -> None:
    api, stand_in = mokuro
    _constraints, tested = api._mokuro_tested_set()
    root = api._mokuro_ocr_root()

    status = _update(api)

    # The newest Mokuro went into a separate environment, without the tested set ...
    [install] = [command for command in stand_in.calls if command[1:4] == ["-m", "pip", "install"]]
    assert install[0] == str(root / "venv-update" / "bin" / "python")
    assert "-c" not in install and {"mokuro", "manga-ocr"} <= set(install)
    # ... was checked there (imports, then the models) while the old one was still in use ...
    assert [env for env, _ in stand_in.checked] == [root / "venv-update"] * 2
    assert all(in_use == tested for _, in_use in stand_in.checked)
    assert "import mokuro, manga_ocr, torch" in [c for c in stand_in.calls if c[1:2] == ["-c"]][0][2]
    # ... and is the one in use now.
    assert stand_in.packages(root / "venv") == NEWER
    assert not (root / "venv-update").exists() and not (root / "venv-previous").exists()
    assert status["state"] == "ready" and status["running"] is False
    assert (status["mokuro_version"], status["manga_ocr_version"]) == ("0.2.6", "0.1.17")
    assert status["last_updated_at"] > 0 and status["last_checked_at"] == status["last_updated_at"]
    assert status["detail"] == web_app_module.t(
        "web.mokuroUpdatedPackages", changes="Mokuro 0.2.5 → 0.2.6, MangaOCR 0.1.16 → 0.1.17"
    )
    assert "RESULT step=mokuro.update result=updated from=0.2.5 to=0.2.6" in caplog.text
    assert "changed=mokuro:0.2.5->0.2.6,manga-ocr:0.1.16->0.1.17" in caplog.text
    # The new environment's set is what Install and repair use from now on.
    assert api._mokuro_tested_set()[1] == NEWER


def test_a_failed_check_keeps_the_working_mokuro(mokuro, caplog) -> None:
    api, stand_in = mokuro
    _constraints, tested = api._mokuro_tested_set()
    root = api._mokuro_ocr_root()
    stand_in.check = lambda env, code: (
        1,
        "Traceback (most recent call last):\nImportError: libtorch_cpu.dylib\n",
    )

    status = _update(api)

    assert stand_in.packages(root / "venv") == tested
    assert not (root / "venv-update").exists()
    assert status["state"] == "ready" and status["updatable"] is True
    assert status["mokuro_version"] == "0.2.5"
    assert status["detail"] == web_app_module.err(
        "setup.mokuroUpdateKept", version="0.2.6", kept="0.2.5", reason="ImportError: libtorch_cpu.dylib"
    )
    assert status["last_checked_at"] > 0 and status["last_updated_at"] == 0.0
    assert "FAIL step=mokuro.update reason=check_failed version=0.2.6 kept=0.2.5" in caplog.text
    # Repair still installs the tested set.
    assert api._mokuro_tested_set()[1] == tested


def test_an_update_that_cannot_install_keeps_the_working_mokuro(mokuro, monkeypatch, caplog) -> None:
    api, stand_in = mokuro
    _constraints, tested = api._mokuro_tested_set()
    root = api._mokuro_ocr_root()
    run = stand_in.run

    def offline(command, *args, **kwargs):
        if [str(part) for part in command][1:4] == ["-m", "pip", "install"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(web_app_module.subprocess, "run", offline)
    status = _update(api)

    assert stand_in.packages(root / "venv") == tested and not (root / "venv-update").exists()
    assert status["state"] == "ready"
    assert status["detail"] == web_app_module.err(
        "setup.mokuroUpdateFailed", kept="0.2.5", reason=web_app_module.err("setup.mokuroInstallExit", code=1)
    )
    assert "FAIL step=mokuro.update" in caplog.text


def test_update_says_when_mokuro_is_already_the_newest(mokuro, caplog) -> None:
    api, stand_in = mokuro
    stand_in.newest = dict(api._mokuro_tested_set()[1])

    status = _update(api)

    assert stand_in.checked == []  # nothing new to check or switch to
    assert not any(command[1:3] == ["-m", "venv"] for command in stand_in.calls)  # nor built
    assert not (api._mokuro_ocr_root() / "venv-update").exists()
    assert status["detail"] == web_app_module.t(
        "web.mokuroNewestAlready", version="0.2.5", manga_ocr="0.1.16"
    )
    assert status["last_checked_at"] > 0 and status["last_updated_at"] == 0.0
    assert "RESULT step=mokuro.update result=current version=0.2.5" in caplog.text


def test_repair_after_an_update_reinstalls_the_recorded_set(mokuro) -> None:
    api, stand_in = mokuro
    _update(api)
    root = api._mokuro_ocr_root()
    shutil.rmtree(root)  # e.g. the cache was emptied
    assert api.manga_ocr_status()["state"] == "not_installed"

    api.install_manga_ocr()
    api._manga_ocr_install_thread.join(30)

    [install] = [
        command for command in stand_in.calls if command[1:4] == ["-m", "pip", "install"] and "-c" in command
    ]
    assert "mokuro==0.2.6" in install
    assert install[install.index("-c") + 1] == str(
        web_app_module.DATA_DIR / "optional" / "mokuro-ocr" / "working-set.txt"
    )
    assert stand_in.packages(root / "venv") == NEWER
    assert api.manga_ocr_status()["mokuro_version"] == "0.2.6"


def test_mokuro_update_is_not_offered_for_your_own_environment(mokuro, tmp_path) -> None:
    api, _stand_in = mokuro
    own = tmp_path / "own-mokuro" / "bin" / "python"
    own.parent.mkdir(parents=True)
    own.write_text("", encoding="utf-8")
    api.config.tools.manga_mokuro_python = str(own)
    assert api.manga_ocr_status()["updatable"] is False
    with pytest.raises(RuntimeError, match="your own environment"):
        api.update_mokuro()


def en_text(key: str, **params) -> str:
    return web_app_module.t(key, lang="en", **params).strip()


MOKURO_SETTINGS = r"""
setPage('settings'); await __tick(100);
await PudgeMedia.refreshMangaOcrStatus();
// The tab the Manga OCR block is on: Advanced once Mokuro works, Essential before.
const category = $('mangaOcrStatus').closest('.setting-block').dataset.settingsCategory;
await __click(`[role="tab"][data-settings-category="${category}"]`); await __tick(10);
const view = () => ({install: __visible('#installMangaOcr'), update: __visible('#updateMokuro') ? __text('#updateMokuro') : null,
                     disabled: $('updateMokuro').disabled, version: __text('#mangaOcrVersion'), status: __text('#mangaOcrStatus')});
const texts = {update: t('media.updateMokuro'), updating: t('media.updatingMokuro'), state: t('media.ocr.updating'),
               version: [t('media.mokuroVersion', {version: '0.2.5'}), t('media.mangaOcrVersion', {version: '0.1.16'})].join(' · ')};
"""
READY = {
    "backend": "mokuro",
    "state": "ready",
    "running": False,
    "installed": True,
    "model_ready": True,
    "updatable": True,
    "mokuro_version": "0.2.5",
    "manga_ocr_version": "0.1.16",
    "last_checked_at": 0,
    "last_updated_at": 0,
}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_show_the_mokuro_version_and_update_it(page, lang) -> None:
    updating = {**READY, "state": "updating", "running": True, "updatable": False}
    result = page.run(
        MOKURO_SETTINGS
        + r"""
        const out = {texts, ready: view()};
        await __click('#updateMokuro'); await __tick(10); out.updating = view();
        return out;
        """,
        lang=lang,
        before=f"window.STATUS = [{json.dumps(READY)}, {json.dumps(READY)}, {json.dumps(updating)}];"
        + r"""
        __api.manga_ocr_status = async () => window.STATUS.length > 1 ? window.STATUS.shift() : window.STATUS[0];
        __api.update_mokuro = async () => ({state: 'updating', running: true});
        """,
    )
    out, texts = result.value, result.value["texts"]
    assert out["ready"] == {
        "install": False,
        "update": texts["update"],
        "disabled": False,
        "version": texts["version"],
        "status": out["ready"]["status"],
    }
    assert result.calls_of("update_mokuro") == [[]] and result.calls_of("install_manga_ocr") == []
    assert out["updating"]["update"] == texts["updating"] and out["updating"]["disabled"] is True
    assert out["updating"]["install"] is False and out["updating"]["status"] == texts["state"]


def test_settings_show_when_mokuro_was_checked_and_updated(page) -> None:
    checked = {**READY, "last_checked_at": 1790000000, "last_updated_at": 1789000000}
    result = page.run(
        MOKURO_SETTINGS + "return {texts, view: view()};",
        api={"manga_ocr_status": checked},
    )
    version = result.value["view"]["version"]
    assert version.startswith(result.value["texts"]["version"] + " · ")
    assert (
        en_text("settings.toolLastChecked", date="") in version
        and en_text("settings.toolLastUpdated", date="") in version
    )


def test_settings_offer_install_not_update_while_mokuro_is_missing(page) -> None:
    missing = {
        "backend": "mokuro",
        "state": "not_installed",
        "running": False,
        "installed": False,
        "updatable": False,
    }
    result = page.run(MOKURO_SETTINGS + "return view();", api={"manga_ocr_status": missing})
    assert (
        result.value["install"] is True and result.value["update"] is None and result.value["version"] == "—"
    )


# ---------------------------------------------------------------------------------------
# Irodori
def _commit(repo: Path, message: str) -> str:
    git = ["git", "-C", str(repo), "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid"]
    (repo / "server.py").write_text(f"# {message}\n", encoding="utf-8")
    REAL_RUN([*git, "add", "-A"], check=True)
    REAL_RUN([*git, "commit", "-qm", message], check=True)
    return REAL_RUN(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


class IrodoriStandIn:
    """GitHub's side of git (clone, fetch) and uv; reset and rev-parse are real git.

    The clone has the installed commit; `fetch origin main` brings `newer` (when set)."""

    def __init__(self, api) -> None:
        self.api = api
        self.calls: list[list[str]] = []
        self.first = self.newer = ""
        self.fetch_main_to = ""
        self.synced_at: list[str] = []  # HEAD at each uv sync
        self.uv = api._irodori_install_root() / "fake-uv"

    def head(self) -> str:
        repo = self.api._irodori_repo_dir()
        return REAL_RUN(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def run(self, command, **kwargs):
        command = [str(part) for part in command]
        if "rev-parse" in command:  # reading the installed commit
            return REAL_RUN(command, **kwargs)
        if "ls-remote" in command:  # GitHub's newest commit of main
            newest = self.fetch_main_to or self.first
            return subprocess.CompletedProcess(command, 0, stdout=f"{newest}\tHEAD\n", stderr="")
        self.calls.append(command)
        repo = self.api._irodori_repo_dir()
        if command[1:2] == ["clone"]:
            repo.mkdir(parents=True)
            (repo / ".env.example").write_text("# env\n", encoding="utf-8")
            REAL_RUN(["git", "init", "-q", str(repo)], check=True)
            self.first = _commit(repo, "installed")
            self.newer = _commit(repo, "newer upstream")
            REAL_RUN(["git", "-C", str(repo), "reset", "-q", "--hard", self.first], check=True)
            return SimpleNamespace(returncode=0)
        if "fetch" in command:
            if command[-1] == "main" and self.fetch_main_to:
                REAL_RUN(
                    ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", self.fetch_main_to],
                    check=True,
                )
            return SimpleNamespace(returncode=0)
        if "reset" in command:
            return REAL_RUN(["git", "-C", str(repo), "reset", "-q", "--hard", command[-1]])
        if command[0] == str(self.uv):
            self.synced_at.append(self.head())
            python = self.api._irodori_managed_python()
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("python", encoding="utf-8")
            return SimpleNamespace(returncode=0)
        if command[:2] == ["open", "-R"]:
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def irodori(tmp_path, monkeypatch, caplog):
    if not shutil.which("git"):
        pytest.skip("git required")
    # The stand-in's clone has its own commits: installs take main, not the shipped pin.
    monkeypatch.setattr(web_app_module, "IRODORI_TTS_COMMIT", "")
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(web_app_module, "DEFAULT_LOG_PATH", tmp_path / "logs" / "pudge.log")
    logging.getLogger("pudge").propagate = True
    caplog.set_level(logging.INFO)
    stand_in = IrodoriStandIn(api)
    stand_in.uv.parent.mkdir(parents=True, exist_ok=True)
    stand_in.uv.write_text("uv", encoding="utf-8")
    monkeypatch.setattr(api, "_irodori_uv", lambda _log: stand_in.uv)
    monkeypatch.setattr(
        web_app_module.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )
    monkeypatch.setattr(web_app_module.subprocess, "run", stand_in.run)
    monkeypatch.setattr(web_app_module.time, "sleep", lambda _seconds: None)
    api.install_irodori_tts()
    api._irodori_install_thread.join(30)
    assert api.irodori_install_status()["state"] == "ready"
    stand_in.fetch_main_to = stand_in.newer
    stand_in.calls.clear()
    stand_in.synced_at.clear()
    return api, stand_in


class _Health:
    def json(self):
        return {"status": "ok"}


def _server(api, monkeypatch, *, starts: bool) -> list[str]:
    """The managed server: answers /health once started (or exits at once); returns its starts."""
    started: list[str] = []

    def start(url: str) -> bool:
        started.append(url)
        if not starts:
            api._irodori_server_process = SimpleNamespace(poll=lambda: 1, pid=0)
        return True

    def health(url: str, _key: str, timeout: float = 5.0):
        if not started or not starts:
            raise ConnectionError("not running")
        return _Health()

    monkeypatch.setattr(api, "_start_managed_irodori_server", start)
    monkeypatch.setattr(api, "_irodori_health", health)
    return started


def _update_irodori(api) -> dict:
    api.update_irodori_tts()
    api._irodori_install_thread.join(30)
    return api.irodori_install_status()


def test_irodori_install_records_its_commit_as_last_good(irodori) -> None:
    api, stand_in = irodori
    record = api._irodori_last_good()
    assert record["revision"] == stand_in.first == stand_in.head()
    status = api.irodori_install_status()
    assert status["last_good_revision"] == stand_in.first[:8] and status["last_updated_at"] > 0


def test_irodori_update_keeps_main_when_the_updated_server_answers(irodori, monkeypatch, caplog) -> None:
    api, stand_in = irodori
    started = _server(api, monkeypatch, starts=True)
    monkeypatch.setattr(api, "_irodori_remote_revision", lambda **_kwargs: stand_in.newer)
    assert api.irodori_install_status()["update_available"] is True

    status = _update_irodori(api)

    assert [
        "/usr/bin/git",
        "-C",
        str(api._irodori_repo_dir()),
        "reset",
        "--hard",
        "origin/main",
    ] in stand_in.calls
    assert stand_in.head() == stand_in.newer and stand_in.synced_at == [stand_in.newer]
    # The check started this install once, on a port of its own.
    assert len(started) == 1 and started[0].startswith("http://127.0.0.1:")
    assert api._irodori_last_good()["revision"] == stand_in.newer
    assert status["state"] == "ready" and status["update_available"] is False
    assert status["revision"] == status["last_good_revision"] == stand_in.newer[:8]
    assert status["detail"] == web_app_module.t("web.irodoriUpdated", revision=stand_in.newer[:8])
    assert (
        f"RESULT step=irodori.update result=updated revision={stand_in.newer} previous={stand_in.first}"
        in caplog.text
    )


def test_irodori_update_goes_back_to_the_last_good_commit_when_the_server_fails(
    irodori, monkeypatch, caplog
) -> None:
    api, stand_in = irodori
    started = _server(api, monkeypatch, starts=False)

    status = _update_irodori(api)

    assert len(started) == 1
    # The new commit was installed and checked, then the last good one put back with its packages.
    assert stand_in.synced_at == [stand_in.newer, stand_in.first]
    assert stand_in.head() == stand_in.first
    assert api._irodori_last_good()["revision"] == stand_in.first
    assert status["state"] == "ready" and status["installed"] is True
    reason = web_app_module.err("setup.irodoriExited", code=1)
    assert status["detail"] == web_app_module.err(
        "setup.irodoriUpdateKept", revision=stand_in.first[:8], reason=reason
    )
    assert (
        f"FAIL step=irodori.update reason=check_failed revision={stand_in.newer} kept={stand_in.first}"
        in caplog.text
    )


def test_irodori_update_says_when_main_has_nothing_new(irodori, monkeypatch, caplog) -> None:
    api, stand_in = irodori
    started = _server(api, monkeypatch, starts=True)
    stand_in.fetch_main_to = stand_in.first

    status = _update_irodori(api)

    assert started == [] and stand_in.head() == stand_in.first
    assert status["detail"] == web_app_module.t("web.irodoriUpToDate", revision=stand_in.first[:8])
    assert "RESULT step=irodori.update result=current" in caplog.text


@pytest.mark.parametrize("pin", ["", "0123456789abcdef0123456789abcdef01234567"])
def test_irodori_repair_reinstalls_the_last_good_commit(irodori, monkeypatch, pin) -> None:
    api, stand_in = irodori
    monkeypatch.setattr(web_app_module, "IRODORI_TTS_COMMIT", pin)
    _server(api, monkeypatch, starts=True)
    _update_irodori(api)  # main passed its check: the newer commit is last good
    REAL_RUN(["git", "-C", str(api._irodori_repo_dir()), "reset", "-q", "--hard", stand_in.first], check=True)
    api._irodori_managed_python().unlink()  # a broken install
    stand_in.calls.clear()
    stand_in.synced_at.clear()
    assert api.irodori_install_status()["state"] == "not_installed"

    api.update_irodori_tts()  # what Settings call: not installed, so a repair
    api._irodori_install_thread.join(30)

    repo = str(api._irodori_repo_dir())
    assert ["/usr/bin/git", "-C", repo, "reset", "--hard", stand_in.newer] in stand_in.calls
    assert not any("origin/main" in command for command in stand_in.calls)
    assert not any(pin and pin in command for command in stand_in.calls)
    assert stand_in.synced_at == [stand_in.newer]
    assert api.irodori_install_status()["state"] == "ready"


def test_irodori_update_waits_for_a_running_audiobook_generation(irodori) -> None:
    api, _stand_in = irodori

    class Busy:
        def is_alive(self) -> bool:
            return True

    api._irodori_tts_threads[7] = Busy()
    with pytest.raises(RuntimeError, match="audiobook is being generated"):
        api.update_irodori_tts()


def test_settings_update_irodori_through_update_and_repair_through_install(page) -> None:
    installed = {
        "state": "ready",
        "installed": True,
        "update_available": True,
        "revision": "1234abcd",
        "last_checked_at": 1790000000,
        "last_updated_at": 1789000000,
    }
    failed = {
        "state": "failed",
        "installed": True,
        "detail": "uv sync failed",
        "log_path": "/tmp/irodori-install.log",
    }
    result = page.run(
        IRODORI_SETTINGS
        + r"""
        const out = {texts, update: view(), version: __text('#irodoriVersion'), revision: t('ui.irodoriRevision', {revision: '1234abcd'})};
        await __click('#installIrodoriTts'); await __tick(10);
        out.failed = view();
        await __click('#installIrodoriTts'); await __tick(10);
        return out;
        """,
        state=IRODORI_ON,
        before=f"window.STATUS = [{json.dumps(installed)}, {json.dumps(failed)}];"
        + IRODORI_API
        + "__api.update_irodori_tts = async () => ({ok: true});",
    )
    out = result.value
    assert out["update"]["button"] == out["texts"]["update"]
    assert out["version"].startswith(out["revision"] + " · ")
    assert result.calls_of("update_irodori_tts") == [[]]
    # A broken install is repaired with Install (the last good commit).
    assert out["failed"]["button"] == out["texts"]["install"] and out["failed"]["log"] is True
    assert result.calls_of("install_irodori_tts") == [[]]
