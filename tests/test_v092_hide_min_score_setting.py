"""The subtitle minimum score is not a setting any more (behaviour; audit 71Б rewrite). The
release version in pyproject.toml, pudge/__init__.py and the README's "Current version" line
is checked in test_v0672_ln_qbt_bleach_repair.py::test_v0672_version."""

from __future__ import annotations

import tomllib

from source_policy import source


def test_settings_have_no_min_score_control_and_saving_keeps_the_stored_value(page) -> None:
    save = "__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});"
    run = """
    const present = Boolean(document.getElementById('s_min_score'));
    await __click('#saveSettings'); await __tick(10);
    return present;
    """
    stored = page.run(run, state={"settings": {"min_score": 80}}, before=save)
    assert stored.value is False
    assert stored.calls_of("save_settings")[0][0]["min_score"] == 80
    default = page.run(run, before=save)  # an old config without the key
    assert default.calls_of("save_settings")[0][0]["min_score"] == 72


def test_readme_install_command_unzips_the_current_release() -> None:
    # policy: the README's install command is copied by users; it must name this release's archive.
    expected = tomllib.loads(source("pyproject.toml"))["project"]["version"]
    assert f"unzip pudge-macos-v{expected}.zip" in source("README.md")  # the release asset name
