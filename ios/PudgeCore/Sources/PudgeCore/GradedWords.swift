import Foundation

/// The states of the words just graded (or added) on this device. Every text on screen — the
/// subtitles, the light-novel chapter, a looked-up manga bubble — takes them at once, for every
/// occurrence of the word (same Jiten word id and reading index), not only the tapped one.
public struct GradedWords: Equatable {
    /// Grows with each grade: screens apply `rows` when it changes.
    public private(set) var revision = 0
    /// The rows of the latest grade, by `StudyStateRow.key`.
    public private(set) var rows: [String: StudyStateRow] = [:]
    public init() {}

    public mutating func record(_ new: [StudyStateRow]) {
        guard !new.isEmpty else { return }
        rows = Dictionary(new.map { ($0.key, $0) }, uniquingKeysWith: { _, last in last })
        revision += 1
    }
}

extension SubtitleTrack {
    /// Every cue's words with fresher states (words not in `states` keep theirs).
    public func updating(_ states: [String: StudyStateRow]) -> SubtitleTrack {
        guard !states.isEmpty else { return self }
        var value = self
        for index in value.cues.indices { value.cues[index].tokens = value.cues[index].tokens.updating(states) }
        return value
    }
}

extension ParsedText {
    public func updating(_ states: [String: StudyStateRow]) -> ParsedText {
        guard !states.isEmpty else { return self }
        var value = self
        for index in value.paragraphs.indices { value.paragraphs[index].tokens = value.paragraphs[index].tokens.updating(states) }
        return value
    }
}

extension ChapterText {
    public func updating(_ states: [String: StudyStateRow]) -> ChapterText {
        guard !states.isEmpty else { return self }
        var value = self
        for index in value.paragraphs.indices { value.paragraphs[index].tokens = value.paragraphs[index].tokens.updating(states) }
        return value
    }
}

/// The word card's state label.
public enum WordStateLabel {
    /// The Mac's label (Jiten's names), except that a due word is always "Due", as its colour
    /// is (an older Mac labelled a mature-but-due word "Mature" in a Due-coloured badge).
    /// `fallback` names the state code when the Mac sent no label.
    public static func text(state: Int, macLabel: String, fallback: (Int) -> String) -> String {
        if state == 2 { return "Due" }
        return macLabel.isEmpty ? fallback(state) : macLabel
    }
}
