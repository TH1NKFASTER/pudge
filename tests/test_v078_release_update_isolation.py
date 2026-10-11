"""The installer checks the installed package in an isolated Python (behaviour; audit 71Б
rewrite: install.sh runs with stand-ins, tests/t71g01_support.py). That the updater starts
an extracted installer through zsh: test_v074_fast_safe_updater.py
(test_updater_runs_the_fast_installer_with_zsh_and_a_clean_environment)."""

from __future__ import annotations

from t71g01_support import source_history


def test_version_check_ignores_a_matching_pudge_on_pythonpath() -> None:
    # The installed code says 9.8.9 for a 9.9.0 wheel; PYTHONPATH points at a 9.9.0 copy
    # elsewhere. Only the installed package counts: the install stops before the app is touched.
    mismatch = source_history().steps["mismatch"]
    assert mismatch.result.returncode != 0
    assert "AssertionError: ('9.8.9', '9.9.0')" in mismatch.result.output
    assert not mismatch.result.calls("clang")
    assert mismatch.app_after == mismatch.app_before
