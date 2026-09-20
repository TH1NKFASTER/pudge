# Manga OCR benchmark

Pudge has two benchmark layers:

1. **JMangaBench_Mixed** — recognition-only benchmark. Pudge writes the exact upstream prediction JSONL contract and the upstream JMangaBench evaluator remains authoritative for CER/exact-match/slices.
2. **Manga109-v2026 full-page** — Pudge page-local end-to-end benchmark. Apple Vision detects regions, the normal Pudge MangaOCR worker recognizes them, and Pudge evaluates region detection + recognized text against Manga109 text boxes/transcriptions.

COO / stylized SFX is intentionally separate and will be added later.

## Data directory

Keep benchmark data outside the repository:

```text
~/Downloads/pudge-bench-data/
├── JMangaBench_Mixed-repo/      # clone of muscgab/JMangaBench_Mixed
├── JMangaBench_Mixed/           # locally built benchmark; contains Manga109-derived pixels
├── Manga109-v2026/
│   ├── images/
│   └── annotations/
└── results/
```

Do **not** commit, upload, or include Manga109/JMangaBench image data in Pudge patch ZIPs. Manga109 images must be obtained separately under the dataset's terms.

## One-command local setup

From the Pudge repository on macOS:

```bash
make benchmark-setup
```

This creates `~/Downloads/pudge-bench-data/`, clones the upstream JMangaBench repository, runs its frozen environment/release verification, and builds JMangaBench automatically if the separately obtained Manga109-s release is already present at `~/Downloads/pudge-bench-data/Manga109s_released_2026_05_21/`. It never downloads Manga109 pixels itself.

## Build JMangaBench_Mixed once

Clone the upstream benchmark repository into the data directory:

```bash
cd ~/Downloads/pudge-bench-data
git clone https://github.com/muscgab/JMangaBench_Mixed.git JMangaBench_Mixed-repo
cd JMangaBench_Mixed-repo
uv sync --frozen
```

Obtain the matching Manga109-s / Manga109-v2026 image package separately, then build the benchmark with the upstream builder. Example:

```bash
cd ~/Downloads/pudge-bench-data/JMangaBench_Mixed-repo
uv run python tools/verify_release.py --release-root .
uv run python -m jmangabench build \
  --manga109s-root ~/Downloads/pudge-bench-data/Manga109s_released_2026_05_21 \
  --output ~/Downloads/pudge-bench-data/JMangaBench_Mixed \
  --workers 10
```

The expected built benchmark contains `annotations.jsonl`, `manifest.json`, `images/`, and `masks/`.

## Run JMangaBench

From the Pudge repository:

```bash
make benchmark-jmanga
```

This performs four steps:

1. Pudge/MangaOCR writes `jmanga-predictions.jsonl` plus `jmanga-predictions.jsonl.meta.json`.
2. Upstream `jmangabench validate-predictions` validates IDs/hashes/format.
3. Upstream `jmangabench evaluate` computes the frozen JMangaBench metrics and slices.
4. Pudge wraps the native `report.json` with Pudge version + OCR generation metadata as `jmanga-report.json`.

A quick predictor smoke test can be run directly with `--limit`, but limited predictions are intentionally not valid input to the official full evaluator:

```bash
PYTHONPATH="$PWD" ~/.local/share/pudge/venv/bin/python -m pudge.manga_benchmark_cli jmanga-predict \
  --benchmark ~/Downloads/pudge-bench-data/JMangaBench_Mixed \
  --output ~/Downloads/pudge-bench-data/results/smoke.jsonl \
  --limit 20
```

## Run Manga109 full-page benchmark

Put the authorized Manga109-v2026 package under:

```text
~/Downloads/pudge-bench-data/Manga109-v2026/images/
~/Downloads/pudge-bench-data/Manga109-v2026/annotations/
```

Then:

```bash
make benchmark-manga109
```

Or run a small subset first:

```bash
PYTHONPATH="$PWD" ~/.local/share/pudge/venv/bin/python -m pudge.manga_benchmark_cli manga109 \
  --images-root ~/Downloads/pudge-bench-data/Manga109-v2026/images \
  --annotations-root ~/Downloads/pudge-bench-data/Manga109-v2026/annotations \
  --output-dir ~/Downloads/pudge-bench-data/results/manga109-smoke \
  --book ARMS \
  --max-pages 20
```

Full-page metrics use a frozen default IoU threshold of `0.50` and include:

- detection precision / recall / F1;
- matched/missed/false-positive counts;
- matched-region CER and exact match;
- end-to-end CER including unmatched GT as deletions and false-positive predictions as insertions;
- per-page latency and detailed matching rows in `pages.jsonl`.

Pudge bounding boxes use normalized coordinates with `y=0` at the page bottom; the Manga109 XML parser converts the dataset's top-left pixel boxes accordingly.

The v1 full-page runner deliberately benchmarks the **page-local production path**: Vision detection → MangaOCR worker → service finalization. Book-level post-processing that needs peer pages or imported-book state (for example repeated Latin-title consensus) is excluded, so the result stays deterministic per page and does not depend on benchmark traversal order.

## Compare two runs

```bash
make benchmark-compare \
  OLD=~/Downloads/pudge-bench-data/results/v74/manga109/report.json \
  NEW=~/Downloads/pudge-bench-data/results/v75/manga109/report.json \
  OUT=~/Downloads/pudge-bench-data/results/v74-v75.json
```

Comparison refuses incompatible benchmark ids/schema versions and refuses Manga109 reports produced with different IoU thresholds. For Manga109 it also counts pages whose end-to-end edit distance improved, regressed, or stayed unchanged.

## Result schema

Pudge-authored reports use:

```text
pudge_manga_benchmark/v1
```

Each report records the Pudge version, OCR worker/generation markers, Python/platform metadata, parameters, metrics, and artifact references. JMangaBench predictions have a metadata sidecar so wrapping the upstream report later still records the OCR generation that actually produced those predictions.
