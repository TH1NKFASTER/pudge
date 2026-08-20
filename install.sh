#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
source "$PROJECT_DIR/pudge/brand.env"
LEGACY_NAMES=("${(@s:|:)APP_LEGACY_NAMES}")
LEGACY_SLUGS=("${(@s:|:)APP_LEGACY_SLUGS}")
LEGACY_BUNDLE_IDS=("${(@s:|:)APP_LEGACY_BUNDLE_IDS}")
LEGACY_CLIS=("${(@s:|:)APP_LEGACY_CLIS}")
LEGACY_AGENT_CLIS=("${(@s:|:)APP_LEGACY_AGENT_CLIS}")
APP_AGENT_LABEL="${APP_BUNDLE_ID%.app}.agent"
DATA_DIR="$HOME/.local/share/$APP_SLUG"
VENV_DIR="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/Applications"
APP_PATH="$APP_DIR/$APP_NAME.app"
OLD_SETTINGS_APP="$APP_DIR/$APP_NAME Settings.app"
CONFIG_PATH="$HOME/.config/$APP_SLUG/config.toml"
LOG_DIR="$HOME/Library/Logs"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
AGENT_PLIST="$LAUNCH_AGENTS/$APP_AGENT_LABEL.plist"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

UPDATE_MODE=0
if [[ "${1:-}" == "--update" ]]; then
  UPDATE_MODE=1
  shift
