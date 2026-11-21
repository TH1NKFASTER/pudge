"""The iPhone/iPad app runs on iOS/iPadOS 17 (iPads that cannot update to 18 stop at 17.7).

policy: Swift cannot be compiled here; these guards keep the project settings consistent and
keep iOS-18-only SwiftUI APIs behind an availability check."""

from __future__ import annotations

import re

from source_policy import source, sources


def test_every_target_and_the_package_allow_ios_17() -> None:
    pbx = source("ios/Pudge.xcodeproj/project.pbxproj")
    targets = re.findall(r'"IPHONEOS_DEPLOYMENT_TARGET" = "([0-9.]+)";', pbx)
    assert targets and set(targets) == {"17.0"}
    assert '"TARGETED_DEVICE_FAMILY" = "1,2";' in pbx  # iPhone and iPad
    package = source("ios/PudgeCore/Package.swift")
    assert '.iOS("17.0")' in package
    generator = source("scripts/generate_ios_project.py")
    assert '"IPHONEOS_DEPLOYMENT_TARGET": "17.0"' in generator


IOS18_ONLY = (
    "onScrollPhaseChange",
    "onScrollGeometryChange",
    "onScrollVisibilityChange",
    "MeshGradient",
    "presentationSizing",
    "navigationTransition",
    "matchedTransitionSource",
    "Group(subviews",
    "ForEach(subviews",
    "TabSection",
)


def test_ios_18_only_swiftui_apis_sit_behind_an_availability_check() -> None:
    swift = sources("ios/**/*.swift")
    assert swift
    for path, text in swift.items():
        for api in IOS18_ONLY:
            for match in re.finditer(re.escape(api), text):
                before = text[max(0, match.start() - 400) : match.start()]
                assert "#available(iOS 18" in before or "@available(iOS 18" in before, (path, api)


def test_swift_files_using_core_graphics_types_import_them() -> None:
    # On Linux Foundation carries CGPoint/CGRect with all members; on iOS/macOS `.zero`,
    # CGRect(x:y:width:height:) and `.width` come from CoreGraphics (or SwiftUI/UIKit, which bring it).
    swift = sources("ios/**/*.swift")
    assert swift
    for path, text in swift.items():
        if not re.search(r"\bCG(Point|Rect|Size|Float|Vector|AffineTransform)\b", text):
            continue
        imports = set(re.findall(r"^\s*import\s+(\w+)", text, flags=re.M))
        assert imports & {
            "CoreGraphics",
            "SwiftUI",
            "UIKit",
            "AppKit",
            "QuartzCore",
            "PDFKit",
            "AVFoundation",
        }, path
