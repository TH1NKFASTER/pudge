from pathlib import Path

import pudge


def test_min_score_is_not_exposed_in_settings_ui() -> None:
    source = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "input('s_min_score'" not in source
    assert "min_score:Number(v('s_min_score'))" not in source
    assert "min_score:Number(ui.state.settings?.min_score??72)" in source


def test_release_version_matches_project() -> None:
    import tomllib
    from pathlib import Path

    root = Path(__file__).parents[1]
    expected = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert pudge.__version__ == expected
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    assert f'version = "{expected}"' in pyproject
    assert f"Current version: **{expected}**." in readme
    assert "pudge-macos-v0.7.17.zip" in readme
