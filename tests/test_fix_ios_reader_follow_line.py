"""iPhone/iPad light-novel reader with the audiobook (Swift; runs only in Xcode on the Mac).

- "Read along" in the audiobook player opens the reader full screen, as Light Novels does: as a
  sheet over the player's own sheet it left an empty band at the top. It carries the light
  novel's title.
- Following the audio keeps the spoken LINE in the middle, once per line: the text view reports
  the line of its current word, the reader puts a zero-width view with a fixed id on that line
  and scrolls to it. Before, it centred the whole paragraph once per paragraph, so the spoken
  line ran off a paragraph taller than the screen, and each paragraph change jumped by half of
  two paragraphs. A paragraph off screen is brought in without animation first (a lazy stack
  only estimates rows it has not drawn: an animated scroll there landed beside the line and
  jerked back), and the line goes back to the middle when the bars hide or show.
"""

from __future__ import annotations

from source_policy import source

PLAYER = "ios/Pudge/UI/PlayerScreen.swift"
READER = "ios/Pudge/UI/ReadScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"


def test_read_along_from_the_player_opens_the_reader_full_screen_with_the_novels_title() -> None:
    player = source(PLAYER)
    cover = player.index(".fullScreenCover(item: Binding(get: { reader.map(ReaderTarget.init) }")
    call = player.index("NovelReaderScreen(media: model.media, bookId: target.id,", cover)
    assert call - cover < 400
    assert "model.media.novels.first { $0.id == target.id }?.title" in player[call : call + 300]
    assert ".sheet(item: Binding(get: { reader.map(ReaderTarget.init) }" not in player
    assert ".fullScreenCover(isPresented: $readingAlong)" in player
    assert ".sheet(isPresented: $readingAlong)" not in player


def test_the_text_view_reports_the_line_of_its_current_word_once_per_line() -> None:
    text = source(TEXT)
    assert "var onCurrentLine: ((CGRect?) -> Void)?" in text
    assert "var onCurrentLine: ((CGRect?) -> Void)? = nil" in text  # the SwiftUI wrapper
    assert "view.onCurrentLine = onCurrentLine" in text
    report = text[text.index("private func reportCurrentLine") :]
    report = report[: report.index("\n    }\n")]
    # The whole width: words moving along one line report nothing new.
    assert "CGRect(x: 0, y: flipY - $0.maxY, width: bounds.width, height: $0.height)" in report
    assert "guard line != reportedCurrentLine else { return }" in report
    assert "reportCurrentLine(current.flatMap" in text


def test_following_the_audio_centres_the_spoken_line() -> None:
    reader = source(READER)
    assert "onCurrentLine: { line in placeSpokenLine(index, line) }" in reader
    anchor = reader[reader.index(".background(alignment: .topLeading) {") :][:500]
    assert "if let line = spokenLine, line.paragraph == index" in anchor
    # The id sits on the line-high view, the padding outside it.
    assert anchor.index(".id(SpokenLineAnchor.id)") < anchor.index(".padding(.top, max(0, line.rect.minY))")
    follow = reader[reader.index(".onChange(of: spokenLine) { _, line in") :][:600]
    assert "proxy.scrollTo(SpokenLineAnchor.id, anchor: .center)" in follow
    assert "guard follow, !dragging, let line, line.paragraph == follower.spoken?.paragraph" in follow
    paragraph = reader[reader.index(".onChange(of: follower.spoken?.paragraph) { _, value in") :][:1800]
    # Off screen: no animation, so the lazy stack lands on the real row.
    assert (
        "if !tracker.visible.contains(value) { DispatchQueue.main.async { proxy.scrollTo(value, anchor: .center) } }"
        in paragraph
    )
    assert "follower.spoken?.token == nil" in paragraph  # sentence timing: the paragraph, as before
    chrome = reader[reader.index(".onChange(of: chrome) { _, _ in") :][:500]
    assert "proxy.scrollTo(SpokenLineAnchor.id, anchor: .center)" in chrome
    assert "revealed = []; spokenLine = nil" in reader  # a new chapter starts without a line
