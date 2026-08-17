from __future__ import annotations

from pathlib import Path


def test_home_download_status_is_external_module() -> None:
    index = Path("pudge/web/index.html").read_text(encoding="utf-8")
    module = Path("pudge/web/home_status.js").read_text(encoding="utf-8")

    assert '<script src="home_status.js"></script>' in index
    assert '<script src="library.js"></script>' in index
    assert "function compactDownloadStatus(download){" not in index
    assert "function libraryCard(a){" not in index
    assert "function libraryCard(a){" in Path("pudge/web/library.js").read_text(encoding="utf-8")
    assert "function compactDownloadStatus(download){" in module
    assert "function episodePresentationStatus(a,episode){" in module
    assert "episodePresentationStatus(a,episode)+finalSuffix" in index


def test_ci_checks_all_python_and_javascript() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "ruff check --select E9,F pudge scripts" in workflow
    assert "find pudge/web -type f -name '*.js'" in workflow
    assert "ruff check --select E9,F pudge scripts" in makefile
    assert "find pudge/web -type f -name '*.js'" in makefile


def test_release_uses_locked_hashed_dependencies() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build = Path("build_release.sh").read_text(encoding="utf-8")
    install = Path("install.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "[tool.uv]" in pyproject
    assert "required-environments" in pyproject
    assert "uv lock" in build
    assert "uv export" in build
    assert "release-requirements.txt" in build
    assert "release-requirements.txt" in install
    assert "--require-hashes" in install
    assert " uv" in workflow or "uv " in workflow
