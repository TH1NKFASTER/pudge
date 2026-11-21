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
    "test_fix_ios_finished_anime_no_card.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_fix_finale2_ios_guards.py": "iPhone/iPad finale sheet (Swift) runs only in Xcode on the Mac",
    "test_fix_seqpre_ios_guards.py": "iPhone/iPad finale sheet (Swift) runs only in Xcode on the Mac",
    "test_fix_iosperf_guards.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_fix_ios_player_orientation_guards.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_patch_68b_localization.py": "every interface text comes from the catalogs; every key used exists",
    "test_v073_inline_js_syntax.py": "syntax lint of index.html's inline scripts",
    "test_v0107_branded_confirm_dialog.py": "no native confirm() anywhere in the window",
    "test_iphone_native_swift_guards.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_iphone_reader_media_order.py": "iPhone-only interaction (Swift)",
    "test_iphone_single_continue.py": "iPhone-only interaction (Swift)",
    "test_fix_review_ios.py": "iPhone app (Swift) runs only in Xcode on the Mac",
    "test_fix_iphone_app_sync_review.py": "iPhone app (Swift) runs only in Xcode on the Mac; its protocol doc",
    "test_fix_ios17_deployment_target.py": "iPhone/iPad app (Swift) settings; runs only in Xcode on the Mac",
    "test_fix_ios_reader_guards.py": "iPhone/iPad readers (Swift) run only in Xcode on the Mac",
    "test_fix_ios_reader_follow_line.py": "iPhone/iPad light-novel reader with the audiobook (Swift) runs only in Xcode on the Mac",
    "test_fix_release_notes_and_clean_tree.py": "the release workflow file is GitHub configuration; its notes step is run here",
    "test_fix_ci_runner_images.py": "the workflow files are GitHub configuration; their runner labels are checked here",
    "test_fix_ios_watched_elsewhere_cleanup.py": "iPhone/iPad watched cleanup (Swift) runs only in Xcode / swift test on the Mac",
    "test_fix_ios_pending_books_in_series.py": "iPhone/iPad audiobook library (Swift) runs only in Xcode on the Mac",
    "test_fix_manga_two_page_view.py": "iPhone/iPad manga reader (Swift) runs only in Xcode on the Mac",
    "test_fix_manga_page_alignment_ios.py": "iPhone/iPad manga reader (Swift) runs only in Xcode on the Mac",
    "test_fix_tails_review_leftovers.py": "iPhone/iPad catalog cache, downloads, subtitles, background sync (Swift) run only in Xcode on the Mac; its protocol doc",
    "test_fix_ios_text_selection.py": "iPhone/iPad text selection and pairing (Swift) run only in Xcode on the Mac",
    "test_fix_ios_device_inventory.py": "iPhone/iPad download inventory reporting (Swift) runs only in Xcode on the Mac",
    "test_fix_ios_mac_reachability.py": "iPhone/iPad Mac discovery and reachability (Swift) run only in Xcode on the Mac",
    "test_fix_ios_video_stream_guards.py": "iPhone/iPad streaming player (Swift) runs only in Xcode on the Mac",
    "test_fix_ipadplay_player_guards.py": "iPhone/iPad player log, watchdog, close and download check (Swift) run only in Xcode on the Mac",
    "test_fix_stream_buffer_guards.py": "iPhone/iPad position bar and play while downloading (Swift) run only in Xcode on the Mac",
    "test_fix_ios_word_panel_and_grammar_chat.py": "iPhone/iPad word panel and grammar chat (Swift) run only in Xcode on the Mac",
    "test_fix_ios_panel_and_video_gestures.py": "iPhone/iPad word panel, video gestures and selection (Swift) run only in Xcode on the Mac",
    "test_fix_ios_subtitle_selection_handles.py": "iPhone/iPad subtitle selection handles in the player (Swift) run only in Xcode on the Mac",
    "test_fix_ios_player_handles_wheels_size_watched.py": "iPhone/iPad player handles, wheels, text size and Mac watches (Swift) run only in Xcode on the Mac",
    "test_fix_ios3_player_polish_guards.py": "iPhone/iPad player end, tabs, taps, controls timer, subtitle hit area, outlines, download order (Swift) run only in Xcode on the Mac",
    "test_fix_ios_library_grid.py": "iPhone/iPad library grids (Swift) run only in Xcode on the Mac",
    "test_fix_ios_card_texts.py": "iPhone/iPad card texts (Swift) run only in Xcode on the Mac",
    "test_fix_reader_selection_window_space.py": "iPhone/iPad light-novel reader selection coordinates (Swift) run only in Xcode on the Mac",
    "test_fix_reader_landscape_selection.py": "iPhone/iPad light-novel reader selection (Swift) runs only in Xcode on the Mac",
    "test_fix_ios_japanese_glyphs.py": "iPhone/iPad Japanese typesetting and fonts (Swift) run only in Xcode on the Mac",
    "test_fix_ios_player_stays_open_without_mac.py": "iPhone/iPad player continuity (Swift) runs only in Xcode on the Mac",
    "test_fix_iphone_caught_up_shelves.py": "iPhone/iPad anime shelves (Swift) run only in Xcode on the Mac",
    "test_fix_ios_episode_resume_guards.py": "iPhone/iPad episode download resume (Swift) runs only in Xcode on the Mac",
    "test_fix_ios4_phone_screens.py": "iPhone/iPad player wheels, qualities, library header and Settings (Swift) run only in Xcode on the Mac",
    "test_fix_ios5_phone_wishes.py": "iPhone/iPad Settings, anime download picker and player touch window (Swift) run only in Xcode on the Mac",
    "test_fix_ios7_play_list_nowplaying.py": "iPhone/iPad anime card Play, episode list emphasis and audiobook Now Playing (Swift) run only in Xcode on the Mac",
    "test_fix_ios_duplicate_episode_timing_skip_guards.py": "iPhone/iPad episode list, timing wheel and Skip button (Swift) run only in Xcode on the Mac",
    "test_fix_ios_watched_cleanup_and_storage.py": "iPhone/iPad storage, version and diagnostics (Swift, Xcode project) run only in Xcode on the Mac",
    "test_fix_rv3_ios.py": "iPhone/iPad sync, downloads and memory (Swift) run only in Xcode on the Mac; its protocol doc",
    "test_fix_rv4_ios.py": "iPhone/iPad JSON contracts with the Mac, finished rule, catalog cache and subtitle writes (Swift) run only in Xcode on the Mac",
    "test_fix_rv5_ios.py": "iPhone/iPad JSON contract with the Mac (every Codable struct) and sync leftovers (Swift) run only in Xcode on the Mac; its protocol doc",
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
    "test_source_neutral.py": "no built-in release source address or site name in any shipped file",
    "test_fix_size_whisper_vendored.py": "imports of the vendored Whisper; uv.lock and package data",
    "test_fix_repository_content_checks.py": "the gateway itself: it reads only files git tracks",
    "test_fix_bridge_allowlist.py": "the bridge allow-list equals the API methods the pages name",
    "test_fix_atomic_writes.py": "no module writes through a fixed temporary file name",
    "test_fix_watch_union_swift.py": "iPhone/iPad player place sync (Swift) runs only in Xcode on the Mac",
    "test_no_copyrighted_text.py": "the repository holds invented text only: no line of a real work in any file",
    "test_fix_stats_precompute.py": "iPhone/iPad Statistics cache and spinner (Swift) run only in Xcode on the Mac",
    "test_fix_review2_swift_guards.py": "iPhone/iPad sync availability and finale score retries (Swift) run only in Xcode on the Mac",
    "test_fix_review_swift_guards.py": "iPhone/iPad player end notice and stall monitor (Swift) run only in Xcode on the Mac",
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
