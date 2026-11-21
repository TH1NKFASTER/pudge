"""The source release holds the repository's files, not a developer folder's leftovers
(review of J, ARCH-5).

Tests now ask git what the repository holds, so an untracked ``pudge/web/companion/``
no longer fails them; ``cp -R pudge`` would still have put it into the release ZIP. In a
checkout the release now takes the files git tracks (plus the two the build writes);
built from a plain folder, the bundle check refuses paths Pudge removed.
"""

from __future__ import annotations

from pathlib import Path

from t71g01_support import build_release

LEFTOVERS = {
    "pudge/web/companion/companion.js": "old\n",
    "pudge/old_module.py": "x = 1\n",
    "tests/golden/page.txt": "private\n",
}


def test_a_checkout_ships_only_what_git_tracks(tmp_path: Path) -> None:
    build = build_release(
        tmp_path / "git",
        git=True,
        untracked=LEFTOVERS,
        env={"PUDGE_TRIAL_JIMAKU_API_KEY": "trial-123"},
    )
    assert build.result.returncode == 0, build.result.output
    shipped = build.shipped()
    for path in ("pudge/__init__.py", "pudge/legacy_install.py", "tests/test_example.py", "install.sh"):
        assert path in shipped, path
    # Written by this build, not tracked: still part of the release.
    assert "pudge/build-info.json" in shipped and "pudge/assets/jimaku-trial-key" in shipped
    assert not set(LEFTOVERS) & set(shipped)
    assert not [path for path in shipped if "__pycache__" in path]


def test_a_plain_folder_with_a_removed_path_is_refused(tmp_path: Path) -> None:
    build = build_release(tmp_path / "tree", untracked={"pudge/web/companion/companion.js": "old\n"})
    assert build.result.returncode != 0
    assert "retired path in release: pudge/web/companion" in build.result.output
    assert not build.archive.exists()


def test_a_plain_folder_without_leftovers_still_builds(tmp_path: Path) -> None:
    build = build_release(tmp_path / "tree")
    assert build.result.returncode == 0, build.result.output
    assert "pudge/__init__.py" in build.shipped()
