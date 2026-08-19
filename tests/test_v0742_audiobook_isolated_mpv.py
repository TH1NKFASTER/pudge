from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE_PATH = ROOT / "pudge/audiobooks.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


def _play_command_literals() -> list[str]:
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "play":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "command"
                for target in child.targets
            ):
                continue
            if not isinstance(child.value, ast.List):
                continue
            values = [
                element.value
                for element in child.value.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            if "--no-video" in values:
                return values
    raise AssertionError("Audiobook mpv command was not found")


def test_audiobook_mpv_ignores_all_user_config_and_scripts() -> None:
    values = _play_command_literals()

    assert "--no-config" in values
    assert "--load-scripts=no" in values
    assert values.index("--no-config") < values.index("--no-video")
    assert values.index("--load-scripts=no") < values.index("--no-video")


def test_audiobook_command_does_not_explicitly_load_study_plugins() -> None:
    values = _play_command_literals()
    lowered = [value.casefold() for value in values]

    assert not any("jiten" in value for value in lowered)
    assert not any("jpdb" in value for value in lowered)
    assert not any(value.startswith("--script=") for value in values)


def test_paired_reading_uses_the_same_isolated_audiobook_player() -> None:
    paired_source = SOURCE.split(
        "    def play_paired(",
        1,
    )[1].split(
        "\n    def ",
        1,
    )[0]

    assert "self.play(" in paired_source
