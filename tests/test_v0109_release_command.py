from pathlib import Path

ROOT = Path(__file__).parents[1]
MAKEFILE = ROOT / "Makefile"
RELEASE = ROOT / "scripts" / "release.py"


def test_make_release_requires_version_and_uses_release_helper() -> None:
    source = MAKEFILE.read_text(encoding="utf-8")
    assert 'Usage: make release VERSION=0.7.21' in source
    assert 'scripts/release.py "$(VERSION)" --python "$(PYTHON)"' in source
    assert "build-release:" in source


def test_release_helper_has_full_safe_release_pipeline() -> None:
    source = RELEASE.read_text(encoding="utf-8")
    for contract in (
        '"fetch", "origin", "main", "--tags"',
        '"merge-base", "--is-ancestor", "origin/main", "HEAD"',
        'run(python, "scripts/bump_version.py", version)',
        'run("make", "lint", f"PYTHON={python}")',
        'run("make", "test-batches", f"PYTHON={python}")',
        'git("diff", "--check")',
        'git("add", "-A")',
        'git("diff", "--cached", "--check")',
        'run("git", "--no-pager", "diff", "--cached", "--stat")',
        'git("commit", "-m", f"Pudge v{version}")',
        'git("push", "origin", "main")',
        'git("tag", tag)',
        'git("push", "origin", tag)',
    ):
        assert contract in source


def test_release_helper_is_resume_safe_around_tags() -> None:
    source = RELEASE.read_text(encoding="utf-8")
    assert "local_tag_sha" in source
    assert "remote_tag_sha" in source
    assert "already published at HEAD; nothing to do" in source
    assert "remote tag {tag} already exists" in source
