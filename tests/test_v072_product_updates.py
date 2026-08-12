from __future__ import annotations

import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.updater import AppUpdater, UpdateError, _version_key


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return config


def test_release_update_check_and_version_order(monkeypatch: pytest.MonkeyPatch) -> None:
    updater = AppUpdater()
    monkeypatch.setattr(updater, "_source", lambda: {"channel": "release"})
    monkeypatch.setattr(
        updater,
        "_latest_release",
        lambda **_kwargs: {
            "version": "99.1.0",
            "html_url": "https://github.com/TH1NKFASTER/pudge/releases/tag/v99.1.0",
        },
    )
    result = updater.check()
    assert result["channel"] == "release"
    assert result["available"] is True
    assert _version_key("v0.7.10") > _version_key("0.7.2")


def test_running_update_status_does_not_reenter_lock() -> None:
    updater = AppUpdater()

    class RunningThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    updater._thread = RunningThread()  # type: ignore[assignment]
    result: list[dict[str, object]] = []
    thread = threading.Thread(target=lambda: result.append(updater.start()))
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert result[0]["running"] is True


def test_development_update_blocks_detached_dirty_and_nonofficial_origins(
    tmp_path: Path,
) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Pudge Tests"],
        check=True,
    )
    tracked = source / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", "https://github.com/elsewhere/pudge.git"],
        check=True,
    )
    updater = AppUpdater()
    status = updater._development_status({"source_path": str(source)})
    assert status["blocked"] is True
    assert "official" in status["detail"]

    subprocess.run(
        ["git", "-C", str(source), "remote", "set-url", "origin", "git@github.com:TH1NKFASTER/pudge.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "checkout", "--detach", "-q"], check=True)
    status = updater._development_status({"source_path": str(source)})
    assert "detached HEAD" in status["detail"]

    subprocess.run(["git", "-C", str(source), "switch", "-q", "-"], check=True)
    tracked.write_text("dirty\n", encoding="utf-8")
    status = updater._development_status({"source_path": str(source)})
    assert "local changes" in status["detail"]


def test_development_update_reinstalls_an_uninstalled_source_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = AppUpdater()
    source = tmp_path / "checkout"
    source.mkdir()

    def fake_git(_source: Path, *args: str, **_kwargs) -> str:
        if args[0] == "status" or args[0] == "fetch":
            return ""
        if args[:2] == ("rev-parse", "HEAD") or args[:2] == ("rev-parse", "FETCH_HEAD"):
            return "new-revision"
        raise AssertionError(args)

    def fake_optional(_source: Path, *args: str, **_kwargs) -> str:
        if args[0] == "symbolic-ref":
            return "main"
        if args[0] == "remote":
            return "https://github.com/TH1NKFASTER/pudge.git"
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "_git_optional", fake_optional)
    result = updater._development_status(
        {"source_path": str(source), "source_revision": "installed-revision"}
    )
    assert result["available"] is True
    assert result["source_revision"] == "new-revision"
    assert result["remote_revision"] == "new-revision"


def test_development_install_merges_the_checked_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = AppUpdater()
    launched: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(
        updater,
        "_development_status",
        lambda _source: {
            "available": True,
            "blocked": False,
            "branch": "main",
            "source_revision": "a" * 40,
            "remote_revision": "b" * 40,
        },
    )
    monkeypatch.setattr(updater, "_launch_script", lambda path, command: launched.append((path, command)))
    updater._development_worker({"source_path": str(tmp_path), "channel": "development"})
    assert launched == [
        (tmp_path.resolve(), ["git", "-C", str(tmp_path.resolve()), "merge", "--ff-only", "b" * 40])
    ]


def test_release_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "unsafe")
    with pytest.raises(UpdateError, match="unsafe path"):
        AppUpdater._extract_archive(archive, tmp_path / "extract", "0.7.2")


def test_update_installer_script_keeps_a_rollback_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    (project / "install.sh").write_text("#!/bin/zsh\n", encoding="utf-8")
    updater = AppUpdater()
    updater.update_root = tmp_path / "updates"
    updater.log_path = tmp_path / "update.log"
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: object())
    updater._launch_script(project, [])
    script = (tmp_path / "run-pudge-update.zsh").read_text(encoding="utf-8")
    assert ".app.before-update" in script
    assert "/usr/bin/ditto" in script
    assert "if ! /bin/zsh ./install.sh --update; then" in script
    assert "/bin/mv" in script


def test_jiten_finished_media_uses_exact_anilist_link_and_private_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LightNovelService(_config(tmp_path))
    service.save_settings({"jiten_api_key": "configured-token"})
    calls: list[str] = []

    def fake_get(action: str, _params=None):
        calls.append(action)
        if action.endswith("/detail"):
            return {
                "deckId": 42,
                "characterCount": 123456,
                "wordCount": 50000,
                "uniqueWordCount": 7100,
                "difficulty": 3,
                "difficultyRaw": 3.4,
                "speechDuration": 7_200_000,
                "coverage": 87.25,
                "youngCoverage": 91.0,
            }
        return {
            "data": [
                {
                    "deckId": 42,
                    "originalTitle": "葬送のフリーレン",
                    "links": [
                        {"linkType": 4, "url": "https://anilist.co/anime/154587"}
                    ],
                }
            ]
        }

    monkeypatch.setattr(service, "_jiten_get", fake_get)
    result = service.jiten_media_stats(
        154587,
        "anime",
        "TV",
        ["Frieren: Beyond Journey's End", "葬送のフリーレン"],
    )
    assert result["available"] is True
    assert result["deck_id"] == 42
    assert result["difficulty"] == 3
    assert result["speech_duration_ms"] == 7_200_000
    assert result["coverage_available"] is True
    assert result["known_coverage"] == 87.25
    assert calls[-1] == "media-deck/42/detail"


def test_anilist_character_glossary_includes_unambiguous_short_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.anilist.enabled = True
    config.anilist.access_token = "token"
    service = LightNovelService(config)
    monkeypatch.setattr(
        service,
        "_anilist_post",
        lambda *_args, **_kwargs: {
            "Media": {
                "characters": {
                    "edges": [
                        {
                            "node": {
                                "name": {
                                    "first": "Kazuto",
                                    "middle": None,
                                    "last": "Kirigaya",
                                    "full": "Kazuto Kirigaya",
                                    "native": "桐ヶ谷 和人",
                                    "alternative": ["キリト"],
                                }
                            }
                        }
                    ]
                }
            }
        },
    )
    glossary = {
        item["source"]: item["preferred"] for item in service.character_glossary(11757)
    }
    assert glossary["桐ヶ谷 和人"] == "Kazuto Kirigaya"
    assert glossary["桐ヶ谷"] == "Kirigaya"
    assert glossary["和人"] == "Kazuto"
    assert glossary["キリト"] == "Kazuto Kirigaya"


def test_update_jiten_and_manga_cover_frontend_contracts() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    installer = (root / "install.sh").read_text(encoding="utf-8")
    release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'id="checkAppUpdate"' in html
    assert "app_update_install" in html and "app_update_progress" in app
    assert "planning_jiten_stats" in html and "planning_jiten_stats" in app
    assert 'data-manga-v2-action="anilist"' in manga
    assert "install-source.json" in installer
    assert "source_revision" in installer
    assert '"development"' in installer and '"release"' in installer
    assert ".sha256" in release
