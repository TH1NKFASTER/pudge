// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "PudgeCore",
    platforms: [.iOS("18.0"), .macOS(.v13)],
    products: [.library(name: "PudgeCore", targets: ["PudgeCore"])],
    targets: [
        .systemLibrary(name: "CSQLite", pkgConfig: "sqlite3", providers: [.apt(["libsqlite3-dev"])]),
        .target(name: "PudgeCore", dependencies: ["CSQLite"]),
        .testTarget(name: "PudgeCoreTests", dependencies: ["PudgeCore"])
    ])
