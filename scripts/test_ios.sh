#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
log_root="${PUDGE_IOS_LOG_ROOT:-$HOME/Downloads}"
log_dir="$log_root/pudge-iphone-checks-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$log_dir"

finish() {
  status=$?
  printf "\nLogs: %s\n" "$log_dir"
  if [[ $status -ne 0 && "$(uname -s)" == Darwin ]]; then
    open "$log_dir" || true
  fi
  exit "$status"
}
trap finish EXIT

exec > >(tee "$log_dir/checks.log") 2>&1
cd "$repo_root"

python3 scripts/check_ios_project.py

if [[ "$(uname -s)" != Darwin ]]; then
  echo "Xcode and Apple SDK required; native iOS checks were not run."
  exit 2
fi
command -v xcodebuild >/dev/null || {
  echo "Select a full Xcode installation, not only Command Line Tools."
  echo "Example: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
  exit 2
}

xcodebuild -version
xcrun --sdk iphonesimulator --show-sdk-path
xcodebuild -list -project ios/Pudge.xcodeproj

simulator_id="${PUDGE_IOS_SIMULATOR_ID:-}"
if [[ -z "$simulator_id" ]]; then
  simulator_id="$(python3 - <<'PY'
import json
import subprocess

data = json.loads(subprocess.check_output([
    "xcrun", "simctl", "list", "devices", "available", "-j"
]))

# Prefer an iPhone. Runtime dictionaries are returned newest-first by current
# simctl, but sort defensively by runtime name so a newly installed runtime wins.
for runtime in sorted(data.get("devices", {}), reverse=True):
    devices = data["devices"].get(runtime, [])
    iphones = [d for d in devices if d.get("isAvailable", True) and d.get("name", "").startswith("iPhone")]
    if iphones:
        print(iphones[0]["udid"])
        raise SystemExit(0)
raise SystemExit(1)
PY
)" || true
fi

if [[ -z "$simulator_id" ]]; then
  xcodebuild -showdestinations -project ios/Pudge.xcodeproj -scheme Pudge
  echo "No available iPhone Simulator was found. Install an iOS Simulator runtime in Xcode Settings > Components."
  exit 2
fi

if ! xcrun simctl list devices available | grep -F -- "$simulator_id" >/dev/null; then
  echo "Simulator $simulator_id is unavailable."
  xcrun simctl list devices available
  exit 2
fi

echo "Using simulator: $simulator_id"
xcrun simctl boot "$simulator_id" >/dev/null 2>&1 || true
xcrun simctl bootstatus "$simulator_id" -b

# Simulator verification does not require the user's real App Store bundle ID
# or development team. A deterministic local ID keeps a fresh checkout buildable.
bundle_id="${PUDGE_IOS_BUNDLE_ID:-com.pudge.local.Pudge}"
settings_args=("PUDGE_IOS_BUNDLE_ID=$bundle_id")

xcodebuild -showBuildSettings \
  -project ios/Pudge.xcodeproj \
  -scheme Pudge \
  -destination "platform=iOS Simulator,id=$simulator_id" \
  "${settings_args[@]}" > "$log_dir/build-settings.txt"

resolved_bundle_id="$(awk '/ PRODUCT_BUNDLE_IDENTIFIER = / {print $3; exit}' "$log_dir/build-settings.txt")"
if [[ -z "$resolved_bundle_id" || "$resolved_bundle_id" == *YOUR_* || "$resolved_bundle_id" == *'$('* ]]; then
  echo "Could not resolve a usable simulator bundle identifier."
  exit 2
fi

echo "Bundle ID for simulator checks: $resolved_bundle_id"

# Checkpoints may be moved between directories. Swift module caches can contain
# absolute paths, so always discard build products before the portable core test.
rm -rf ios/PudgeCore/.build
swift test --package-path ios/PudgeCore 2>&1 | tee "$log_dir/swift-core.log"

# Build first so compile/typecheck errors are easy to distinguish from test failures.
xcodebuild \
  -project ios/Pudge.xcodeproj \
  -scheme Pudge \
  -destination "platform=iOS Simulator,id=$simulator_id" \
  -derivedDataPath "$log_dir/DerivedData" \
  CODE_SIGNING_ALLOWED=NO \
  "${settings_args[@]}" \
  build 2>&1 | tee "$log_dir/xcode-build.log"

xcodebuild \
  -project ios/Pudge.xcodeproj \
  -scheme Pudge \
  -destination "platform=iOS Simulator,id=$simulator_id" \
  -derivedDataPath "$log_dir/DerivedData" \
  -resultBundlePath "$log_dir/Pudge.xcresult" \
  CODE_SIGNING_ALLOWED=NO \
  "${settings_args[@]}" \
  test 2>&1 | tee "$log_dir/xcode-test.log"

echo "iOS Xcode checks passed."
