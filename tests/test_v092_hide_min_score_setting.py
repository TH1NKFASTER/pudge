from pathlib import Path

import pudge


def test_min_score_is_not_exposed_in_settings_ui() -> None:
    source = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "input('s_min_score'" not in source
    assert "min_score:Number(v('s_min_score'))" not in source
    assert "min_score:Number(ui.state.settings?.min_score??72)" in source


def test_release_version_is_0717() -> None:
    assert pudge.__version__ == "0.7.17"
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    assert 'version = "0.7.17"' in pyproject
    assert "Current version: **0.7.17**." in readme
    assert "pudge-macos-v0.7.17.zip" in readme
