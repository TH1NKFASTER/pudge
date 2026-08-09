#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
source "$PROJECT_DIR/anime_mpv/brand.env"
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

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS." >&2
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

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
rm -rf "$VENV_DIR"
python3.12 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip

WHEEL_BUILD_DIR=""
if [[ -d "$PROJECT_DIR/.git" ]]; then
  # Development checkout: always build the wheel from the current working tree.
  # This prevents a stale wheel from a previous version from being installed
  # after applying a source patch. Keep build artifacts out of the repository.
  WHEEL_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/${APP_SLUG}-wheel.XXXXXX")"
  trap '[[ -n "${WHEEL_BUILD_DIR:-}" ]] && rm -rf "$WHEEL_BUILD_DIR"' EXIT
  "$VENV_DIR/bin/python" -m pip install --upgrade "setuptools>=75" wheel
  "$VENV_DIR/bin/python" -m pip wheel "$PROJECT_DIR" \
    --no-deps --no-build-isolation -w "$WHEEL_BUILD_DIR"
  WHEEL_CANDIDATES=("$WHEEL_BUILD_DIR"/anime_mpv-*.whl(N))
else
  # Release ZIPs already contain the exact wheel built by GitHub Actions.
  WHEEL_CANDIDATES=("$PROJECT_DIR"/anime_mpv-*.whl(N))
