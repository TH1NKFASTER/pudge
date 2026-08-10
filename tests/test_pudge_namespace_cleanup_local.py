from __future__ import annotations

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
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pudge"' in text
    assert 'pudge = "pudge.cli:main"' in text
    assert 'pudge-agent = "pudge.agent:main"' in text
    assert 'include = ["pudge*"]' in text
    assert "anime" + "_mpv" not in text
