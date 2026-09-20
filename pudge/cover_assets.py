from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CoverSourceKind = Literal["embedded", "remote", "local_page"]


@dataclass(frozen=True, slots=True)
class CoverRef:
    """Typed reference for a thumbnail plus a lazily resolved preview asset."""

    asset_id: str
    source_revision: str
    thumbnail_url: str
    preview_url: str
    source_kind: CoverSourceKind
    original_width: int | None = None
    original_height: int | None = None

    def payload(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_revision": self.source_revision,
            "thumbnail_url": self.thumbnail_url,
            "preview_url": self.preview_url,
            "source_kind": self.source_kind,
            "original_width": self.original_width,
            "original_height": self.original_height,
        }
