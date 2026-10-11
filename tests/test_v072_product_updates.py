"""The in-app updater (release and development channels), Jiten stats for Planning, the
AniList character glossary and manga cover links (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from source_policy import source
from t71g01_support import (
    MacSim,
    Recorder,
    launch_update,
    load_workflow,
    release_history,
    run_step,
    source_history,
    workflow_step,
)

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.updater import AppUpdater, UpdateError, _version_key
from pudge.web_app import WebAppApi


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


def test_update_script_keeps_a_rollback_copy_of_the_app_while_the_installer_runs(
    tmp_path: Path, monkeypatch
) -> None:
    sim = MacSim(tmp_path / "mac")
    (sim.app / "Contents/MacOS").mkdir(parents=True)
    (sim.app / "Contents/MacOS/pudge").write_text("working app\n", encoding="utf-8")
    project = tmp_path / "checkout"
    project.mkdir()
    rollback = sim.base / "updates/rollback/pudge.app.before-update"
    # The installer sees a complete copy of the app it is about to replace.
    (project / "install.sh").write_text(
        "line=\"installer$(printf '\\t')$(cat '" + str(rollback) + "/Contents/MacOS/pudge')\"\n"
        'printf \'%s\\n\' "$line" >> "$MACSIM_LOG"\n',
        encoding="utf-8",
    )
    result = sim.run(launch_update(sim, project, monkeypatch), cwd=sim.base)
    assert result.returncode == 0, (sim.base / "update.log").read_text(encoding="utf-8")
    assert result.calls("ditto") == [[str(sim.app), str(rollback)]]
    assert result.calls("installer") == [["working app"]]
    # A successful update drops the copy and reopens the app; the checkout stays.
    assert not rollback.exists() and project.is_dir()
    assert result.calls("open") == [["-n", str(sim.app)]]


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
                    "links": [{"linkType": 4, "url": "https://anilist.co/anime/154587"}],
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
                                "id": 1234,
                                "name": {
                                    "first": "Kazuto",
                                    "middle": None,
                                    "last": "Kirigaya",
                                    "full": "Kazuto Kirigaya",
                                    "native": "桐ヶ谷 和人",
                                    "alternative": ["キリト"],
                                },
                            }
                        }
                    ]
                }
            }
        },
    )
    glossary = {item["source"]: item["preferred"] for item in service.character_glossary(11757)}
    assert glossary["桐ヶ谷 和人"] == "Kazuto Kirigaya"
    assert glossary["桐ヶ谷"] == "Kirigaya"
    assert glossary["和人"] == "Kazuto"
    assert glossary["キリト"] == "Kazuto Kirigaya"
    rows = service.character_glossary(11757)
    assert all(item.get("character_id") == 1234 for item in rows)


def test_settings_check_and_install_an_update_and_follow_its_progress(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(200);
        await __click('[data-settings-category="advanced"]'); await __tick(10);
        const view = () => ({status: __text('#appUpdateState'), install: __visible('#installAppUpdate'), check: $('checkAppUpdate').disabled});
        const out = {start: view()};
        await __click('#checkAppUpdate'); await __tick(10); out.checked = view();
        await __click('#installAppUpdate'); await __tick(10); out.installing = view();
        await __tick(760); out.later = view();
        await __tick(760); out.restarting = view();
        await __tick(3000); out.polls = __apiCalls.filter(c => c.name === 'app_update_progress').length;
        out.texts = {available: t('update.available', {version: '9.9.9'}), downloading: t('update.downloading'),
                     installing: t('update.installing'), restarting: t('update.restarting')};
        return out;
        """,
        before=r"""
        window.PROGRESS = [{state: 'downloading', running: true}, {state: 'installing', running: true}, {state: 'restarting', running: false}];
        __api.app_update_status = async () => ({state: 'idle', available: true, latest_version: '9.9.9', channel: 'release'});
        __api.app_update_install = async () => ({state: 'preparing', running: true});
        __api.app_update_progress = async () => window.PROGRESS.shift();
        """,
    )
    out, texts = result.value, result.value["texts"]
    assert out["start"] == {"status": "", "install": False, "check": False}
    assert out["checked"] == {"status": texts["available"], "install": True, "check": False}
    assert result.calls_of("app_update_status") == [[True]] and result.calls_of("app_update_install") == [[]]
    # While it runs the window polls progress (and the buttons are locked) until Pudge restarts.
    assert out["installing"] == {"status": texts["downloading"], "install": False, "check": True}
    assert (out["later"]["status"], out["restarting"]["status"]) == (texts["installing"], texts["restarting"])
    assert out["polls"] == 3
    # The window's update calls are the updater's answers.
    api = WebAppApi.__new__(WebAppApi)
    api.app_updater = SimpleNamespace(
        state=lambda: {"state": "downloading"},
        start=lambda: {"state": "preparing"},
        check=lambda force: {"forced": force},
    )
    assert (api.app_update_progress(), api.app_update_install(), api.app_update_status(True)) == (
        {"state": "downloading"},
        {"state": "preparing"},
        {"forced": True},
    )


