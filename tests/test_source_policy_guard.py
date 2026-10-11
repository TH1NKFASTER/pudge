"""Tests check behaviour, not source text (audit 71Б): the guard and its allow-list.

Tests may read Pudge's code base only through tests/source_policy.py, for rules about the
code base as a whole where there is nothing to run. Every module that does is listed here
with the reason; adding one is a deliberate decision in review. Everything else is a
behaviour test: tests/page_harness.py (window), calling Python, tests/lua_harness.py (mpv),
tests/css_support.py (styles), tests/t71g00_support.py (install.sh with stand-ins).
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

import pytest

import source_policy
from source_read_guard import SourceTextRead

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

POLICY_TESTS = {
    "test_patch_68b_localization.py": "every interface text comes from the catalogs; every key used exists",
    "test_v073_inline_js_syntax.py": "syntax lint of index.html's inline scripts",
    "test_v0107_branded_confirm_dialog.py": "no native confirm() anywhere in the window",
    "test_iphone_native_swift_guards.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_iphone_reader_media_order.py": "iPhone-only interaction (Swift)",
    "test_iphone_single_continue.py": "iPhone-only interaction (Swift)",
    "test_pudge_namespace_cleanup_local.py": "package metadata in pyproject.toml",
    "test_v0106_audiobook_chapter_hover_and_dialog_icon.py": "dependency range in pyproject.toml",
    "test_v0558_torrent_cleanup_and_permissions.py": "Info.plist written by the macOS-only installer; a dependency",
    "test_v0577_native_notifications.py": "the generated Objective-C launcher (compiled only on a Mac)",
    "test_v0615_ocr_graph_library_shortcuts.py": "runs a block of install.sh (the script cannot be sourced)",
    "test_install_smoke_v0730.py": "runs blocks of install.sh (the script cannot be sourced)",
    "test_v0628_aria2_backend.py": "the launcher and LaunchAgent environment, macOS-only installer parts",
    "test_v0672_ln_qbt_bleach_repair.py": "release version in pyproject.toml and README",
    "test_v092_hide_min_score_setting.py": "README's install command names this release",
    "test_v0716_first_experience.py": "README documentation people follow",
    "test_v0722_uninstall_and_mobile_refresh.py": "README removal instructions",
    "test_v072_audio_status_credentials_playback.py": "README setup instructions",
    "test_v072_followup_ln_credentials_progress.py": "README credentials guide",
    "test_v0727_vn_sck_audiobook_live.py": "a dependency declaration",
    "test_v0730_mokuro_environment.py": "GPL-3.0 tools are never imported by pudge modules",
    "test_v0728_g19_release_migration_schedule_ui.py": "GitHub workflow configuration (its steps are run)",
    "test_v072_jobs_state_links_docs.py": "GitHub workflow configuration (its steps are run)",
    "test_v072_product_updates.py": "GitHub workflow configuration (its steps are run)",
    "test_v0730_update_installer_survives_quit.py": "GitHub workflow configuration (its steps are run)",
    "test_v095_release_reproducibility.py": "GitHub workflow configuration; uv lock environments",
    "test_fix_locked_dev_installs.py": "GitHub workflow configuration (its steps are run); uv lock environments",
    "test_fix_lean_ci.py": "GitHub workflow configuration (its jobs and steps are run); uv.lock versions",
    "test_fix_release_v0729_structure.py": "GitHub workflow configuration (its steps are run)",
    "test_fix_ci_without_media_tools.py": "GitHub workflow configuration (its steps are run)",
}
# Node harnesses that run code as a page or load a module file whole.
JS_CORE = {"page.cjs", "page_env.cjs", "page_worker.cjs", "dom_lite.cjs", "i18n_prelude.cjs"}


def _imports_source_policy(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.endswith("source_policy") for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("source_policy"):
            return True
    return False


def test_only_listed_policy_tests_read_source_text() -> None:
    users = {
        path.name
        for path in TESTS.glob("*.py")
        if path.name != "source_policy.py" and _imports_source_policy(path)
    }
    users.discard(Path(__file__).name)
    assert users - set(POLICY_TESTS) == set(), "add a reason to POLICY_TESTS or test behaviour instead"
    assert set(POLICY_TESTS) - users == set(), "remove modules that no longer read source text"


def test_node_harnesses_run_modules_instead_of_cutting_their_text() -> None:
    offenders = []
    for path in sorted((TESTS / "js").glob("*.cjs")):
        if path.name in JS_CORE:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"readFileSync\(", text):
            before = text[max(0, match.start() - 40) : match.start()]
            if not re.search(r"vm\.runIn(?:New)?Context\(\s*(?:fs\.)?$", before):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}")
    assert offenders == [], (
        "load a module whole with vm.runInContext(fs.readFileSync(...)) or use the page harness"
    )


def test_reading_source_text_from_a_test_fails_with_an_explanation(tmp_path) -> None:
    with pytest.raises(SourceTextRead, match="tests check behaviour"):
        (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    with pytest.raises(SourceTextRead):
        open(ROOT / "install.sh", encoding="utf-8")  # noqa: SIM115
    # The allowed ways: the policy gateway, copying code to run it, the app reading its files.
    assert "<html" in source_policy.source("pudge/web/index.html")
    shutil.copy2(ROOT / "install.sh", tmp_path / "install.sh")
    from pudge import i18n

    assert i18n.catalog("en")["nav.settings"]
