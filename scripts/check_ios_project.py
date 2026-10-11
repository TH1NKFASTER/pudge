#!/usr/bin/env python3
"""Cross-platform structural checks for the hand-written Pudge Xcode project."""

from __future__ import annotations

import plistlib
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios"
PBX = IOS / "Pudge.xcodeproj" / "project.pbxproj"
SCHEME = IOS / "Pudge.xcodeproj" / "xcshareddata" / "xcschemes" / "Pudge.xcscheme"
INFO = IOS / "Pudge" / "Info.plist"
PACKAGE = IOS / "PudgeCore" / "Package.swift"
ICON = IOS / "Pudge" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
RUNNER = ROOT / "run-ios-xcode-checks.command"
IOS_README = IOS / "README.md"


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    for path in (PBX, SCHEME, INFO, PACKAGE, ICON, RUNNER, IOS_README):
        if not path.is_file():
            fail(f"missing required iOS project file: {path.relative_to(ROOT)}")

    pbx = PBX.read_text(encoding="utf-8")
    object_ids = set(re.findall(r'^\s*"([0-9A-F]{24})" = \{', pbx, re.M))
    if not object_ids:
        fail("project.pbxproj contains no Xcode objects")
    references: list[str] = []
    for line in pbx.splitlines():
        if re.match(r'^\s*"[0-9A-F]{24}" = \{$', line):
            continue
        references.extend(re.findall(r'"([0-9A-F]{24})"', line))
    missing_refs = sorted(set(references) - object_ids)
    if missing_refs:
        fail(f"dangling Xcode object references: {missing_refs}")

    declared_paths = re.findall(r'"path" = "([^"]+)";', pbx)
    missing_paths = []
    for value in declared_paths:
        if value.endswith((".app", ".xctest")):
            continue
        if not (IOS / value).exists():
            missing_paths.append(value)
    if missing_paths:
        fail(f"project references missing paths: {missing_paths}")

    expected_swift = sorted(
        str(path.relative_to(IOS))
        for folder in ("Pudge", "PudgeTests", "PudgeUITests")
        for path in (IOS / folder).rglob("*.swift")
    )
    omitted = [path for path in expected_swift if f'"path" = "{path}";' not in pbx]
    if omitted:
        fail(f"Swift files omitted from Xcode project: {omitted}")

    tree = ET.parse(SCHEME)
    scheme_target_ids = {
        node.attrib["BlueprintIdentifier"]
        for node in tree.findall(".//BuildableReference")
        if "BlueprintIdentifier" in node.attrib
    }
    unknown_targets = sorted(scheme_target_ids - object_ids)
    if unknown_targets:
        fail(f"scheme references unknown target IDs: {unknown_targets}")
    target_names = set(re.findall(r'"name" = "(Pudge(?:Tests|UITests)?)";', pbx))
    if target_names != {"Pudge", "PudgeTests", "PudgeUITests"}:
        fail(f"unexpected native targets: {sorted(target_names)}")

    with INFO.open("rb") as handle:
        info = plistlib.load(handle)
    required_info = {
        "CFBundleIdentifier",
        "CFBundleShortVersionString",
        "CFBundleVersion",
        "NSCameraUsageDescription",
        "NSLocalNetworkUsageDescription",
        "NSBonjourServices",
        "UIBackgroundModes",
    }
    missing_info = sorted(required_info - info.keys())
    if missing_info:
        fail(f"Info.plist missing required keys: {missing_info}")
    if "_pudge-audio._tcp" not in info.get("NSBonjourServices", []):
        fail("Info.plist is missing the Pudge Bonjour service")
    if "audio" not in info.get("UIBackgroundModes", []):
        fail("Info.plist is missing background audio mode")

    signing_example = IOS / "Config" / "Signing.example.xcconfig"
    if not signing_example.is_file():
        fail("missing Signing.example.xcconfig")
    signing = signing_example.read_text(encoding="utf-8")
    if "PUDGE_IOS_BUNDLE_ID" not in signing or "DEVELOPMENT_TEAM" not in signing:
        fail("Signing.example.xcconfig does not document required signing settings")

    if not (RUNNER.stat().st_mode & 0o111):
        fail("run-ios-xcode-checks.command must be executable")

    # App Store/device builds reject App Store icons with transparency.
    png = ICON.read_bytes()
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or png[12:16] != b"IHDR":
        fail("AppIcon is not a valid PNG")
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    color_type = png[25]
    if (width, height) != (1024, 1024):
        fail(f"AppIcon must be 1024x1024, got {(width, height)}")
    if color_type in (4, 6) or b"tRNS" in png:
        fail("AppIcon must not contain transparency")

    # MPNowPlayingInfoCenter.playbackState is a macOS-only API. The iOS app
    # communicates playback state through nowPlayingInfo/playback rate instead.
    audio_player = (IOS / "Pudge" / "Features" / "Audio" / "AudioPlayerService.swift").read_text(
        encoding="utf-8"
    )
    if "MPNowPlayingInfoCenter.default().playbackState" in audio_player:
        fail("iOS target uses macOS-only MPNowPlayingInfoCenter.playbackState")

    print(f"iOS project structure OK: {len(object_ids)} Xcode objects, {len(expected_swift)} Swift files")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"iOS project check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
