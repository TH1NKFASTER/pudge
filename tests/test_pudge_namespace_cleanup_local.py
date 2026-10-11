from __future__ import annotations

import importlib
import json
import tomllib
from pathlib import Path

from pudge.branding import (
    APP_AGENT_CLI,
    APP_BUNDLE_ID,
    APP_CLI,
    APP_ENV_PREFIX,
    APP_NAME,
    APP_SLUG,
    LEGACY_APP_NAMES,
    LEGACY_APP_SLUGS,
    LEGACY_BUNDLE_IDS,
)
from source_policy import source

ROOT = Path(__file__).parents[1]


def test_pudge_is_the_active_internal_identity() -> None:
    assert APP_NAME == "pudge"
    assert APP_SLUG == "pudge"
    assert APP_BUNDLE_ID == "com.pudge.app"
    assert APP_CLI == "pudge"
    assert APP_AGENT_CLI == "pudge-agent"
    assert APP_ENV_PREFIX == "PUDGE"
    assert (ROOT / "pudge").is_dir()
    assert not (ROOT / ("anime" + "_mpv")).exists()


def test_legacy_brand_is_retained_only_for_migration() -> None:
    old_visible = "Anime" + " MPV"
    old_slug = "anime" + "-mpv"
    old_bundle = "com." + old_slug + ".app"
    assert old_visible in LEGACY_APP_NAMES
    assert old_slug in LEGACY_APP_SLUGS
    assert old_bundle in LEGACY_BUNDLE_IDS


def test_packaging_points_at_pudge_namespace() -> None:
    # policy: the package metadata in pyproject.toml (name, console scripts, packages) is only
    # read by pip/setuptools at install time; check it names the pudge namespace and that the
    # console scripts lead to callables.
    project = tomllib.loads(source("pyproject.toml"))
    assert project["project"]["name"] == "pudge"
    scripts = project["project"]["scripts"]
    assert scripts == {"pudge": "pudge.cli:main", "pudge-agent": "pudge.agent:main"}
    for target in scripts.values():
        module, _, function = target.partition(":")
        assert callable(getattr(importlib.import_module(module), function))
    include = project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert include == ["pudge*"]
    assert "anime" + "_mpv" not in json.dumps(project)
