# Manga OCR Standardized Benchmark Design

## Goal
Add a reproducible benchmark subsystem for Pudge manga OCR with two layers:

1. **JMangaBench_Mixed** for recognition-only benchmarking using the benchmark's native prediction/evaluation protocol.
2. **Manga109-v2026 full pages** for Pudge end-to-end detection + recognition benchmarking against Manga109 text bounding boxes and transcriptions.

COO/SFX benchmarking is explicitly deferred to a follow-up patch.

## Data and licensing constraints

- Benchmark code lives in the Pudge repository.
- Dataset pixels and locally reconstructed JMangaBench artifacts never live in git or in distributable patch ZIPs.
- Default external data root: `~/Downloads/pudge-bench-data/`.
- JMangaBench_Mixed is expected to be built separately from an authorized Manga109-s/Manga109-v2026 copy using the upstream builder.
- Manga109-v2026 images/annotations are supplied by the user and referenced by path.

## JMangaBench_Mixed adapter

Pudge provides a predictor that:

- reads `<benchmark>/annotations.jsonl` in canonical order;
- loads MangaOCR once;
- opens `sample["image"]` for each sample;
- writes exactly one JSON object per sample with `id`, `prediction`, `latency_seconds`, and `error`;
- refuses to overwrite an existing predictions file;
- supports `--limit` for smoke tests while full official evaluation requires the complete output.

Pudge does **not** reimplement the official JMangaBench evaluator. The CLI can optionally invoke the upstream evaluator through a user-supplied JMangaBench repository/python command, but the canonical output remains upstream `report.json` + `per_sample.jsonl`.

## Manga109 full-page evaluator

Pudge parses Manga109 XML directly with the standard library. A GT text annotation contains:

- book title;
- page index and dimensions;
- annotation id;
- transcription;
- normalized bounding box in Pudge coordinates (x from left, y from bottom).

Pudge runs the page-local production OCR pipeline used by the reader:

1. Apple Vision region detection (`MangaService._vision_text_regions`);
2. Pudge MangaOCR worker (`MangaService._ocr_regions`);
3. service-side finalization (`_finalize_recognized_regions`).

Book-level post-processing that requires peer pages or imported-book state (for example repeated Latin-title consensus) is intentionally excluded from v1. This keeps each benchmark page deterministic and independent of traversal order.

No database-backed book import is required for benchmark pages. A temporary Database/MangaService instance is used only to reuse the production OCR code path.

### Matching

Predicted regions and GT text regions are greedily matched by descending IoU with a frozen default threshold of `0.50`. Each GT/prediction may be matched once.

### Metrics

Report:

- detection precision / recall / F1;
- GT count, predicted count, matched count, missed count, false-positive count;
- matched-region CER (micro-average after Unicode NFKC + whitespace removal);
- matched-region exact match;
- end-to-end CER where unmatched GT contributes full reference length as deletions and unmatched predictions contribute normalized prediction length as insertions;
- per-page latency;
- per-page/per-region records for regression inspection.

## Frozen Pudge result schema

Every Pudge-authored benchmark report uses:

`pudge_manga_benchmark/v1`

and records:

- benchmark id (`jmangabench_mixed` metadata wrapper or `manga109_fullpage`);
- Pudge package version;
- OCR worker and generation markers;
- Python/platform metadata;
- command parameters;
- aggregate metrics;
- sample/page result file names.

## Comparison

`compare` accepts two Pudge benchmark result JSON files of the same benchmark/schema. It fails on incompatible benchmark ids or IoU thresholds.

For Manga109 it reports metric deltas and page-level regression/improvement counts based on end-to-end edit distance. For JMangaBench wrappers it compares the copied native upstream metric summary if present.

## CLI / Makefile

Primary CLI:

```bash
python -m pudge.manga_benchmark_cli jmanga-predict ...
python -m pudge.manga_benchmark_cli jmanga-wrap-report ...
python -m pudge.manga_benchmark_cli manga109 ...
python -m pudge.manga_benchmark_cli compare ...
```

Makefile convenience targets use data under `~/Downloads/pudge-bench-data/` by default:

```bash
make benchmark-jmanga
make benchmark-manga109
make benchmark-compare OLD=... NEW=...
```

## Testing

Tests use tiny synthetic images/XML and injected detector/recognizer functions. They must not require Apple Vision, MangaOCR weights, private datasets, network access, or macOS.
