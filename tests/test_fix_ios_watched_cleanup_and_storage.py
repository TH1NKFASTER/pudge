"""policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac.

iPhone/iPad: watched downloads deleted like on the Mac, storage that matches its lists, the
app's version and a diagnostics report that opens.

The rules live in PudgeCore with XCTest cases (ios/PudgeCore/Tests/PudgeCoreTests/
WatchedCleanupTests.swift and StorageScanTests.swift, `swift test --package-path ios/PudgeCore`
on the Mac). Here: the Mac sends its delay with the episode list, and the app is wired to the rules.
"""

from __future__ import annotations

import importlib.util
import math
import plistlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from source_policy import source, source_bytes

from pudge.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return source(relative)


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _api(hours):
    return SimpleNamespace(config=SimpleNamespace(agent=SimpleNamespace(delete_after_watched_hours=hours)))


# Mac: agent.delete_after_watched_hours reaches the phones


@pytest.mark.parametrize(("hours", "sent"), [(24.0, 24.0), (6, 6.0), (0, 0.0), (-3, 0.0), ("12", 12.0)])
def test_the_anime_list_carries_the_macs_delete_delay(service, hours, sent):
    service.api = _api(hours)
    answer = service.media.anime()
    assert answer["items"] == []
    assert answer["deleteWatchedAfterHours"] == sent
    # The phone's route answers the same.
    routed = service.media.handle("GET", "/api/v2/media/anime", {}, dict)
    assert routed["deleteWatchedAfterHours"] == sent


@pytest.mark.parametrize("hours", [math.nan, math.inf, None, "soon"])
def test_an_unusable_setting_is_not_sent(service, hours):
    service.api = _api(hours)
    assert "deleteWatchedAfterHours" not in service.media.anime()


def test_no_api_no_setting_and_the_flat_episode_list_is_unchanged(service):
    service.api = None
    assert service.media.anime() == {"items": []}
    service.api = _api(24)
    assert service.media.episodes() == {"items": []}


# iPhone/iPad: wiring of the PudgeCore rules


def test_core_rules_and_their_xctests_exist():
    core = _text("ios/PudgeCore/Sources/PudgeCore/WatchedCleanup.swift")
    assert "watchingBelow = 0.85" in core
    for case in ("mac", "immediately", "day", "week", "off"):
        assert f"case .{case}" in core
    tests = _text("ios/PudgeCore/Tests/PudgeCoreTests/WatchedCleanupTests.swift")
    for name in (
        "testNeverWhileWatchedAgainUnder85Percent",
        "testNeverWhenKeptOrDownloadedAgainAfterTheWatch",
        "testNotWhilePlayingOrDownloading",
        "testTheListSaysWhen",
    ):
        assert f"func {name}()" in tests
    scan = _text("ios/PudgeCore/Tests/PudgeCoreTests/StorageScanTests.swift")
    for name in (
        "testAVideoWithoutItsRecordIsAnOtherFileRemovedAtLaunch",
        "testARecordThatCouldNotBeReadWaitsForClear",
        "testInterruptedDownloadsStayWhileSomethingDownloads",
        "testBackgroundSessionFilesStayWhileAnAudiobookCanResume",
    ):
        assert f"func {name}()" in scan


def test_the_phone_keeps_the_macs_delay_and_deletes_after_it():
    media = _text("ios/Pudge/Features/Media/MediaModel.swift")
    assert (
        'sendIfChanged("/api/v2/media/anime", etag: live ? animeListTag : nil, as: AnimeListResponse.self)'
        in media
    )
    assert "let deleteWatchedAfterHours: Double?" in media
    assert "ItemsResponse<AnimeGroup>" not in media
    # The time of the watch is kept; a re-download after a watch is never deleted on its own.
    assert "item.watchedAt = Date().timeIntervalSince1970" in media
    assert (
        "if episode.watched || pendingEpisodes[episode.id]?.watchedSent != nil { local.kept = true }" in media
    )
    assert "busy: playingEpisodes.contains(item.id) || videoJobs[item.id] != nil" in media
    assert "WatchedCleanup.isDue(cleanupState($0), delay: delay, now: now)" in media
    # A failed download never leaves a video without its record.
    assert "if !FileManager.default.fileExists(atPath: metaURL(episode.id).path) {" in media
    watch = _text("ios/Pudge/UI/WatchScreen.swift")
    assert "media.playerOpened(episode.id)" in watch and "media.playerClosed(episode.id)" in watch
    assert "PendingDeletionLine(media: media, id: item.id)" in watch
    app = _text("ios/Pudge/App/PudgeApp.swift")
    assert "model.launchCleanup()" in app
    assert "model.media.deleteWatchedEpisodes()" in app
    settings = _text("ios/Pudge/UI/SettingsScreen.swift")
    assert 'Picker("Delete watched episodes", selection: $deleteWatched)' in settings
    for tag in ("mac", "immediately", "day", "week", "off"):
        assert f"WatchedCleanupChoice.{tag}.rawValue" in settings
    assert 'Button("Keep") { media.keepEpisode(id) }' in settings


