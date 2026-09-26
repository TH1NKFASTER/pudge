"""Normalize torrent backend transport states for observed traffic summaries.

Do not infer torrent completion from a rounded progress display or a rate of 0.
"""


def normalize_torrent_state(raw: str) -> str:
    """Map aria2/qBittorrent states to transport categories, preserving unknowns."""
    state = str(raw or "").strip().casefold()
    if state in {"paused", "stopped", "pauseddl", "pausedup", "stoppeddl", "stoppedup"}:
        return "paused"
    if state in {"queued", "waiting", "queueddl", "queuedup"}:
        return "queued"
    if state in {"stalled", "stalleddl", "stalledup"}:
        return "stalled"
    if state in {"checking", "checkingdl", "checkingup", "checkingresumdata", "moving", "allocating"}:
        return "checking"
    if state in {"active", "downloading", "forceddl", "metadl", "forcedmetadl", "metadownloading"}:
        return "downloading"
    if state in {"uploading", "seeding", "forcedup"}:
        return "seeding"
    if state in {"complete", "completed"}:
        return "complete"
    if state in {"error", "missingfiles"}:
        return "error"
    return "unknown"
