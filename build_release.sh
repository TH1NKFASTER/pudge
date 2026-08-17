#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
source "$PROJECT_DIR/pudge/brand.env"

TRIAL_KEY_ASSET="$PROJECT_DIR/pudge/assets/jimaku-trial-key"
remove_trial_asset=0
if [[ -n "${PUDGE_TRIAL_JIMAKU_API_KEY:-}" ]]; then
  printf '%s' "$PUDGE_TRIAL_JIMAKU_API_KEY" > "$TRIAL_KEY_ASSET"
  chmod 600 "$TRIAL_KEY_ASSET"
  remove_trial_asset=1
fi
cleanup_trial_asset() {
  if [[ "$remove_trial_asset" == "1" ]]; then
    rm -f "$TRIAL_KEY_ASSET"
  fi
}
trap cleanup_trial_asset EXIT

VERSION=$(python - <<'PY'
from pudge import __version__
print(__version__)
PY
)

rm -rf build dist pudge.egg-info
find pudge tests -type d -name __pycache__ -prune -exec rm -rf {} +
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  python -m pytest -q
fi
find pudge tests -type d -name __pycache__ -prune -exec rm -rf {} +
python -m pip wheel . --no-deps --no-build-isolation -w dist
command -v uv >/dev/null 2>&1 || { echo "uv is required to build a release" >&2; exit 1; }
uv lock
uv export --locked --extra sync --no-dev --no-emit-project --format requirements.txt --output-file dist/release-requirements.txt

STAGE="$PROJECT_DIR/dist/release/$APP_SLUG"
rm -rf "$PROJECT_DIR/dist/release"
mkdir -p "$STAGE"
cp -R pudge tests "$STAGE/"
cp -R docs "$STAGE/"
cp install.sh README.md config.example.toml pyproject.toml build_release.sh rename_brand.py brand_migration.py \
  LICENSE SECURITY.md CONTRIBUTING.md DEVELOPMENT.md RELEASING.md CHANGELOG.md "$STAGE/"
cp "dist/pudge-${VERSION}-py3-none-any.whl" "$STAGE/"
cp "dist/release-requirements.txt" "$STAGE/"
chmod +x "$STAGE/install.sh" "$STAGE/build_release.sh"

(
  cd "$PROJECT_DIR/dist/release"
  zip -qr "../${APP_SLUG}-macos-v${VERSION}.zip" "$APP_SLUG"
)

ARCHIVE="$PROJECT_DIR/dist/${APP_SLUG}-macos-v${VERSION}.zip"
shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"

echo "$ARCHIVE"
