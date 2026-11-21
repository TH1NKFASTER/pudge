import Foundation

/// The word card's frequency as the Mac's word popup writes it ("Frequency #1234"); nil when
/// Jiten has no rank for the word (the card then shows none).
public enum WordFrequency {
    public static func label(_ rank: Int?) -> String? {
        guard let rank, rank > 0 else { return nil }
        return "Frequency #" + String(rank)
    }
}