fi
if (( $# > 0 )); then
  echo "Unknown installer argument: $1" >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS." >&2
  exit 1
fi

BREW_BIN=""
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [[ -x "$candidate" ]]; then
    BREW_BIN="$candidate"
    break
  fi
done
if [[ -z "$BREW_BIN" ]] && command -v brew >/dev/null 2>&1; then
  BREW_BIN="$(command -v brew)"
fi
if [[ -z "$BREW_BIN" ]]; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

# GUI-launched apps do not inherit the user's login-shell PATH on macOS.
# Put the discovered Homebrew prefix first so brew-provided python/tools below
# resolve the same way whether install.sh is launched from Terminal or Pudge.
export PATH="${BREW_BIN:h}:$PATH"

for formula in mpv ffmpeg alass sevenzip aria2 python@3.12 python-tk@3.12; do
  if ! brew list --versions "$formula" >/dev/null 2>&1; then
    echo "Installing $formula..."
    brew install "$formula"
  fi
done

# Product renames are migrations, not fresh installs. Move the old default
# config/data/cache/library locations before reading config so a branding
# change never creates an empty second installation.
python3.12 "$PROJECT_DIR/brand_migration.py" paths \
  --app-name "$APP_NAME" --app-slug "$APP_SLUG" \
  --legacy-names "${APP_LEGACY_NAMES:-}" --legacy-slugs "${APP_LEGACY_SLUGS:-}"

MPV_BIN="$(brew --prefix mpv)/bin/mpv"
FFMPEG_BIN="$(brew --prefix ffmpeg)/bin/ffmpeg"
FFPROBE_BIN="$(brew --prefix ffmpeg)/bin/ffprobe"
ALASS_BIN="$(brew --prefix alass)/bin/alass-cli"
if [[ ! -x "$ALASS_BIN" ]]; then
  ALASS_BIN="$(brew --prefix alass)/bin/alass"
fi
SEVENZIP_BIN="$(brew --prefix sevenzip)/bin/7zz"
if [[ ! -x "$SEVENZIP_BIN" ]]; then
  SEVENZIP_BIN="$(brew --prefix sevenzip)/bin/7z"
fi
if [[ ! -x "$SEVENZIP_BIN" ]]; then
  echo "Installer error: sevenzip was installed but 7zz/7z was not found." >&2
  exit 1
fi
ARIA2_BIN="$(brew --prefix aria2)/bin/aria2c"
if [[ ! -x "$ARIA2_BIN" ]]; then
  echo "Installer error: aria2 was installed but aria2c was not found." >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$BIN_DIR" "$APP_DIR" "$LOG_DIR" "$LAUNCH_AGENTS"

FAST_UPDATE=0
if (( UPDATE_MODE )) && [[ -x "$VENV_DIR/bin/python" ]]; then
  FAST_UPDATE=1
  echo "Fast update: preserving the existing runtime environment."
fi

UPDATE_PACKAGE_BACKUP="$DATA_DIR/update-package-backup"
UPDATE_SITE_PACKAGES=""
WHEEL_BUILD_DIR=""
APP_SWAP_BACKUP=""
cleanup_install() {
  local exit_code=$?
  if (( exit_code != 0 && FAST_UPDATE )) && [[ -n "$UPDATE_SITE_PACKAGES" && -d "$UPDATE_PACKAGE_BACKUP" ]]; then
    echo "Update failed; restoring the previous Pudge package..."
    rm -rf "$UPDATE_SITE_PACKAGES/pudge" "$UPDATE_SITE_PACKAGES"/pudge-*.dist-info(N)
    if [[ -d "$UPDATE_PACKAGE_BACKUP/pudge" ]]; then
      /usr/bin/ditto "$UPDATE_PACKAGE_BACKUP/pudge" "$UPDATE_SITE_PACKAGES/pudge"
    fi
    for info in "$UPDATE_PACKAGE_BACKUP"/pudge-*.dist-info(N); do
      /usr/bin/ditto "$info" "$UPDATE_SITE_PACKAGES/${info:t}"
    done
  fi
  if (( exit_code != 0 )) && [[ -n "${APP_SWAP_BACKUP:-}" && -d "$APP_SWAP_BACKUP" ]]; then
    echo "Install failed; restoring the previous app bundle..."
    rm -rf "$APP_PATH"
    /bin/mv "$APP_SWAP_BACKUP" "$APP_PATH"
  elif (( exit_code == 0 )) && [[ -n "${APP_SWAP_BACKUP:-}" ]]; then
    rm -rf "$APP_SWAP_BACKUP"
  fi
  [[ -n "${WHEEL_BUILD_DIR:-}" ]] && rm -rf "$WHEEL_BUILD_DIR"
  rm -rf "$UPDATE_PACKAGE_BACKUP"
  return "$exit_code"
}
trap cleanup_install EXIT

# Preserve the optional MangaOCR capability across updates. The installer
# recreates the runtime venv, so remember whether OCR was available before
# deleting it. A surviving model-ready marker also counts: in that case the
# Python package must be restored even if the old venv itself is damaged.
MANGA_OCR_WAS_INSTALLED=0
if [[ -x "$VENV_DIR/bin/python" ]]; then
  if CONFIG_PATH="$CONFIG_PATH" "$VENV_DIR/bin/python" - <<'PYMANGAOLD'
import importlib.util
import os
from pathlib import Path

installed = importlib.util.find_spec("manga_ocr") is not None
model_ready = False
try:
    from pudge.config import load_config
    config = load_config(Path(os.environ["CONFIG_PATH"]).expanduser())
    marker = Path(config.paths.cache_dir) / "manga-ocr" / "model-ready.json"
    model_ready = marker.exists()
except Exception:
    pass

raise SystemExit(0 if installed or model_ready else 1)
PYMANGAOLD
  then
    MANGA_OCR_WAS_INSTALLED=1
    echo "MangaOCR detected; it will be preserved across this update."
  fi
fi

if (( FAST_UPDATE )); then
  UPDATE_SITE_PACKAGES="$("$VENV_DIR/bin/python" - <<'PYSITE'
import sysconfig
print(sysconfig.get_paths()["purelib"])
PYSITE
)"
  rm -rf "$UPDATE_PACKAGE_BACKUP"
  mkdir -p "$UPDATE_PACKAGE_BACKUP"
  if [[ -d "$UPDATE_SITE_PACKAGES/pudge" ]]; then
    /usr/bin/ditto "$UPDATE_SITE_PACKAGES/pudge" "$UPDATE_PACKAGE_BACKUP/pudge"
  fi
  for info in "$UPDATE_SITE_PACKAGES"/pudge-*.dist-info(N); do
    /usr/bin/ditto "$info" "$UPDATE_PACKAGE_BACKUP/${info:t}"
  done
else
  rm -rf "$VENV_DIR"
  python3.12 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
fi
if [[ -d "$PROJECT_DIR/.git" ]]; then
  # Development checkout: always build the wheel from the current working tree.
  # This prevents a stale wheel from a previous version from being installed
  # after applying a source patch. Keep build artifacts out of the repository.
  WHEEL_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/${APP_SLUG}-wheel.XXXXXX")"
  "$VENV_DIR/bin/python" -m pip install --upgrade "setuptools>=75" wheel
  "$VENV_DIR/bin/python" -m pip wheel "$PROJECT_DIR" \
    --no-deps --no-build-isolation -w "$WHEEL_BUILD_DIR"
  WHEEL_CANDIDATES=("$WHEEL_BUILD_DIR"/pudge-*.whl(N))
else
  # Release ZIPs already contain the exact wheel built by GitHub Actions.
  WHEEL_CANDIDATES=("$PROJECT_DIR"/pudge-*.whl(N))
fi
if (( ${#WHEEL_CANDIDATES[@]} != 1 )) || [[ ! -f "${WHEEL_CANDIDATES[1]}" ]]; then
  echo "Installer error: expected exactly one pudge wheel." >&2
  exit 1
fi
WHEEL_PATH="${WHEEL_CANDIDATES[1]}"

# Install runtime dependencies from the wheel metadata, including the optional
# subtitle synchronization stack. Then force-reinstall the exact bundled wheel
# so a rebuilt ZIP can never leave stale or metadata-only package contents.
if [[ -f "$PROJECT_DIR/release-requirements.txt" && ! -d "$PROJECT_DIR/.git" ]]; then
  "$VENV_DIR/bin/python" -m pip install --upgrade --require-hashes -r "$PROJECT_DIR/release-requirements.txt"
else
  "$VENV_DIR/bin/python" -m pip install --upgrade "${WHEEL_PATH}[sync]"
fi
"$VENV_DIR/bin/python" -m pip install --force-reinstall --no-deps "$WHEEL_PATH"

if (( MANGA_OCR_WAS_INSTALLED && ! FAST_UPDATE )); then
  echo "Restoring MangaOCR..."
  "$VENV_DIR/bin/python" -m pip install --upgrade "manga-ocr>=0.1.14,<1"
  "$VENV_DIR/bin/python" - <<'PYMANGANEW'
import importlib.util

if importlib.util.find_spec("manga_ocr") is None:
    raise SystemExit("Installer error: MangaOCR was present before update but could not be restored.")
PYMANGANEW
fi

# Fail before touching the app bundle if the packaged Python modules are absent
# or the installed code does not match the bundled wheel. Derive the expected
# version from the wheel name so release bumps cannot leave a stale constant.
WHEEL_NAME="${WHEEL_PATH:t}"
EXPECTED_VERSION="${WHEEL_NAME#pudge-}"
EXPECTED_VERSION="${EXPECTED_VERSION%%-*}"
EXPECTED_VERSION="$EXPECTED_VERSION" "$VENV_DIR/bin/python" -I - <<'PYVERIFY'
import os
from importlib.metadata import version as distribution_version

from pudge.config import load_config
from pudge import __version__

expected = os.environ["EXPECTED_VERSION"]
installed = distribution_version("pudge")
assert installed == expected, (installed, expected)
assert __version__ == expected, (__version__, expected)
assert callable(load_config)
PYVERIFY
ln -sfn "$VENV_DIR/bin/pudge" "$BIN_DIR/$APP_CLI"
ln -sfn "$VENV_DIR/bin/pudge-agent" "$BIN_DIR/$APP_AGENT_CLI"
for legacy_cli in "${LEGACY_CLIS[@]}"; do
  [[ -z "$legacy_cli" || "$legacy_cli" == "$APP_CLI" ]] && continue
  ln -sfn "$VENV_DIR/bin/pudge" "$BIN_DIR/$legacy_cli"
done
for legacy_cli in "${LEGACY_AGENT_CLIS[@]}"; do
  [[ -z "$legacy_cli" || "$legacy_cli" == "$APP_AGENT_CLI" ]] && continue
  ln -sfn "$VENV_DIR/bin/pudge-agent" "$BIN_DIR/$legacy_cli"
done

if [[ ! -f "$CONFIG_PATH" ]]; then
  "$BIN_DIR/$APP_CLI" --init-config
fi

# Rewrite only old *default* branded paths/category values. Custom user paths
# are left intact. This runs after the physical folder migration above.
python3.12 "$PROJECT_DIR/brand_migration.py" config \
  --app-name "$APP_NAME" --app-slug "$APP_SLUG" \
  --legacy-names "${APP_LEGACY_NAMES:-}" --legacy-slugs "${APP_LEGACY_SLUGS:-}" \
  --config "$CONFIG_PATH"

MPV_BIN="$MPV_BIN" FFMPEG_BIN="$FFMPEG_BIN" FFPROBE_BIN="$FFPROBE_BIN" ALASS_BIN="$ALASS_BIN" ARIA2_BIN="$ARIA2_BIN" CONFIG_PATH="$CONFIG_PATH" \
  "$VENV_DIR/bin/python" - <<'PYCONFIG'
import os
from pathlib import Path
from pudge.config import load_config, write_config

path = Path(os.environ["CONFIG_PATH"])
config = load_config(path)
config.tools.mpv = os.environ["MPV_BIN"]
config.tools.ffmpeg = os.environ["FFMPEG_BIN"]
config.tools.ffprobe = os.environ["FFPROBE_BIN"]
config.tools.alass = os.environ["ALASS_BIN"]
# Updating Pudge must not alter the user's torrent-backend choice.
# aria2 remains installed and available, but enabled/disabled is persisted.
config.aria2.binary = os.environ["ARIA2_BIN"]
write_config(config, path)
PYCONFIG

if [[ ":$PATH:" != *":$BIN_DIR:"* ]] && ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.zshrc" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zshrc"
fi

# Ordinary Python/web updates must not replace/re-sign the native launcher.
# macOS Files & Folders permissions are tied to app code identity, so rebuilding
# this tiny shell on every patch caused repeated Downloads-folder prompts.
NATIVE_SHELL_REV=1
NATIVE_SHELL_REV_FILE="$DATA_DIR/native-shell-rev"
PRESERVE_NATIVE_APP=0
if (( FAST_UPDATE )) && [[ "${PUDGE_FORCE_NATIVE_REBUILD:-0}" != "1" ]] \
   && [[ -x "$APP_PATH/Contents/MacOS/$APP_NAME" ]] \
   && [[ -f "$APP_PATH/Contents/Info.plist" ]]; then
  EXISTING_SHELL_REV="$(cat "$NATIVE_SHELL_REV_FILE" 2>/dev/null || echo "$NATIVE_SHELL_REV")"
  if [[ "$EXISTING_SHELL_REV" == "$NATIVE_SHELL_REV" ]]; then
    PRESERVE_NATIVE_APP=1
    echo "Fast update: preserving native app identity and existing macOS folder grants."
  fi
fi

if (( ! PRESERVE_NATIVE_APP )); then
# Build a tiny native app bundle that always runs the managed venv package.
# The .app contains no frozen copy of Pudge, Python stdlib, or third-party
# packages; after an update, the newly installed wheel is the code the GUI runs.
BUILD_DIR="$DATA_DIR/app-build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/dist"

NEW_APP="$BUILD_DIR/dist/$APP_NAME.app"
NEW_CONTENTS="$NEW_APP/Contents"
NEW_MACOS="$NEW_CONTENTS/MacOS"
NEW_RESOURCES="$NEW_CONTENTS/Resources"
mkdir -p "$NEW_MACOS" "$NEW_RESOURCES"

APP_LAUNCHER="$NEW_MACOS/$APP_NAME"
LAUNCHER_SOURCE="$BUILD_DIR/pudge-launcher.m"

LAUNCHER_SOURCE="$LAUNCHER_SOURCE" \
VENV_PYTHON="$VENV_DIR/bin/python" \
CONFIG_PATH="$CONFIG_PATH" \
SEVENZIP_BIN="$SEVENZIP_BIN" \
ARIA2_BIN="$ARIA2_BIN" \
APP_NAME="$APP_NAME" \
python3.12 - <<'PYNATIVELAUNCHER'
import json
import os
from pathlib import Path

source = Path(os.environ["LAUNCHER_SOURCE"])
app_name = json.dumps(os.environ["APP_NAME"])
venv_python = json.dumps(os.environ["VENV_PYTHON"])
config_path = json.dumps(os.environ["CONFIG_PATH"])
sevenzip_bin = json.dumps(os.environ["SEVENZIP_BIN"])
aria2_bin = json.dumps(os.environ["ARIA2_BIN"])

template = r"""#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>
#include <Python.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

@interface PudgeNotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation PudgeNotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler {
    UNNotificationPresentationOptions options =
        UNNotificationPresentationOptionSound |
        UNNotificationPresentationOptionBanner |
        UNNotificationPresentationOptionList;
    completionHandler(options);
}
@end

static int send_notification(const char *subtitle_arg, const char *message_arg) {
    @autoreleasepool {
        NSString *subtitle = [NSString stringWithUTF8String:subtitle_arg ?: ""];
        NSString *message = [NSString stringWithUTF8String:message_arg ?: ""];

        UNUserNotificationCenter *center =
            [UNUserNotificationCenter currentNotificationCenter];
        PudgeNotificationDelegate *delegate =
            [[PudgeNotificationDelegate alloc] init];
        center.delegate = delegate;

        dispatch_semaphore_t settings_sem = dispatch_semaphore_create(0);
        __block BOOL authorized = NO;
        [center getNotificationSettingsWithCompletionHandler:
            ^(UNNotificationSettings *settings) {
                UNAuthorizationStatus status = settings.authorizationStatus;
                authorized =
                    status == UNAuthorizationStatusAuthorized ||
                    status == UNAuthorizationStatusProvisional;
                dispatch_semaphore_signal(settings_sem);
            }];

        if (dispatch_semaphore_wait(
                settings_sem,
                dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) != 0 ||
            !authorized) {
            return 1;
        }

        UNMutableNotificationContent *content =
            [[UNMutableNotificationContent alloc] init];
        content.title = @__APP_NAME__;
        content.subtitle = subtitle;
        content.body = message;
        content.sound = [UNNotificationSound defaultSound];

        UNNotificationRequest *request =
            [UNNotificationRequest requestWithIdentifier:[[NSUUID UUID] UUIDString]
                                                 content:content
                                                 trigger:nil];

        dispatch_semaphore_t delivery_sem = dispatch_semaphore_create(0);
        __block BOOL delivered = NO;
        [center addNotificationRequest:request
                 withCompletionHandler:^(NSError *error) {
                     delivered = error == nil;
                     dispatch_semaphore_signal(delivery_sem);
                 }];

        if (dispatch_semaphore_wait(
                delivery_sem,
                dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) != 0 ||
            !delivered) {
            return 1;
        }

        [[NSRunLoop currentRunLoop]
            runUntilDate:[NSDate dateWithTimeIntervalSinceNow:1.0]];
        (void)delegate;
        return 0;
    }
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        if (argc >= 2 &&
            strcmp(argv[1], "--pudge-native-notification") == 0) {
            if (argc < 4) {
                return 2;
            }
            return send_notification(argv[2], argv[3]);
        }

        unsetenv("TCL_LIBRARY");
        unsetenv("TK_LIBRARY");
        unsetenv("TCLLIBPATH");
        unsetenv("PYTHONHOME");
        unsetenv("PYTHONPATH");
        unsetenv("PYTHONEXECUTABLE");
        unsetenv("_PYI_ARCHIVE_FILE");
        unsetenv("_PYI_PARENT_PROCESS_LEVEL");
        unsetenv("_PYI_APPLICATION_HOME_DIR");

        const char *python = __VENV_PYTHON__;
        setenv("PUDGE_PYTHON", python, 1);
        setenv("PUDGE_CONFIG", __CONFIG_PATH__, 1);
        setenv("PUDGE_7ZIP", __SEVENZIP_BIN__, 1);
        setenv("PUDGE_ARIA2C", __ARIA2_BIN__, 1);

        NSString *icon_path =
            [[NSBundle mainBundle] pathForResource:@"AppIcon" ofType:@"icns"];
        if (icon_path != nil) {
            setenv("PUDGE_APP_ICON", [icon_path fileSystemRepresentation], 1);
        }

        // Keep Python in the native Pudge process instead of exec()ing the
        // Homebrew Python.app executable. macOS derives NSRunningApplication's
        // bundle/icon from the running executable; exec() was why Force Quit
        // still showed the Python rocket even after AppKit's Dock icon changed.
        PyConfig config;
        PyConfig_InitPythonConfig(&config);
        config.parse_argv = 0;

        PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, python);
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesString(&config, &config.executable, python);
        }
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetBytesArgv(&config, argc, argv);
        }
        if (!PyStatus_Exception(status)) {
            status = PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry");
        }
        if (!PyStatus_Exception(status)) {
            status = Py_InitializeFromConfig(&config);
        }
        PyConfig_Clear(&config);
        if (PyStatus_Exception(status)) {
            Py_ExitStatusException(status);
        }
        return Py_RunMain();
    }
}
"""

template = (
    template
    .replace("__APP_NAME__", app_name)
    .replace("__VENV_PYTHON__", venv_python)
    .replace("__CONFIG_PATH__", config_path)
    .replace("__SEVENZIP_BIN__", sevenzip_bin)
    .replace("__ARIA2_BIN__", aria2_bin)
)
source.write_text(template, encoding="utf-8")
PYNATIVELAUNCHER

PYTHON_CONFIG="$($VENV_DIR/bin/python - <<'PYTHONCONFIG'
import os
import sysconfig
version = str(sysconfig.get_config_var("VERSION") or "")
bindir = str(sysconfig.get_config_var("BINDIR") or "")
print(os.path.join(bindir, f"python{version}-config"))
PYTHONCONFIG
)"
if [[ ! -x "$PYTHON_CONFIG" ]]; then
  echo "Python embed config not found: $PYTHON_CONFIG" >&2
  exit 1
fi
PYTHON_EMBED_CFLAGS="$($PYTHON_CONFIG --embed --cflags)"
PYTHON_EMBED_LDFLAGS="$($PYTHON_CONFIG --embed --ldflags)"

/usr/bin/clang \
  -fobjc-arc \
  -fblocks \
  ${=PYTHON_EMBED_CFLAGS} \
  -framework Foundation \
  -framework UserNotifications \
  "$LAUNCHER_SOURCE" \
  ${=PYTHON_EMBED_LDFLAGS} \
  -o "$APP_LAUNCHER"

chmod 755 "$APP_LAUNCHER"

PLIST="$NEW_CONTENTS/Info.plist"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$APP_BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$EXPECTED_VERSION</string>
  <key>CFBundleVersion</key><string>$EXPECTED_VERSION</string>
  <key>LSMultipleInstancesProhibited</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSDownloadsFolderUsageDescription</key><string>$APP_NAME accesses Downloads only when you select it as the external subtitle folder.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>$APP_NAME needs access to the folders selected for the anime library.</string>
  <key>NSDesktopFolderUsageDescription</key><string>$APP_NAME accesses Desktop only when you explicitly select a folder there.</string>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>Video</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSHandlerRank</key><string>Alternate</string>
      <key>CFBundleTypeExtensions</key>
      <array>
        <string>mkv</string><string>mp4</string><string>m4v</string>
        <string>avi</string><string>mov</string><string>webm</string>
        <string>ts</string><string>m2ts</string><string>mts</string>
        <string>wmv</string><string>flv</string><string>ogv</string>
        <string>mpg</string><string>mpeg</string>
      </array>
    </dict>
  </array>
</dict>
</plist>
PLIST

ICON_SOURCE="$PROJECT_DIR/pudge/web/app-logo.png"
ICONSET="$BUILD_DIR/AppIcon.iconset"
ICNS_PATH="$NEW_RESOURCES/AppIcon.icns"
if [[ -f "$ICON_SOURCE" ]]; then
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  icon_ok=1
  for spec in \
    "16 icon_16x16.png" "32 icon_16x16@2x.png" \
    "32 icon_32x32.png" "64 icon_32x32@2x.png" \
    "128 icon_128x128.png" "256 icon_128x128@2x.png" \
    "256 icon_256x256.png" "512 icon_256x256@2x.png" \
    "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
    size="${spec%% *}"
    name="${spec#* }"
    if ! sips -z "$size" "$size" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null 2>&1; then
      icon_ok=0
      break
    fi
  done
  if (( icon_ok )) && iconutil -c icns "$ICONSET" -o "$ICNS_PATH" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon.icns" "$PLIST" >/dev/null 2>&1 || true
  fi
fi

# Replacement is complete before the working app is touched.
pkill -f "pudge.cli --app" >/dev/null 2>&1 || true
pkill -f "pudge.app_entry" >/dev/null 2>&1 || true
for app_name in "$APP_NAME" "${LEGACY_NAMES[@]}"; do
  [[ -z "$app_name" ]] && continue
  pkill -f "$APP_DIR/$app_name.app/Contents/MacOS/$app_name" >/dev/null 2>&1 || true
done
sleep 1

for legacy_name in "${LEGACY_NAMES[@]}"; do
  [[ -z "$legacy_name" || "$legacy_name" == "$APP_NAME" ]] && continue
  legacy_app="$APP_DIR/$legacy_name.app"
  if [[ -e "$legacy_app" || -L "$legacy_app" ]]; then
    "$LSREGISTER" -u "$legacy_app" >/dev/null 2>&1 || true
    rm -rf "$legacy_app"
  fi
  rm -rf "$APP_DIR/$legacy_name Settings.app"
done
for legacy_bundle in "${LEGACY_BUNDLE_IDS[@]}"; do
  [[ -z "$legacy_bundle" || "$legacy_bundle" == "$APP_BUNDLE_ID" ]] && continue
  legacy_agent_label="${legacy_bundle%.app}.agent"
  legacy_agent_plist="$LAUNCH_AGENTS/$legacy_agent_label.plist"
  launchctl bootout "gui/$(id -u)" "$legacy_agent_plist" >/dev/null 2>&1 || true
  rm -f "$legacy_agent_plist"
done
rm -rf "$OLD_SETTINGS_APP"

APP_SWAP_BACKUP="$BUILD_DIR/$APP_NAME.app.before-swap"
rm -rf "$APP_SWAP_BACKUP"
if [[ -d "$APP_PATH" ]]; then
  /bin/mv "$APP_PATH" "$APP_SWAP_BACKUP"
fi
if ! /bin/mv "$NEW_APP" "$APP_PATH"; then
  rm -rf "$APP_PATH"
  if [[ -d "$APP_SWAP_BACKUP" ]]; then
    /bin/mv "$APP_SWAP_BACKUP" "$APP_PATH"
  fi
  echo "Installer error: could not replace $APP_NAME.app." >&2
  exit 1
fi

codesign --force --deep --sign - "$APP_PATH" >/dev/null 2>&1 || true
fi
printf '%s\n' "$NATIVE_SHELL_REV" > "$NATIVE_SHELL_REV_FILE"

cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$APP_AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/python</string>
    <string>-m</string>
    <string>pudge.agent</string>
    <string>--scheduled</string>
    <string>--config</string>
    <string>$CONFIG_PATH</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/$APP_SLUG-agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$APP_SLUG-agent-error.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$BIN_DIR</string>
    <key>PUDGE_7ZIP</key><string>$SEVENZIP_BIN</string>
    <key>PUDGE_ARIA2C</key><string>$ARIA2_BIN</string>
    <key>PUDGE_NOTIFICATION_HELPER</key><string>$APP_PATH/Contents/MacOS/$APP_NAME</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$AGENT_PLIST" >/dev/null 2>&1 || true

printf '%s\n' "$EXPECTED_VERSION" > "$DATA_DIR/installed-version.txt"

# Let the in-app updater distinguish a packaged release from a development
# checkout. A dirty checkout is never replaced by a release archive or pulled
# automatically; the updater will ask the developer to handle it manually.
INSTALL_CHANNEL="release"
INSTALL_SOURCE=""
INSTALL_REVISION=""
if [[ -e "$PROJECT_DIR/.git" ]]; then
  INSTALL_CHANNEL="development"
  INSTALL_SOURCE="$PROJECT_DIR"
  INSTALL_REVISION="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || true)"
fi
INSTALL_CHANNEL="$INSTALL_CHANNEL" INSTALL_SOURCE="$INSTALL_SOURCE" INSTALL_REVISION="$INSTALL_REVISION" DATA_DIR="$DATA_DIR" \
  python3.12 - <<'PYINSTALLSOURCE'
import json
import os
from pathlib import Path

target = Path(os.environ["DATA_DIR"]) / "install-source.json"
target.write_text(
    json.dumps(
        {
            "channel": os.environ["INSTALL_CHANNEL"],
            "source_path": os.environ["INSTALL_SOURCE"],
            "source_revision": os.environ["INSTALL_REVISION"],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
PYINSTALLSOURCE

"$LSREGISTER" -f "$APP_PATH" >/dev/null 2>&1 || true

# Keep old Dock pins functional after a visible rename. The compatibility app
# links are hidden in Finder, while LaunchServices/Dock resolve them to the new
# bundle and read its new CFBundleDisplayName.
for legacy_name in "${LEGACY_NAMES[@]}"; do
  [[ -z "$legacy_name" || "$legacy_name" == "$APP_NAME" ]] && continue
  legacy_app="$APP_DIR/$legacy_name.app"
  rm -rf "$legacy_app"
  ln -s "$APP_PATH" "$legacy_app"
  chflags hidden "$legacy_app" >/dev/null 2>&1 || true
done
killall Dock >/dev/null 2>&1 || true

if [[ ! -d "/Applications/qbittorrent.app" && ! -d "/Applications/qBittorrent.app" ]] && ! brew list --cask qbittorrent >/dev/null 2>&1; then
  echo
  echo "qBittorrent was not found. $APP_NAME will use its managed aria2 backend."
fi

echo
echo "Installed:"
echo "  App: $APP_PATH"
echo "  CLI: $BIN_DIR/$APP_CLI"
echo "  Agent: $AGENT_PLIST"
echo "  Config: $CONFIG_PATH"
echo
echo "Open $APP_NAME.app. Diagnostics: $BIN_DIR/$APP_CLI --doctor"