def test_storage_counts_what_the_lists_reach_plus_other_files():
    model = _text("ios/Pudge/App/PudgeModel.swift")
    assert "usage.summary = StorageScan.summarize(storedFiles(roots), refs)" in model
    assert "StorageScan.launchCleanup(files, refs)" in model
    # Unreadable store: nothing is a leftover.
    assert (
        "guard let audio = try? store.resourcePaths(), let records = try? store.transfers() else { return nil }"
        in model
    )
    # The old count skipped the video and reading folders and called everything else "audio".
    assert 'url.path.contains("/video/")' not in model
    assert '("tmp", FileManager.default.temporaryDirectory)' in model
    assert '"com.apple.nsurlsessiond"' in model
    settings = _text("ios/Pudge/UI/SettingsScreen.swift")
    assert "DownloadedEpisodesScreen(media: model.media)" in settings
    assert (
        'LabeledContent("Episodes", value: PudgeTheme.storageSize(usage[.episodes]))'
    )  # "—" when empty (IOS4) in settings
    assert 'Button("Clear other files") { model.clearOtherFiles(); usageRequest += 1 }' in settings
    assert "media.videoBytes" not in settings


# Version and diagnostics


def _pyproject_version() -> str:
    return tomllib.loads(_text("pyproject.toml"))["project"]["version"]


def test_the_ios_version_is_pudges():
    pbx = _text("ios/Pudge.xcodeproj/project.pbxproj")
    versions = re.findall(r'"MARKETING_VERSION" = "([^"]+)";', pbx)
    assert versions and set(versions) == {_pyproject_version()}
    info = plistlib.loads(source_bytes("ios/Pudge/Info.plist"))
    assert info["CFBundleShortVersionString"] == "$(MARKETING_VERSION)"
    assert info["PudgeGitCommit"] == "unknown"
    generator = _text("scripts/generate_ios_project.py")
    assert '"MARKETING_VERSION": pudge_version(),' in generator
    settings = _text("ios/Pudge/UI/SettingsScreen.swift")
    assert 'LabeledContent("Version", value: PudgeModel.appVersion)' in settings
    assert "0.7.30" not in settings
    assert 'lines.append("App " + Self.appVersion)' in _text("ios/Pudge/App/PudgeModel.swift")


def test_the_build_records_its_commit_in_the_app_target():
    pbx = _text("ios/Pudge.xcodeproj/project.pbxproj")
    phase = "FB8CDE8C231488530EDE1892"
    block = re.search(r'"%s" = \{(.*?)\n\t\t\};' % phase, pbx, re.S).group(1)
    assert '"isa" = "PBXShellScriptBuildPhase";' in block
    assert "Set :PudgeGitCommit" in block and "|| true" in block
    target = re.search(r'"isa" = "PBXNativeTarget";(.*?)"name" = "Pudge";', pbx, re.S).group(1)
    assert phase in target
    # The script reads .git: the app target runs it outside the script sandbox.
    assert pbx.count('"ENABLE_USER_SCRIPT_SANDBOXING" = "NO";') == 2
    check = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_ios_project.py")], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stderr


def _bump_helper():
    spec = importlib.util.spec_from_file_location("bump_under_test", ROOT / ".github/release/bump_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bumping_the_version_moves_the_ios_version_too(tmp_path, monkeypatch):
    project = tmp_path / "ios/Pudge.xcodeproj"
    project.mkdir(parents=True)
    pbx = project / "project.pbxproj"
    pbx.write_text(_text("ios/Pudge.xcodeproj/project.pbxproj"), encoding="utf-8")
    helper = _bump_helper()
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    helper.replace_ios_version("9.8.7")
    assert set(re.findall(r'"MARKETING_VERSION" = "([^"]+)";', pbx.read_text())) == {"9.8.7"}
    # A checkout without the iOS project (release tests) is fine.
    pbx.unlink()
    helper.replace_ios_version("9.8.8")
    release = _text(".github/release/release.py")
    assert '"ios/Pudge.xcodeproj/project.pbxproj",' in release
    assert 'git("add", "--", *[name for name in RELEASE_FILES if (ROOT / name).exists()])' in release


def test_share_diagnostics_is_one_button_anchored_for_ipad():
    settings = _text("ios/Pudge/UI/SettingsScreen.swift")
    # The ShareLink (shown after "Prepare…") did nothing on the iPad.
    assert "ShareLink(" not in settings
    assert "Button { shareDiagnostics() }" in settings
    assert ".background(ShareAnchorView(anchor: diagnosticsAnchor))" in settings
    assert "popover.sourceView = view" in settings and "popover.sourceRect = view.bounds" in settings
    assert "while let next = top.presentedViewController" in settings
    model = _text("ios/Pudge/App/PudgeModel.swift")
    # The report names every stored kind, the other files and the episodes on the device.
    assert 'lines.append("- other: " + file.path' in model
    assert "Episodes on this \\(PudgeTheme.device)" in model
