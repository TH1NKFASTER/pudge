"""Describe the delivered cue clock (including local repairs)."""
from itertools import pairwise

from ..subtitle_formats import convert_to_plain_srt, parse_srt


def final_timing_runs(raw, output):
    if raw.suffix.lower() != ".srt":
        raw, _ = convert_to_plain_srt(raw, output.parent, force=False, verbose=False)
    source, final = parse_srt(raw), parse_srt(output)
    if len(source) != len(final) or not source:
        return []
    runs = []
    for index, (before, after) in enumerate(zip(source, final)):
        if before[2] != after[2]:
            return []
        offset = round(after[0] - before[0], 3)
        if not runs or abs(offset - runs[-1]["offset_seconds"]) > .020:
            runs.append({"first_cue": index + 1, "last_cue": index + 1,
                         "source_start": before[0], "source_end": before[1],
                         "offset_seconds": offset})
        else:
            runs[-1].update(last_cue=index + 1, source_end=before[1])
    for left, right in pairwise(runs):
        left["boundary_seconds"] = round((left["source_end"] + right["source_start"]) / 2, 3)
    return runs
