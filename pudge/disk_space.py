"""Free space of the volume that receives Pudge downloads.

A Mac with a 256/512 GB SSD fills quickly with torrents; once the volume is
full SQLite fails with "disk is full". Automatic downloads therefore always
leave a reserve free on the download volume, whatever the configured limit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

GB = 1024**3
# The reserve kept free on the download volume unless config.toml sets one.
MIN_FREE_RESERVE_BYTES = 20 * GB
FREE_RESERVE_FRACTION = 0.05
# A release whose size is unknown is admitted only when a typical episode fits.
UNKNOWN_DOWNLOAD_BYTES = 2 * GB


def volume_usage(path: Path) -> tuple[int, int] | None:
    """``(total, free)`` bytes of the volume holding ``path`` (or its nearest existing folder)."""
    candidate = Path(path).expanduser()
    for folder in (candidate, *candidate.parents):
        try:
            if not folder.exists():
                continue
            usage = shutil.disk_usage(folder)
        except OSError:
            return None
        return int(usage.total), int(usage.free)
    return None


def free_space_reserve(total_bytes: int, configured_gb: float = 0.0) -> int:
    """Bytes kept free: the configured value, else max(20 GB, 5 % of the volume)."""
    if float(configured_gb or 0.0) > 0:
        return int(float(configured_gb) * GB)
    return max(MIN_FREE_RESERVE_BYTES, int(max(0, int(total_bytes)) * FREE_RESERVE_FRACTION))
