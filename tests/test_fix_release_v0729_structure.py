"""Releases are published as v0.7.29 was, not as 0.7.30 (behaviour).

v0.7.29: commit `Pudge v0.7.29`, lightweight tag `v0.7.29`, GitHub Release `Pudge v0.7.29` with
exactly `pudge-macos-v0.7.29.zip` and `pudge-macos-v0.7.29.zip.sha256`, notes = its CHANGELOG
section. 0.7.30 was committed as `Pudge 0.7.30`, tagged `0.7.30` and shipped `0.7.30.zip` (plus a
pudge-macos-v copy). release.py runs in a throwaway checkout (tests/test_v0109_release_command.py),
build_release.sh in a small project and the workflow steps with stand-ins (tests/t71g01_support.py),
and the in-app updater installs from what the workflow published.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from source_policy import source
from t71g01_support import Recorder, build_release, load_workflow, run_step, workflow_step
from test_v0109_release_command import _checkout, _git, _release

from pudge.updater import AppUpdater

ARCHIVE = "pudge-macos-v9.8.7.zip"
CHANGELOG = (
    "# Changes\n\n## Unreleased\n\n## v9.8.7 — Same shape as v0.7.29\n\n- First line\n- Second line\n\n"
    "## v9.8.6\n\n- Older\n"
)


def _after_0730(tmp_path: Path):
    """main at 0.7.30 as it was published (`Pudge 0.7.30`, tag `0.7.30`), v0.7.29 before it."""
    repo, origin, module = _checkout(tmp_path)
    (repo / "pudge/__init__.py").write_text('__version__ = "0.7.30"\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "pudge"\nversion = "0.7.30"\n')
    (repo / "README.md").write_text(
        "Current version: **0.7.30**.\n\nDownload `pudge-macos-vX.Y.Z.zip`, then:\n\nunzip 0.7.30.zip\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changes\n\n## Unreleased\n\n- Newer things\n\n## v0.7.30\n\n- 0.7.30 things\n\n"
        "## v0.7.29\n\n- Old things\n"
    )
    _git(repo, "commit", "-qam", "Pudge 0.7.30")
    _git(repo, "tag", "0.7.30")
    _git(repo, "push", "-q", "origin", "main", "--tags")
    return repo, origin, module


def test_release_commits_pudge_v_and_pushes_a_lightweight_v_tag(tmp_path: Path, monkeypatch) -> None:
    repo, origin, module = _after_0730(tmp_path)
    _release(monkeypatch, module, repo, "0.8.0")
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "log", "-1", "--pretty=%s") == "Pudge v0.8.0"
    # Tag vX.Y.Z at the release commit, lightweight (as v0.7.29), on origin; no plain X.Y.Z tag.
    assert _git(origin, "rev-parse", "main") == head
    assert sorted(_git(origin, "tag", "--list").split()) == ["0.7.30", "v0.7.29", "v0.8.0"]
    assert _git(origin, "cat-file", "-t", "v0.8.0") == "commit"
    assert _git(origin, "rev-parse", "v0.8.0") == head
    # The README's install command names the published archive.
    readme = (repo / "README.md").read_text()
    assert "Current version: **0.8.0**" in readme
    assert "Download `pudge-macos-vX.Y.Z.zip`" in readme
    assert "unzip pudge-macos-v0.8.0.zip\n" in readme and "unzip 0.8.0.zip" not in readme


@pytest.mark.parametrize("version", ["0.7.30", "0.7.29", "0.7.28"])
def test_release_refuses_versions_tagged_with_or_without_v(tmp_path: Path, monkeypatch, version) -> None:
    # 0.7.30 is tagged without the v, 0.7.29 with it; both count as published.
    repo, _origin, module = _after_0730(tmp_path)
    calls: list = []
    with pytest.raises(module.ReleaseError, match="already exists|must be newer than 0.7.30"):
        _release(monkeypatch, module, repo, version, calls=calls)
    assert [call for call in calls if call[0] != "git"] == []


def test_a_half_done_release_resumes_from_its_commit(tmp_path: Path, monkeypatch) -> None:
    repo, origin, module = _after_0730(tmp_path)
    _release(monkeypatch, module, repo, "0.8.0", "--no-push")
    commit = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "log", "-1", "--pretty=%s") == "Pudge v0.8.0"
    assert "v0.8.0" not in _git(origin, "tag", "--list").split()
    # Running it again (HEAD is already the release commit) publishes that commit.
    calls = _release(monkeypatch, module, repo, "0.8.0")
    assert not [call for call in calls if call[:2] == ("git", "commit")]
    assert _git(repo, "rev-parse", "HEAD") == commit
    assert _git(origin, "rev-parse", "v0.8.0") == commit == _git(origin, "rev-parse", "main")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    build = build_release(tmp_path_factory.mktemp("build"))
    assert build.result.returncode == 0, build.result.output
    (build.project / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    return build


def test_build_makes_only_the_macos_v_archive_and_its_portable_checksum(built) -> None:
    dist = built.project / "dist"
    assert sorted(path.name for path in dist.iterdir() if path.is_file() and ".zip" in path.name) == [
        ARCHIVE,
        ARCHIVE + ".sha256",
    ]
    digest = hashlib.sha256((dist / ARCHIVE).read_bytes()).hexdigest()
    assert (dist / f"{ARCHIVE}.sha256").read_text(encoding="utf-8") == f"{digest}  {ARCHIVE}\n"
    assert built.result.completed.stdout.strip().splitlines()[-1] == str(dist / ARCHIVE)
    assert "install.sh" in built.shipped()


def _publish(built, tmp_path: Path, tag: str, *, exists: bool) -> tuple[dict[str, str], list[list[str]]]:
    """Runs the release job's Locate archive, notes and Publish steps on the build's dist;
    returns (archive outputs, gh calls)."""
    # policy: the workflow file is configuration for GitHub; its steps are run here.
    workflow = load_workflow(source(".github/workflows/release.yml"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    outputs = tmp_path / "outputs"
    located = run_step(
        workflow_step(workflow, "build-release", "Locate archive"),
        built.project,
        tools=tmp_path,
        env={"GITHUB_OUTPUT": str(outputs)},
    )
    assert located.returncode == 0, located.stderr
    found = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    calls = Recorder(tmp_path / "gh")
    calls.tool("python", f'exec "{sys.executable}" "$@"\n')
    calls.tool("gh", '[ "$2" != view ] || [ -n "$EXISTS" ]\n')
    values = {
        "github.token": "token",
        "github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name": tag,
        **{f"steps.archive.outputs.{name}": value for name, value in found.items()},
    }
    for name in ("Extract release notes from changelog", "Publish GitHub Release"):
        done = run_step(
            workflow_step(workflow, "build-release", name),
            built.project,
            tools=calls.bin,
            expressions=values,
            env={**calls.env, **({"EXISTS": "1"} if exists else {})},
        )
        assert done.returncode == 0, done.stderr
    # The workflow artifact holds the same two files.
    upload = workflow_step(workflow, "build-release", "Upload workflow artifact")
    artifact = re.sub(
        r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values[match.group(1)], upload["with"]["path"]
    ).split()
    assert artifact == [found["path"], found["checksum"]]
    return found, calls.calls("gh")


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("tag", ["v9.8.7", "9.8.7"])
def test_workflow_publishes_exactly_the_archive_and_checksum(built, tmp_path: Path, tag, exists) -> None:
    found, gh = _publish(built, tmp_path, tag, exists=exists)
    assert found == {"path": f"dist/{ARCHIVE}", "checksum": f"dist/{ARCHIVE}.sha256"}
    notes = ["--notes-file", "dist/release-notes.md"]
    if exists:
        assert gh == [
            ["release", "view", tag],
            ["release", "upload", tag, f"dist/{ARCHIVE}", f"dist/{ARCHIVE}.sha256", "--clobber"],
            ["release", "edit", tag, "--title", "Pudge v9.8.7", *notes],
        ]
    else:
        assert gh == [
            ["release", "view", tag],
            [
                "release",
                "create",
                tag,
                f"dist/{ARCHIVE}",
                f"dist/{ARCHIVE}.sha256",
                "--title",
                "Pudge v9.8.7",
                *notes,
            ],
        ]
    # The body is the CHANGELOG section without its heading.
    body = (built.project / "dist/release-notes.md").read_text(encoding="utf-8")
    assert body == "- First line\n- Second line\n"


class _Served:
    def __init__(self, body: bytes) -> None:
        self.body, self.headers = body, {"Content-Length": str(len(body))}

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)

    def raise_for_status(self) -> None:
        pass

    def iter_bytes(self):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_updater_installs_the_release_the_workflow_published(built, tmp_path: Path, monkeypatch) -> None:
    _found, gh = _publish(built, tmp_path / "publish", "v9.8.7", exists=False)
    [create] = [call for call in gh if call[:2] == ["release", "create"]]
    published = create[3 : create.index("--title")]
    files = {Path(path).name: (built.project / path).read_bytes() for path in published}
    base = "https://github.com/TH1NKFASTER/pudge/releases/download/v9.8.7/"
    release = {
        "tag_name": "v9.8.7",
        "assets": [{"name": name, "browser_download_url": base + name} for name in files],
    }

    def serve(url: str) -> _Served:
        if url.endswith("/releases/latest"):
            return _Served(json.dumps(release).encode())
        return _Served(files[url.removeprefix(base)])

    downloads: list[str] = []
    monkeypatch.setattr("pudge.updater.httpx.get", lambda url, **_kw: downloads.append(url) or serve(url))
    monkeypatch.setattr(
        "pudge.updater.httpx.stream", lambda _method, url, **_kw: downloads.append(url) or serve(url)
    )
    updater = AppUpdater()
    updater.update_root, updater.log_path = tmp_path / "updates", tmp_path / "update.log"
    launched: list[Path] = []
    monkeypatch.setattr(updater, "_launch_script", lambda project, _command: launched.append(project))
    updater._release_worker({"channel": "release"})
    assert updater._state["state"] != "failed", updater._state
    # It downloads the macos-v archive, checks it against the .sha256 file and installs it.
    assert [url.removeprefix(base) for url in downloads if url.startswith(base)] == [
        ARCHIVE + ".sha256",
        ARCHIVE,
    ]
    [project] = launched
    assert (project / "install.sh").is_file()
    assert 'version = "9.8.7"' in (project / "pyproject.toml").read_text(encoding="utf-8")


def test_release_tag_check_accepts_both_tag_forms(tmp_path: Path) -> None:
    # Manual re-runs name an existing tag: v0.7.29-style or 0.7.30-style.
    project = tmp_path / "project"
    (project / "pudge").mkdir(parents=True)
    (project / ".github/release").mkdir(parents=True)
    (project / "pudge/__init__.py").write_text('__version__ = "9.8.7"\n')
    (project / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n')
    shutil.copy2(
        Path(__file__).resolve().parents[1] / ".github/release/check_release_tag.py",
        project / ".github/release/check_release_tag.py",
    )
    for tag, status in (("v9.8.7", 0), ("9.8.7", 0), ("v9.8.6", 1)):
        done = subprocess.run(
            [sys.executable, str(project / ".github/release/check_release_tag.py"), tag],
            capture_output=True,
            text=True,
        )
        assert (done.returncode != 0) == bool(status), (tag, done.stdout, done.stderr)