def test_finished_planning_titles_show_jiten_stats_when_they_come_into_view(
    page, tmp_path: Path, monkeypatch
) -> None:
    anime = {
        "media_id": 154587,
        "title": "Frieren",
        "titles": ["Sousou no Frieren"],
        "synonyms": ["葬送のフリーレン"],
        "format": "TV",
        "media_status": "FINISHED",
        "media_kind": "anime",
        "site_url": "https://anilist.co/anime/154587",
    }
    stats = {
        "available": True,
        "difficulty": 3,
        "speech_duration_ms": 7_200_000,
        "word_count": 50000,
        "unique_word_count": 7100,
        "coverage_available": True,
        "known_coverage": 87.25,
        "url": "https://jiten.moe/decks/media/42/detail",
    }
    result = page.run(
        r"""
        setPage('planned'); await __tick(200);
        const before = __apiCalls.filter(c => c.name === 'planning_jiten_stats').length;
        for (const o of __observers.intersection.filter(o => !o.disconnected && o.targets.some(n => n.matches('[data-planned-jiten]'))))
          o.__fire(o.targets.filter(n => n.matches('[data-planned-jiten]')).map(target => ({target})));
        await __tick(50);
        return {before, chips: [...document.querySelectorAll('.planned-jiten .planned-jiten-chip')].map(n => n.textContent),
                expected: [t('uiA.jiten.difficulty', {level: t('uiA.jiten.level3')}), t('uiA.jiten.speech', {duration: jitenDuration(7200000)}),
                           t('uiA.jiten.known', {percent: '87.3'})],
                link: document.querySelector('.planned-jiten [data-action="url"]')?.dataset.url};
        """,
        state={"planned": [anime]},
        api={"planning_jiten_stats": stats},
    )
    out = result.value
    assert out["before"] == 0  # nothing is fetched for cards out of view
    assert result.calls_of("planning_jiten_stats") == [
        [154587, "anime", "TV", ["Frieren", "Sousou no Frieren", "葬送のフリーレン"]]
    ]
    assert out["chips"] == out["expected"] and out["link"] == stats["url"]
    # The backend call the card makes exists and answers from the Light Novel service.
    api = WebAppApi.__new__(WebAppApi)
    seen: list[tuple] = []
    api.light_novels = SimpleNamespace(
        jiten_media_stats=lambda *args, **kwargs: seen.append(args) or {"available": False}
    )
    assert api.planning_jiten_stats(154587, "anime", "TV", ["Frieren"]) == {"available": False}
    assert seen and seen[0][:2] == (154587, "anime")


def test_manga_cover_opens_its_anilist_page(page) -> None:
    result = page.run(
        r"""
        setPage('manga'); await __tick(300);
        await __click('[data-cover-placeholder="21"][data-manga-v2-action="anilist"]'); await __tick(20);
        return document.querySelectorAll('[data-cover-placeholder="22"][data-manga-v2-action]').length;
        """,
        api={
            "manga_state": {
                "books": [
                    {
                        "id": 21,
                        "title": "Linked",
                        "series_title": "Linked",
                        "page_count": 4,
                        "anilist_id": 30013,
                    },
                    {"id": 22, "title": "Unlinked", "series_title": "Unlinked", "page_count": 4},
                ],
                "ocr_available": False,
            }
        },
    )
    assert result.calls_of("open_url") == [["https://anilist.co/manga/30013"]]
    assert result.value == 0  # a volume without AniList has no link on its cover


def test_installs_record_their_channel_for_the_updater() -> None:
    # The updater offers GitHub releases to release installs and fast-forwards to clean
    # development checkouts; install.sh tells it which one this is.
    release = release_history().steps["update"].records["install-source.json"]
    assert json.loads(release) == {"channel": "release", "source_path": "", "source_revision": ""}
    history = source_history()
    checkout = history.projects["checkout"]
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    development = json.loads(history.steps["install"].records["install-source.json"])
    assert development == {"channel": "development", "source_path": str(checkout), "source_revision": head}


def test_release_publishes_the_archive_with_its_checksum(tmp_path: Path) -> None:
    # policy: the workflow file is configuration for GitHub; its steps are run here.
    workflow = load_workflow(source(".github/workflows/release.yml"))
    locate = workflow_step(workflow, "build-release", "Locate archive")
    dist = tmp_path / "dist"
    dist.mkdir()
    # A stray X.Y.Z.zip (the 0.7.30 layout) is not what gets published.
    for name in ("9.8.7.zip", "pudge-macos-v9.8.7.zip"):
        (dist / name).write_bytes(b"zip")
        (dist / f"{name}.sha256").write_text("00  " + name + "\n")
    found = run_step(locate, tmp_path, tools=tmp_path, env={"GITHUB_OUTPUT": str(tmp_path / "outputs")})
    assert found.returncode == 0, found.stderr
    assert dict(line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines()) == {
        "path": "dist/pudge-macos-v9.8.7.zip",
        "checksum": "dist/pudge-macos-v9.8.7.zip.sha256",
    }
    # Without the checksum the release stops (the updater verifies downloads with it).
    (dist / "pudge-macos-v9.8.7.zip.sha256").unlink()
    assert (
        run_step(locate, tmp_path, tools=tmp_path, env={"GITHUB_OUTPUT": str(tmp_path / "again")}).returncode
        != 0
    )
    # The published release carries the archive and its checksum, nothing else.
    calls = Recorder(tmp_path / "gh")
    calls.tool("gh", '[ "$2" != view ] || [ -n "$EXISTS" ]\n')
    publish = workflow_step(workflow, "build-release", "Publish GitHub Release")
    values = {
        "github.token": "token",
        "steps.archive.outputs.path": "dist/pudge-macos-v9.8.7.zip",
        "steps.archive.outputs.checksum": "dist/pudge-macos-v9.8.7.zip.sha256",
        "github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name": "v9.8.7",
    }
    created = run_step(publish, tmp_path, tools=calls.bin, expressions=values, env=calls.env)
    assert created.returncode == 0, created.stderr
    [view, create] = calls.calls("gh")
    assert view == ["release", "view", "v9.8.7"]
    assert create[:6] == [
        "release",
        "create",
        "v9.8.7",
        "dist/pudge-macos-v9.8.7.zip",
        "dist/pudge-macos-v9.8.7.zip.sha256",
        "--title",
    ]
