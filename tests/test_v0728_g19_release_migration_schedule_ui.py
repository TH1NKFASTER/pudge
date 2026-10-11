"""Personal-schedule cards, the release bundle, the release quality gate and the legacy
migrator (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from source_policy import source
from t71g01_support import Recorder, build_release, load_workflow, release_history, run_step, workflow_step

ROOT = Path(__file__).parents[1]
NOW = 1_790_856_000  # the page's clock: 2026-10-01 12:00 UTC


def test_schedule_cards_show_the_next_episode_time_not_the_custom_cadence(page) -> None:
    anime = {
        "media_id": 5,
        "title": "Show",
        "site_url": "https://anilist.co/anime/5",
        "media_status": "RELEASING",
        "personal_schedule": {
            "enabled": True,
            "eligible": True,
            "status": "caught_up",
            "next_episode": 4,
            "next_unlock_at": NOW + 3 * 3600 + 60,
            "rule": {"unit": "days", "every": 3},
        },
    }
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('current'); renderCurrent(); await __tick(50);
            const out = {card: __text('.caught-up-card .airing-state')};
            await __click('.caught-up-card [data-action="card-menu"]'); await __tick(10);
            out.menu = __text('#contextMenu');
            out.expected = t('label.airingIn', {episode: 4, time: formatRemaining(3 * 3600 + 60)});
            out.cadence = [personalScheduleRuleLabel({unit: 'days', every: 3}), personalScheduleRuleLabel({unit: 'days', every: 3}, true)];
            out.actions = [PudgeI18n.t('ui.editSchedule'), PudgeI18n.t('ui.disableSchedule')];
            return out;
            """,
            lang=lang,
            state={"current": [anime], "home": {"caught_up": [anime]}},
        )
        out = result.value
        assert out["card"] == out["expected"]
        assert all(action in out["menu"].splitlines() for action in out["actions"])
        assert not any(label in out["card"] + out["menu"] for label in out["cadence"])


def test_release_bundle_contains_the_documented_developer_paths(tmp_path: Path) -> None:
    build = build_release(tmp_path / "ok")
    assert build.result.returncode == 0, build.result.output
    shipped = build.shipped()
    for path in (
        "pudge/__init__.py",
        "tests/test_example.py",
        "Makefile",
        ".github/workflows/release.yml",
        ".github/release/check_release_bundle.py",
        "install.sh",
        "build_release.sh",
        "pudge-9.8.7-py3-none-any.whl",
    ):
        assert path in shipped, path
    # Not part of a release: the mobile protocol notes and the old root-level renamers.
    assert not {"MOBILE_SYNC_PROTOCOL.md", "rename_brand.py", "brand_migration.py"} & set(shipped)
    assert not [path for path in shipped if "__pycache__" in path]
    # The bundle check runs on every build: a doc linking to a file the release lacks stops it.
    broken = build_release(tmp_path / "broken", readme="Read [the protocol](MOBILE_SYNC_PROTOCOL.md).\n")
    assert broken.result.returncode != 0
    assert "README.md: broken local link: MOBILE_SYNC_PROTOCOL.md" in broken.result.output
    assert not broken.archive.exists()


def test_release_ci_runs_the_same_quality_gate_as_a_local_release(tmp_path: Path, monkeypatch) -> None:
    # policy: the workflow file is configuration for GitHub; its job graph is read, its steps run.
    workflow = load_workflow(source(".github/workflows/release.yml"))
    assert workflow["jobs"]["quality"]["name"] == "Quality"
    assert workflow["jobs"]["build-release"]["needs"] == ["quality", "verify-and-test"]
    ci = Recorder(tmp_path / "ci")
    ci.tool("make")
    completed = run_step(
        workflow_step(workflow, "quality", "Run quality gates"), tmp_path, tools=ci.bin, env=ci.env
    )
    assert completed.returncode == 0, completed.stderr
    ci_gate = ci.calls("make")
    # The local release script (make release) validates with the same target.
    spec = importlib.util.spec_from_file_location(
        "t71g01_release_script", ROOT / ".github/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)  # type: ignore[union-attr]
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "run", lambda *args, **_kw: commands.append(args))
    monkeypatch.setattr(release, "git", lambda *args, **_kw: commands.append(("git", *args)))
    release.validate("python")
    assert ci_gate == [["quality", "PYTHON=python"]]
    assert ("make", "quality", "PYTHON=python") in commands


def test_legacy_migrator_is_a_packaged_step_of_the_installer() -> None:
    assert not (ROOT / "brand_migration.py").exists()
    assert not (ROOT / "rename_brand.py").exists()
    history = release_history()
    project, install = history.projects["9.8.6"], history.steps["install"].result
    migrator = str(project / "pudge/legacy_install.py")
    names = [
        "--app-name",
        "pudge",
        "--app-slug",
        "pudge",
        "--legacy-names",
        "Anime MPV",
        "--legacy-slugs",
        "anime-mpv",
    ]
    # Old-brand folders move before anything reads them; old default paths in the config are
    # rewritten once the config exists.
    assert [migrator, "paths", *names] in install.calls("python3.12")
    assert [migrator, "config", *names, "--config", str(history.sim.config)] in install.calls("python3.12")
    assert install.index("python3.12", migrator, "paths") < install.index("python3.12", "-m", "venv")
    assert install.index("pudge-cli", "--init-config") < install.index("python3.12", migrator, "config")
