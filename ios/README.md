# Pudge iPhone — first Xcode verification

The iPhone client currently targets iOS 18 and is verified structurally on non-macOS environments. The first Apple SDK typecheck and Simulator run must be done on a Mac with full Xcode installed.

## One-command check

From the repository root:

```bash
./run-ios-xcode-checks.command
```

The script:

1. validates the hand-written Xcode project;
2. finds an available iPhone Simulator automatically;
3. boots it if needed;
4. runs `swift test` for `PudgeCore`;
5. builds the native app through the iOS Simulator SDK;
6. runs unit and UI tests;
7. stores all logs and the `.xcresult` bundle under `~/Downloads/pudge-iphone-checks-<timestamp>/`.

If the check fails, Finder opens that log directory automatically.

### Optional overrides

Use a specific simulator:

```bash
PUDGE_IOS_SIMULATOR_ID="<UDID>" ./run-ios-xcode-checks.command
```

Use a custom bundle identifier during Simulator verification:

```bash
PUDGE_IOS_BUNDLE_ID="com.example.Pudge" ./run-ios-xcode-checks.command
```

A real Apple development team is **not** required for Simulator checks.

## Device signing

For a physical iPhone, copy:

```text
ios/Config/Signing.example.xcconfig
```

to:

```text
ios/Config/Signing.local.xcconfig
```

and set your own `PUDGE_IOS_BUNDLE_ID` and `DEVELOPMENT_TEAM`. Do not commit that local file.
