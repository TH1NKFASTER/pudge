"""Which manga pages are shown together when two pages fit side by side.

One table for every reader: the Mac's two-page mode and the iPhone/iPad landscape view (its
copy of these rules, ios/PudgeCore/Sources/PudgeCore/MangaSpreads.swift, is tested against the
same cases, tests/fixtures/manga_spreads.json). Page numbers are indices from 0 in reading
order; each spread lists its pages lower first.

- The cover (first page) is alone.
- Then pages go in pairs. A wide page (one picture over two pages) is alone, and the page
  before it is alone when it has no partner; the pairs start again after a wide page, so the
  pages after a spread pair up as in the printed book.
- "Shift pairs by one page" (a per-volume setting, for scans that are one page off): the page
  after the cover is alone too, so every pair before the first spread moves by one page.
"""

from __future__ import annotations

from typing import Iterable


def spread_table(page_count: int, wide: Iterable[int] = (), *, shift: bool = False) -> list[list[int]]:
    """The spreads of a volume in reading order."""
    count = max(0, int(page_count))
    if count <= 0:
        return []
    wide_pages = {int(page) for page in wide}
    result: list[list[int]] = [[0]]
    index = 1
    if shift and index < count:
        result.append([index])
        index += 1
    while index < count:
        if index not in wide_pages and index + 1 < count and index + 1 not in wide_pages:
            result.append([index, index + 1])
            index += 2
        else:
            result.append([index])
            index += 1
    return result


def spread_of(page: int, spreads: list[list[int]]) -> list[int]:
    """The spread showing `page` (out of range: the nearest one)."""
    if not spreads:
        return [int(page)]
    for spread in spreads:
        if int(page) in spread:
            return spread
    return spreads[0] if int(page) < spreads[0][0] else spreads[-1]
