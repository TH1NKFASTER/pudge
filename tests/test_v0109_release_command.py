from pathlib import Path

ROOT = Path(__file__).parents[1]
MAKEFILE = ROOT / "Makefile"
RELEASE = ROOT / ".github" / "release" / "release.py"


def test_make_release_requires_version_and_uses_release_helper() -> None:
    source = MAKEFILE.read_text(encoding="utf-8")
    assert 'Usage: make release VERSION=' in source
    assert '.github/release/release.py "$(VERSION)" --python "$(PYTHON)"' in source
    assert ".venv-test/bin/python" in source.splitlines()[0]
    assert "build-release:" in source


def test_release_helper_has_full_safe_release_pipeline() -> None:
    source = RELEASE.read_text(encoding="utf-8")
    assert "git add -A" not in source
    assert "ensure_safe_release_start(version)" in source
    for contract in (
        '"fetch", "origin", "main", "--tags"',
        '"merge-base", "--is-ancestor", "origin/main", "HEAD"',
        'run(python, ".github/release/bump_version.py", version)',
        'run("make", "quality", f"PYTHON={python}")',
        'run("make", "test-batches", f"PYTHON={python}")',
        'git("diff", "--check")',
        'git("add", "--", *RELEASE_FILES)',
        'git("diff", "--cached", "--check")',
        'run("git", "--no-pager", "diff", "--cached", "--stat")',
        'git("commit", "-m", f"Pudge {version}")',
        'git("push", "origin", "main")',
        'git("tag", tag)',
        'git("push", "origin", tag)',
    ):
        assert contract in source


# Published tags must now be rejected, including the old v-prefixed form.
# The real local/bare-remote scenarios live in test_release_v0730_regressions.
