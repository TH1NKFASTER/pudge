from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_schedule_cards_do_not_show_custom_cadence() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    caught_up = html[html.index("function caughtUpHomeCard"):html.index("function droppedHomeCard")]
    menu = html[html.index("function showAnimeMenu"):html.index("function positionContextMenu")]
    assert "personalScheduleRuleLabel" not in caught_up
    assert "scheduleCadence" not in caught_up
    assert "personalScheduleRuleLabel" not in menu
    assert "scheduleCadence" not in menu
    assert "formatRemaining(scheduledRemaining)" in caught_up


def test_release_bundle_contains_documented_developer_paths() -> None:
    build = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    assert "cp -R pudge tests" in build
    assert "Makefile" in build and "MOBILE_SYNC_PROTOCOL.md" not in build
    assert "cp -R .github/workflows" in build
    assert ".github/release/check_release_bundle.py" in build
    assert "rename_brand.py" not in build
    assert "brand_migration.py" not in build


def test_release_ci_has_same_quality_gate_as_local_release() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "name: Quality" in workflow
    assert "make quality PYTHON=python" in workflow
    assert "needs: [quality, verify-and-test]" in workflow


def test_legacy_migrator_is_not_a_root_level_generic_renamer() -> None:
    assert not (ROOT / "brand_migration.py").exists()
    assert not (ROOT / "rename_brand.py").exists()
    assert (ROOT / "pudge/legacy_install.py").is_file()
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "pudge/legacy_install.py" in install
