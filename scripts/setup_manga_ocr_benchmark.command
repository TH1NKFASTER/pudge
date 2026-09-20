#!/bin/bash
set -Eeuo pipefail

DATA_ROOT="${PUDGE_BENCH_DATA:-$HOME/Downloads/pudge-bench-data}"
JMANGA_REPO="${JMANGA_REPO:-$DATA_ROOT/JMangaBench_Mixed-repo}"
JMANGA_BENCHMARK="${JMANGA_BENCHMARK:-$DATA_ROOT/JMangaBench_Mixed}"
MANGA109S_ROOT="${MANGA109S_ROOT:-$DATA_ROOT/Manga109s_released_2026_05_21}"
MANGA109_FULL_ROOT="${MANGA109_ROOT:-$DATA_ROOT/Manga109-v2026}"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$HOME/Downloads/pudge-manga-benchmark-setup-$STAMP.log"

mkdir -p "$HOME/Downloads" "$DATA_ROOT/results" "$MANGA109_FULL_ROOT"
exec > >(tee -a "$LOG") 2>&1

on_exit() {
  local code=$?
  if (( code != 0 )); then
    echo "FAILED. Log: $LOG" >&2
    /usr/bin/open -R "$LOG" >/dev/null 2>&1 || true
  else
    echo "Setup log: $LOG"
  fi
  return "$code"
}
trap on_exit EXIT

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 2; }
command -v uv >/dev/null 2>&1 || { echo "uv is required (https://docs.astral.sh/uv/)" >&2; exit 2; }

if [[ ! -d "$JMANGA_REPO/.git" ]]; then
  if [[ -e "$JMANGA_REPO" ]]; then
    echo "Path exists but is not a JMangaBench git checkout: $JMANGA_REPO" >&2
    exit 2
  fi
  git clone https://github.com/muscgab/JMangaBench_Mixed.git "$JMANGA_REPO"
else
  echo "JMangaBench repo already exists: $JMANGA_REPO"
fi

(
  cd "$JMANGA_REPO"
  uv sync --frozen
  uv run python tools/verify_release.py --release-root .
)

if [[ -d "$JMANGA_BENCHMARK" ]]; then
  echo "JMangaBench already built: $JMANGA_BENCHMARK"
elif [[ -d "$MANGA109S_ROOT/images" ]]; then
  echo "Building JMangaBench from: $MANGA109S_ROOT"
  (
    cd "$JMANGA_REPO"
    uv run python -m jmangabench build \
      --manga109s-root "$MANGA109S_ROOT" \
      --output "$JMANGA_BENCHMARK" \
      --workers "${JMANGA_WORKERS:-10}"
  )
else
  cat <<EOF
JMangaBench code is ready, but Manga109-s pixels are not present yet.
Put the separately obtained Manga109-v2026/Manga109-s release here:
  $MANGA109S_ROOT
Expected at minimum:
  $MANGA109S_ROOT/images/
Then rerun this script.
EOF
fi

if [[ ! -d "$MANGA109_FULL_ROOT/images" || ! -d "$MANGA109_FULL_ROOT/annotations" ]]; then
  cat <<EOF

For the Pudge full-page benchmark, place the authorized Manga109-v2026 data at:
  $MANGA109_FULL_ROOT/images/
  $MANGA109_FULL_ROOT/annotations/
EOF
fi

cat <<EOF

Benchmark data root:
  $DATA_ROOT
JMangaBench repo:
  $JMANGA_REPO
JMangaBench built data:
  $JMANGA_BENCHMARK
Manga109 full-page root:
  $MANGA109_FULL_ROOT
EOF

/usr/bin/open "$DATA_ROOT" >/dev/null 2>&1 || true