fi
if (( ${#WHEEL_CANDIDATES[@]} != 1 )) || [[ ! -f "${WHEEL_CANDIDATES[1]}" ]]; then
  echo "Installer error: expected exactly one anime_mpv wheel." >&2
  exit 1
fi
WHEEL_PATH="${WHEEL_CANDIDATES[1]}"

# Install runtime dependencies from the wheel metadata, including the optional
# subtitle synchronization stack. Then force-reinstall the exact bundled wheel
# so a rebuilt ZIP can never leave stale or metadata-only package contents.
"$VENV_DIR/bin/python" -m pip install --upgrade "${WHEEL_PATH}[sync]"
"$VENV_DIR/bin/python" -m pip install --force-reinstall --no-deps "$WHEEL_PATH"

# Fail before touching the app bundle if the packaged Python modules are absent
# or the installed code does not match the bundled wheel. Derive the expected
# version from the wheel name so release bumps cannot leave a stale constant.
WHEEL_NAME="${WHEEL_PATH:t}"
EXPECTED_VERSION="${WHEEL_NAME#anime_mpv-}"
EXPECTED_VERSION="${EXPECTED_VERSION%%-*}"
EXPECTED_VERSION="$EXPECTED_VERSION" "$VENV_DIR/bin/python" - <<'PYVERIFY'
import os
from importlib.metadata import version as distribution_version

from anime_mpv.config import load_config
from anime_mpv import __version__

expected = os.environ["EXPECTED_VERSION"]
installed = distribution_version("anime-mpv")
assert installed == expected, (installed, expected)
assert __version__ == expected, (__version__, expected)
assert callable(load_config)
PYVERIFY
ln -sfn "$VENV_DIR/bin/anime-mpv" "$BIN_DIR/$APP_CLI"
ln -sfn "$VENV_DIR/bin/anime-mpv-agent" "$BIN_DIR/$APP_AGENT_CLI"
for legacy_cli in "${LEGACY_CLIS[@]}"; do
  [[ -z "$legacy_cli" || "$legacy_cli" == "$APP_CLI" ]] && continue
  ln -sfn "$VENV_DIR/bin/anime-mpv" "$BIN_DIR/$legacy_cli"
done
for legacy_cli in "${LEGACY_AGENT_CLIS[@]}"; do
  [[ -z "$legacy_cli" || "$legacy_cli" == "$APP_AGENT_CLI" ]] && continue
  ln -sfn "$VENV_DIR/bin/anime-mpv-agent" "$BIN_DIR/$legacy_cli"
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
from anime_mpv.config import load_config, write_config

path = Path(os.environ["CONFIG_PATH"])
config = load_config(path)
config.tools.mpv = os.environ["MPV_BIN"]
config.tools.ffmpeg = os.environ["FFMPEG_BIN"]
config.tools.ffprobe = os.environ["FFPROBE_BIN"]
config.tools.alass = os.environ["ALASS_BIN"]
config.aria2.enabled = True
config.aria2.binary = os.environ["ARIA2_BIN"]
write_config(config, path)
PYCONFIG

if [[ ":$PATH:" != *":$BIN_DIR:"* ]] && ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.zshrc" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zshrc"
fi

# Build a real native app bundle. An AppleScript wrapper launches a detached
# Homebrew Python process, which macOS labels as "Python" in Dock and Cmd+Tab.
# PyInstaller gives the Cocoa process an actual Anime MPV bundle/executable.
"$VENV_DIR/bin/python" -m pip install --upgrade "pyinstaller>=6.10,<7"

BUILD_DIR="$DATA_DIR/app-build"
PACKAGE_DIR="$("$VENV_DIR/bin/python" - <<'PYPACKAGE'
from pathlib import Path
import anime_mpv
print(Path(anime_mpv.__file__).resolve().parent)
PYPACKAGE
)"
ICON_SOURCE="$PACKAGE_DIR/assets/app-icon.png"
ICONSET="$BUILD_DIR/AnimeMPV.iconset"
ICNS_PATH="$BUILD_DIR/AnimeMPV.icns"
APP_ENTRY="$BUILD_DIR/anime_mpv_app.py"
HOOK_DIR="$BUILD_DIR/hooks"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$ICONSET" "$HOOK_DIR"

# ffsubsync depends on the importable module `webrtcvad`, but modern Python
# installs it from the distribution `webrtcvad-wheels`. PyInstaller's stock
# hook still requests metadata for the old distribution name and aborts the
# whole app build. Override only that hook and copy the metadata that is
# actually installed.
cat > "$HOOK_DIR/hook-webrtcvad.py" <<'PYHOOK'
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
PYHOOK

if [[ -f "$ICON_SOURCE" ]]; then
  for spec in \
    "16 icon_16x16.png" "32 icon_16x16@2x.png" \
    "32 icon_32x32.png" "64 icon_32x32@2x.png" \
    "128 icon_128x128.png" "256 icon_128x128@2x.png" \
    "256 icon_256x256.png" "512 icon_256x256@2x.png" \
    "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
    size="${spec%% *}"
    name="${spec#* }"
    sips -z "$size" "$size" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$ICNS_PATH"
fi

APP_ENTRY="$APP_ENTRY" VENV_PYTHON="$VENV_DIR/bin/python" CONFIG_PATH="$CONFIG_PATH" SEVENZIP_BIN="$SEVENZIP_BIN" ARIA2_BIN="$ARIA2_BIN" \
  "$VENV_DIR/bin/python" - <<'PYAPP'
import os
from pathlib import Path

entry = Path(os.environ["APP_ENTRY"])
python_path = os.environ["VENV_PYTHON"]
config_path = os.environ["CONFIG_PATH"]
sevenzip_path = os.environ["SEVENZIP_BIN"]
aria2_path = os.environ["ARIA2_BIN"]
entry.write_text(
    "import os\n"
    "import sys\n"
    "from pathlib import Path\n"
    f"os.environ['ANIME_MPV_PYTHON'] = {python_path!r}\n"
    f"os.environ['ANIME_MPV_CONFIG'] = {config_path!r}\n"
    f"os.environ['ANIME_MPV_7ZIP'] = {sevenzip_path!r}\n"
    f"os.environ['ANIME_MPV_ARIA2C'] = {aria2_path!r}\n"
    "from anime_mpv.notifications import maybe_handle_notification_helper\n"
    "notification_result = maybe_handle_notification_helper(sys.argv[1:])\n"
    "if notification_result is not None:\n"
    "    raise SystemExit(notification_result)\n"
    "from anime_mpv.app_ui import launch_app\n"
    "raise SystemExit(launch_app(Path(os.environ['ANIME_MPV_CONFIG'])))\n",
    encoding="utf-8",
)
PYAPP

pkill -f "anime_mpv.cli --app" >/dev/null 2>&1 || true
for app_name in "$APP_NAME" "${LEGACY_NAMES[@]}"; do
  [[ -z "$app_name" ]] && continue
  pkill -f "$APP_DIR/$app_name.app/Contents/MacOS/$app_name" >/dev/null 2>&1 || true
done
sleep 1
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
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
rm -rf "$APP_PATH" "$OLD_SETTINGS_APP"

PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
  --osx-bundle-identifier "$APP_BUNDLE_ID"
  --distpath "$BUILD_DIR/dist"
  --workpath "$BUILD_DIR/work"
  --specpath "$BUILD_DIR/spec"
  --collect-all anime_mpv
  --collect-all webview
  --hidden-import webview.platforms.cocoa
  --hidden-import UserNotifications
  --additional-hooks-dir "$HOOK_DIR"
  --exclude-module webview.platforms.android
)
if [[ -f "$ICNS_PATH" ]]; then
  PYINSTALLER_ARGS+=(--icon "$ICNS_PATH")
fi
"$VENV_DIR/bin/python" -m PyInstaller "${PYINSTALLER_ARGS[@]}" "$APP_ENTRY"
mv "$BUILD_DIR/dist/$APP_NAME.app" "$APP_PATH"

PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable $APP_NAME" "$PLIST" >/dev/null 2>&1 || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleExecutable string $APP_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleName $APP_NAME" "$PLIST" >/dev/null 2>&1 || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleName string $APP_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $APP_NAME" "$PLIST" >/dev/null 2>&1 || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $APP_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $APP_BUNDLE_ID" "$PLIST" >/dev/null 2>&1 || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $APP_BUNDLE_ID" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :LSMultipleInstancesProhibited" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :LSMultipleInstancesProhibited bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSHighResolutionCapable" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSDownloadsFolderUsageDescription" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :NSDownloadsFolderUsageDescription string $APP_NAME accesses Downloads only when you select it as the external subtitle folder." "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSDocumentsFolderUsageDescription" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :NSDocumentsFolderUsageDescription string $APP_NAME needs access to the folders selected for the anime library." "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :NSDesktopFolderUsageDescription" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :NSDesktopFolderUsageDescription string $APP_NAME accesses Desktop only when you explicitly select a folder there." "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :CFBundleDocumentTypes" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string Video" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Alternate" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions array" "$PLIST"
idx=0
for ext in mkv mp4 m4v avi mov webm ts m2ts mts wmv flv ogv mpg mpeg; do
  /usr/libexec/PlistBuddy -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions:$idx string $ext" "$PLIST"
  idx=$((idx + 1))
done

codesign --force --deep --sign - "$APP_PATH" >/dev/null 2>&1 || true

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
    <string>anime_mpv.agent</string>
    <string>--scheduled</string>
    <string>--config</string>
    <string>$CONFIG_PATH</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/$APP_SLUG-agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$APP_SLUG-agent-error.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$BIN_DIR</string>
    <key>ANIME_MPV_7ZIP</key><string>$SEVENZIP_BIN</string>
    <key>ANIME_MPV_ARIA2C</key><string>$ARIA2_BIN</string>
    <key>ANIME_MPV_NOTIFICATION_HELPER</key><string>$APP_PATH/Contents/MacOS/$APP_NAME</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$AGENT_PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" >/dev/null 2>&1 || true

printf '%s\n' "$EXPECTED_VERSION" > "$DATA_DIR/installed-version.txt"

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
