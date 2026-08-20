#!/usr/bin/env python3
"""Summarize historical torrent selection outcomes without changing state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def report(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database_path.expanduser()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT provider,score,listed_seeders,metadata_seconds,outcome "
            "FROM torrent_observations ORDER BY observed_at"
        ).fetchall()
    finally:
        connection.close()
    providers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"observations": 0, "winners": 0, "metadata_seconds": []}
    )
    score_bands: dict[str, dict[str, int]] = defaultdict(lambda: {"observations": 0, "winners": 0})
    for row in rows:
        provider = str(row["provider"] or "unknown")
        provider_row = providers[provider]
        provider_row["observations"] += 1
        winner = str(row["outcome"] or "") == "winner"
        provider_row["winners"] += int(winner)
        if row["metadata_seconds"] is not None:
            provider_row["metadata_seconds"].append(float(row["metadata_seconds"]))
        score = float(row["score"] or 0.0)
        band = f"{int(score // 25) * 25:03d}-{int(score // 25) * 25 + 24:03d}"
        score_bands[band]["observations"] += 1
        score_bands[band]["winners"] += int(winner)
    for item in providers.values():
        samples = sorted(item.pop("metadata_seconds"))
        item["winner_rate"] = round(item["winners"] / max(1, item["observations"]), 4)
        item["median_metadata_seconds"] = round(samples[len(samples) // 2], 3) if samples else None
    for item in score_bands.values():
        item["winner_rate"] = round(item["winners"] / max(1, item["observations"]), 4)
    return {
        "observations": len(rows),
        "providers": dict(sorted(providers.items())),
        "score_bands": dict(sorted(score_bands.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.database), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
