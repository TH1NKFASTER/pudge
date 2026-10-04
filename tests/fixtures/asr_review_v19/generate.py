"""Recreate caption fixtures from public geometry and anonymous glyph slots.

The snapshot contains no original caption text or original-to-glyph mapping.
Its timing, word confidence, ASS overrides and glyph layout retain the
historical regression cases. Run directly, or pass --output to another folder.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def glyphs(slots: list[int | str]) -> str:
    return "".join(chr(slot) if isinstance(slot, int) else slot for slot in slots)


def render(record: dict) -> str:
    kind = record["format"]
    if kind == "srt":
        return "\n\n".join(
            "\n".join([*cue["prefix"], glyphs(cue["glyphs"])])
            for cue in record["cues"]
        ) + "\n"
    if kind == "ass":
        return "\n".join(
            [*record["header"], *(event["prefix"] + glyphs(event["glyphs"])
                                  for event in record["events"])]
        ) + "\n"
    if kind == "json":
        payload = copy.deepcopy(record["payload"])
        for segment in payload["segments"]:
            segment["text"] = glyphs(segment.pop("glyphs"))
            for word in segment.get("words", []):
                word["word"] = glyphs(word.pop("glyphs"))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    raise ValueError(f"Unknown fixture format: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    snapshot = json.loads(gzip.decompress((ROOT / "geometry.json.gz").read_bytes()))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, record in snapshot["files"].items():
        destination = args.output / (name + ".gz")
        data = bytearray(gzip.compress(render(record).encode("utf-8"), mtime=0))
        data[9] = 3  # gzip header OS byte: fixed (macOS zlib writes 19)
        destination.write_bytes(bytes(data))


if __name__ == "__main__":
    main()
