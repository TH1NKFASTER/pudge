#!/usr/bin/env python3
"""Generate the checked-in Xcode project without XcodeGen or network packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios"
objects: dict[str, dict] = {}


def uid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()


def obj(key_name: str, kind: str, **fields) -> str:
    key = uid(key_name)
    objects[key] = {"isa": kind, **fields}
    return key


def serialize(value, depth=0) -> str:
    if isinstance(value, dict):
        return (
            "{\n"
            + "".join(
                "\t" * (depth + 1) + json.dumps(str(k)) + " = " + serialize(v, depth + 1) + ";\n"
                for k, v in value.items()
            )
            + "\t" * depth
            + "}"
        )
    if isinstance(value, list):
        return (
            "(\n"
            + "".join("\t" * (depth + 1) + serialize(v, depth + 1) + ",\n" for v in value)
            + "\t" * depth
            + ")"
        )
    return json.dumps(str(value))


def configurations(name: str, settings: dict, *, base=None) -> str:
    entries = []
    for mode in ("Debug", "Release"):
        fields = {
            "buildSettings": {**settings, "SWIFT_OPTIMIZATION_LEVEL": "-Onone" if mode == "Debug" else "-O"},
            "name": mode,
        }
        if base:
            fields["baseConfigurationReference"] = base
        entries.append(obj(name + mode, "XCBuildConfiguration", **fields))
    return obj(
        name + "configs",
        "XCConfigurationList",
        buildConfigurations=entries,
        defaultConfigurationIsVisible=0,
        defaultConfigurationName="Release",
    )


def generate() -> None:
    objects.clear()
    project_id, app_id = uid("project"), uid("Pudge-target")
    base = obj(
        "base-config",
        "PBXFileReference",
        lastKnownFileType="text.xcconfig",
        path="Config/Base.xcconfig",
        sourceTree="<group>",
    )
    package = obj("core-package", "XCLocalSwiftPackageReference", relativePath="PudgeCore")
    product = obj("core-product", "XCSwiftPackageProductDependency", package=package, productName="PudgeCore")
    core_link = obj("core-link", "PBXBuildFile", productRef=product)
    source_refs = []
    products = []
    targets = []
    target_ids = {name: uid(name + "-target") for name in ("Pudge", "PudgeTests", "PudgeUITests")}
    for name in target_ids:
        build_files = []
        for path in sorted((IOS / name).rglob("*.swift")):
            relative = path.relative_to(IOS).as_posix()
            reference = obj(
                relative,
                "PBXFileReference",
                lastKnownFileType="sourcecode.swift",
                path=relative,
                sourceTree="<group>",
            )
            source_refs.append(reference)
            build_files.append(obj(relative + "-build", "PBXBuildFile", fileRef=reference))
        app = name == "Pudge"
        ext = ".app" if app else ".xctest"
        result = obj(
            name + "-product",
            "PBXFileReference",
            explicitFileType="wrapper.application" if app else "wrapper.cfbundle",
            path=name + ext,
            sourceTree="BUILT_PRODUCTS_DIR",
        )
        products.append(result)
        resources = []
        if app:
            reference = obj(
                "assets",
                "PBXFileReference",
                lastKnownFileType="folder.assetcatalog",
                path="Pudge/Assets.xcassets",
                sourceTree="<group>",
            )
            source_refs.append(reference)
            resources.append(obj("assets-build", "PBXBuildFile", fileRef=reference))
        phases = [
            obj(
                name + "-sources",
                "PBXSourcesBuildPhase",
                buildActionMask=2147483647,
                files=build_files,
                runOnlyForDeploymentPostprocessing=0,
            ),
            obj(
                name + "-frameworks",
                "PBXFrameworksBuildPhase",
                buildActionMask=2147483647,
                files=[core_link] if app else [],
                runOnlyForDeploymentPostprocessing=0,
            ),
            obj(
                name + "-resources",
                "PBXResourcesBuildPhase",
                buildActionMask=2147483647,
                files=resources,
                runOnlyForDeploymentPostprocessing=0,
            ),
        ]
        settings = {
            "PRODUCT_NAME": "$(TARGET_NAME)",
            "SWIFT_VERSION": "5.0",
            "IPHONEOS_DEPLOYMENT_TARGET": "18.0",
            "TARGETED_DEVICE_FAMILY": "1,2",
            "CODE_SIGN_STYLE": "Automatic",
            "SDKROOT": "iphoneos",
            "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
            "SUPPORTS_MACCATALYST": "NO",
            "PRODUCT_BUNDLE_IDENTIFIER": "$(PUDGE_IOS_BUNDLE_ID)" + ("" if app else "." + name.lower()),
        }
        if app:
            settings.update(
                {
                    "INFOPLIST_FILE": "Pudge/Info.plist",
                    "ASSETCATALOG_COMPILER_APPICON_NAME": "AppIcon",
                    "CURRENT_PROJECT_VERSION": "1",
                    "MARKETING_VERSION": "0.7.30",
                    "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
                    "ENABLE_TESTABILITY": "YES",
                }
            )
        else:
            settings.update(
                {
                    "GENERATE_INFOPLIST_FILE": "YES",
                    "TEST_TARGET_NAME": "Pudge",
                    "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks @loader_path/Frameworks",
                }
            )
            if name == "PudgeTests":
                settings.update(
                    {"TEST_HOST": "$(BUILT_PRODUCTS_DIR)/Pudge.app/Pudge", "BUNDLE_LOADER": "$(TEST_HOST)"}
                )
        dependencies = []
        if not app:
            proxy = obj(
                name + "-proxy",
                "PBXContainerItemProxy",
                containerPortal=project_id,
                proxyType=1,
                remoteGlobalIDString=app_id,
                remoteInfo="Pudge",
            )
            dependencies.append(
                obj(name + "-dependency", "PBXTargetDependency", target=app_id, targetProxy=proxy)
            )
        targets.append(
            obj(
                name + "-target",
                "PBXNativeTarget",
                buildConfigurationList=configurations(name, settings, base=base),
                buildPhases=phases,
                buildRules=[],
                dependencies=dependencies,
                name=name,
                productName=name,
                productReference=result,
                productType="com.apple.product-type.application"
                if app
                else "com.apple.product-type.bundle.unit-test"
                if name == "PudgeTests"
                else "com.apple.product-type.bundle.ui-testing",
                packageProductDependencies=[product] if app else [],
            )
        )
    source_group = obj(
        "sources", "PBXGroup", children=source_refs + [base], name="Sources", sourceTree="<group>"
    )
    product_group = obj("products", "PBXGroup", children=products, name="Products", sourceTree="<group>")
    root_group = obj("root", "PBXGroup", children=[source_group, product_group], sourceTree="<group>")
    obj(
        "project",
        "PBXProject",
        attributes={
            "LastUpgradeCheck": "1640",
            "TargetAttributes": {
                key: {"CreatedOnToolsVersion": "16.4", **({"TestTargetID": app_id} if key != app_id else {})}
                for key in targets
            },
        },
        buildConfigurationList=configurations(
            "project", {"CLANG_ENABLE_MODULES": "YES", "ENABLE_USER_SCRIPT_SANDBOXING": "YES"}
        ),
        compatibilityVersion="Xcode 14.0",
        developmentRegion="en",
        hasScannedForEncodings=0,
        knownRegions=["ru", "en", "Base"],
        mainGroup=root_group,
        productRefGroup=product_group,
        projectDirPath="",
        projectRoot="",
        targets=targets,
        packageReferences=[package],
    )
    directory = IOS / "Pudge.xcodeproj"
    directory.mkdir(exist_ok=True)
    (directory / "project.pbxproj").write_text(
        "// !$*UTF8*$!\n"
        + serialize(
            {
                "archiveVersion": 1,
                "classes": {},
                "objectVersion": 56,
                "objects": objects,
                "rootObject": project_id,
            }
        )
        + "\n"
    )
    scheme = ET.Element("Scheme", LastUpgradeVersion="1640", version="1.3")
    build = ET.SubElement(scheme, "BuildAction", parallelizeBuildables="YES", buildImplicitDependencies="YES")
    entries = ET.SubElement(build, "BuildActionEntries")

    def ref(parent, name):
        ET.SubElement(
            parent,
            "BuildableReference",
            BuildableIdentifier="primary",
            BlueprintIdentifier=target_ids[name],
            BuildableName=name + (".app" if name == "Pudge" else ".xctest"),
            BlueprintName=name,
            ReferencedContainer="container:Pudge.xcodeproj",
        )

    for name in target_ids:
        entry = ET.SubElement(
            entries,
            "BuildActionEntry",
            buildForTesting="YES",
            buildForRunning="YES" if name == "Pudge" else "NO",
            buildForProfiling="YES" if name == "Pudge" else "NO",
            buildForArchiving="YES" if name == "Pudge" else "NO",
            buildForAnalyzing="YES",
        )
        ref(entry, name)
    tests = ET.SubElement(
        scheme,
        "TestAction",
        buildConfiguration="Debug",
        selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB",
        selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB",
        shouldUseLaunchSchemeArgsEnv="YES",
    )
    testables = ET.SubElement(tests, "Testables")
    for name in ("PudgeTests", "PudgeUITests"):
        ref(ET.SubElement(testables, "TestableReference", skipped="NO"), name)
    launch = ET.SubElement(
        scheme,
        "LaunchAction",
        buildConfiguration="Debug",
        selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB",
        selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB",
        launchStyle="0",
        useCustomWorkingDirectory="NO",
        ignoresPersistentStateOnLaunch="NO",
        debugDocumentVersioning="YES",
        debugServiceExtension="internal",
        allowLocationSimulation="YES",
    )
    ref(ET.SubElement(launch, "BuildableProductRunnable", runnableDebuggingMode="0"), "Pudge")
    profile = ET.SubElement(
        scheme,
        "ProfileAction",
        buildConfiguration="Release",
        shouldUseLaunchSchemeArgsEnv="YES",
        savedToolIdentifier="",
        useCustomWorkingDirectory="NO",
        debugDocumentVersioning="YES",
    )
    ref(ET.SubElement(profile, "BuildableProductRunnable", runnableDebuggingMode="0"), "Pudge")
    ET.SubElement(scheme, "AnalyzeAction", buildConfiguration="Debug")
    ET.SubElement(scheme, "ArchiveAction", buildConfiguration="Release", revealArchiveInOrganizer="YES")
    schemes = directory / "xcshareddata/xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    ET.indent(scheme)
    ET.ElementTree(scheme).write(schemes / "Pudge.xcscheme", encoding="utf-8", xml_declaration=True)
    icon = IOS / "Pudge/Assets.xcassets/AppIcon.appiconset"
    icon.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "pudge/assets/app-icon.png", icon / "AppIcon.png")
    (icon / "Contents.json").write_text(
        json.dumps(
            {
                "images": [
                    {"filename": "AppIcon.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}
                ],
                "info": {"author": "xcode", "version": 1},
            },
            indent=2,
        )
    )
    (icon.parent / "Contents.json").write_text('{"info":{"author":"xcode","version":1}}\n')


if __name__ == "__main__":
    generate()
