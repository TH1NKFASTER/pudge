"""iPhone/iPad audiobooks: volumes still on the Mac join their series (Swift; runs only in Xcode).

"On the Mac" listed every book the Mac prepares on request as its own card titled
"<series> · Volume N": a long series title hid the volume, so the cards looked alike and
volume 1 had to be guessed. Now those volumes join their series card (with the volumes on this
device) and the series screen; a series with nothing here yet is one card in "On the Mac"; a
lone book shows "Vol. N · <series>" (volume first); and each shows its cover, which the catalog
now sends with it."""

from __future__ import annotations

from source_policy import source

LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def test_pending_volumes_are_grouped_with_their_series() -> None:
    library = source(LIBRARY)
    screen = _between(library, "struct LibraryScreen: View", "struct BookSeriesRoute")
    assert "pending: BookGroup.ordered(waiting[key] ?? [])" in screen
    assert "if group.books.count + group.pending.count > 1 {" in screen
    assert "let onMac = pendingGroups" in screen and "PendingSeriesCard(model: model, group: group" in screen
    assert (
        "ForEach(pending) { item in PendingBookCard(model: model, item: item, width: layout.card) }"
        not in screen
    )
    series = _between(library, "struct BookSeriesScreen: View", "enum BookCoverImages")
    assert (
        "ForEach(pending) { item in PendingBookCard(model: model, item: item, inSeries: true, width: layout.card) }"
        in series
    )
    card = _between(library, "struct BookSeriesCard: View", "struct PendingSeriesCard: View")
    assert "count: group.volumes" in card and 'Label("All \\(group.volumes) volumes"' in card


def test_a_pending_book_shows_its_volume_first_and_its_cover() -> None:
    library = source(LIBRARY)
    card = _between(library, "struct PendingBookCard: View", "struct PendingBookCover: View")
    assert '"Vol. \\($0) · \\(item.seriesTitle ?? item.title)"' in card and '"Volume \\($0)"' in card
    assert (
        "PendingBookCover(store: model.store, cover: item.cover, generation: model.coverGeneration)" in card
    )
    cover = _between(library, "struct PendingBookCover: View", "/// The volumes of one audiobook series.")
    assert "image = await BookCoverImages.load(cover, store: store)" in cover
    model = source(MODEL)
    assert "let volume: Int?; let cover: CoverResource?; var id: String { assetId } }" in model
    assert "await prefetchCovers(catalog.items, pending: pendingBooks, connection: connection)" in model
    assert "for cover in wanted + waiting {" in model
