import Foundation
#if canImport(CoreGraphics)
import CoreGraphics  // CGPoint.zero, CGRect(x:y:width:height:), .width on Apple platforms
#endif

/// Two manga pages side by side like a printed book when the reader is wide enough (iPhone/iPad
/// landscape, an unfolded display): the Mac reader's rules (pudge/web/manga_reader_v2.js).
/// Page numbers are indices from 0 in reading order.
public enum MangaSpreads {
    /// height / width of a usual manga page (B6 tankōbon) until the volume's own is known.
    public static let defaultAspect = 1.42

    /// One picture over two pages (a double-page spread): clearly wider than tall, as on the Mac.
    public static func isWide(width: Double, height: Double) -> Bool {
        width > 0 && height > 0 && width >= height * 1.15
    }

    /// Two pages when each one is still shown at least 85% as large as one page alone would be.
    /// Decided by the space (width × height) and the page shape, never by the device model.
    public static func showsTwoPages(width: Double, height: Double, pageAspect: Double) -> Bool {
        guard width > 0, height > 0, pageAspect.isFinite, pageAspect > 0 else { return false }
        let single = min(width, height / pageAspect)
        let pair = min(width / 2, height / pageAspect)
        return pair >= single * 0.85
    }

    /// The volume as it is turned, in reading order, the lower page first in each spread.
    /// One page each unless `twoPages`; then the Mac's table (pudge/manga_spreads.py, the same
    /// cases in tests/fixtures/manga_spreads.json): the cover (first page) alone, pairs after it,
    /// a wide page alone (with the page before it when that one has no partner), and the pairing
    /// starts again after a wide page, so the pages after a spread pair up as in the book.
    /// `shift` ("Shift pairs by one page", per volume): the page after the cover is alone too.
    public static func spreads(pageCount: Int, wide: Set<Int>, twoPages: Bool, shift: Bool = false) -> [[Int]] {
        guard pageCount > 0 else { return [] }
        guard twoPages else { return (0..<pageCount).map { [$0] } }
        var result: [[Int]] = [[0]]
        var index = 1
        if shift, index < pageCount {
            result.append([index])
            index += 1
        }
        while index < pageCount {
            if !wide.contains(index), index + 1 < pageCount, !wide.contains(index + 1) {
                result.append([index, index + 1])
                index += 2
            } else {
                result.append([index])
                index += 1
            }
        }
        return result
    }

    /// The first page of the spread showing `page` (where the reader is after the layout changed;
    /// back to one page each it is that page again). Out of range: the nearest spread.
    public static func first(of page: Int, in spreads: [[Int]]) -> Int {
        if let spread = spreads.first(where: { $0.contains(page) }), let first = spread.first { return first }
        guard let firstSpread = spreads.first, let lastSpread = spreads.last else { return page }
        return page < (firstSpread.first ?? 0) ? (firstSpread.first ?? page) : (lastSpread.first ?? page)
    }

    /// The pages of a spread left to right on the screen: right to left (manga) puts the lower
    /// page on the right.
    public static func onScreen(_ spread: [Int], rightToLeft: Bool) -> [Int] {
        rightToLeft ? Array(spread.reversed()) : spread
    }

    /// The reader's title: "4–5 / 211" for a pair, "4 / 211" for one page (numbers from 1).
    public static func title(_ spread: [Int], pageCount: Int) -> String {
        guard let low = spread.min(), let high = spread.max() else { return "0 / \(pageCount)" }
        return low == high ? "\(low + 1) / \(pageCount)" : "\(low + 1)–\(high + 1) / \(pageCount)"
    }

    /// Where each page goes in a box of `box` size (the pages left to right as on screen, by their
    /// image sizes). One page fits the box, centred. Two meet in the middle (the gutter, where a
    /// fold is), each fitted into its half and centred vertically; with `sameHeight` (an aligned
    /// pair: its transforms are made for that) both get the lower of the two heights.
    public static func frames(_ sizes: [CGSize], in box: CGSize, sameHeight: Bool = false) -> [CGRect] {
        let whole = CGRect(origin: .zero, size: box)
        guard sizes.count == 2 else { return sizes.map { fit($0, in: whole, alignX: 0.5) } }
        let half = box.width / 2
        let fitted = [fit(sizes[0], in: CGRect(x: 0, y: 0, width: half, height: box.height), alignX: 1),
                      fit(sizes[1], in: CGRect(x: half, y: 0, width: half, height: box.height), alignX: 0)]
        guard sameHeight, sizes.allSatisfy({ $0.width > 0 && $0.height > 0 }) else { return fitted }
        let height: CGFloat = min(fitted[0].height, fitted[1].height)
        let leftWidth: CGFloat = sizes[0].width * height / sizes[0].height
        let rightWidth: CGFloat = sizes[1].width * height / sizes[1].height
        let top: CGFloat = (box.height - height) / 2
        return [CGRect(x: half - leftWidth, y: top, width: leftWidth, height: height),
                CGRect(x: half, y: top, width: rightWidth, height: height)]
    }

    /// `size` scaled to fit `slot`, centred vertically; `alignX` 0 puts it at the slot's left edge,
    /// 1 at its right edge, 0.5 in the middle. An unknown size takes the whole slot.
    public static func fit(_ size: CGSize, in slot: CGRect, alignX: CGFloat) -> CGRect {
        guard size.width > 0, size.height > 0, slot.width > 0, slot.height > 0 else { return slot }
        let scale = min(slot.width / size.width, slot.height / size.height)
        let width: CGFloat = size.width * scale
        let height: CGFloat = size.height * scale
        let x: CGFloat = slot.minX + (slot.width - width) * alignX
        let y: CGFloat = slot.minY + (slot.height - height) / 2
        return CGRect(x: x, y: y, width: width, height: height)
    }

    /// Wide pages and the usual (median) page shape of a volume from its pages' pixel sizes
    /// (page index → size; unreadable pages left out).
    public static func pages(sizes: [Int: CGSize]) -> MangaSpreadPages {
        var wide: [Int] = []
        var ratios: [Double] = []
        for (index, size) in sizes {
            let width = Double(size.width), height = Double(size.height)
            if isWide(width: width, height: height) {
                wide.append(index)
            } else if width > 0, height > 0 {
                ratios.append(height / width)
            }
        }
        ratios.sort()
        return MangaSpreadPages(wide: wide.sorted(), aspect: ratios.isEmpty ? nil : ratios[ratios.count / 2])
    }
}

/// A place to save after a turn: the first page of the spread turned to and, for a pair, its
/// second page (read too).
public struct MangaSave: Equatable {
    public let page: Int
    public let readThrough: Int?
    public init(page: Int, readThrough: Int?) { self.page = page; self.readThrough = readThrough }
}

/// The reader's place and what is on screen, kept apart: only a turn moves the place (and is
/// saved, with read pages); a new layout (rotation, a spread found, pairs shifted) only shows
/// the spread holding the place. So turning the device never saves, never marks pages read and
/// never moves the synced place back, and back in one page each it is the same page again.
public struct MangaReadingPlace: Equatable {
    /// The place: the page saved and synced (after a turn the first page of the spread).
    public private(set) var page: Int
    /// What one turn shows, in reading order.
    public private(set) var layout: [[Int]]
    /// Changes with the layout: the pager is made anew, and a page an old pager reports is ignored.
    public private(set) var generation = 0

    public init(page: Int, layout: [[Int]]) {
        self.page = max(0, page)
        self.layout = layout
    }

    /// The spread on screen: its first page (the pager's tag).
    public var shown: Int { MangaSpreads.first(of: page, in: layout) }
    /// The pages on screen, in reading order.
    public var shownPages: [Int] { layout.first { $0.first == shown } ?? [shown] }

    /// A new layout: the same place, on screen in the spread that holds it. Never a turn.
    public mutating func relayout(_ new: [[Int]]) {
        guard new != layout else { return }
        layout = new
        generation += 1
    }

    /// The pager settled on the spread starting at `tag`, in the pager of `generation`.
    /// What to save, or nil when it is not a turn: the spread already on screen, a pager of an
    /// older layout, or no spread of this layout starts there.
    public mutating func turned(to tag: Int, generation: Int) -> MangaSave? {
        guard generation == self.generation, tag != shown, let spread = layout.first(where: { $0.first == tag }) else { return nil }
        page = tag
        return MangaSave(page: tag, readThrough: spread.count > 1 ? spread.last : nil)
    }
}

/// How one page is drawn in the two-page view: a scanned page straightened and aligned with its
/// facing page (the Mac works it out, pudge/manga_align.py; the same cases as the Mac reader in
/// tests/fixtures/manga_alignment.json). Around the page's centre it is turned `angle` degrees
/// clockwise and scaled by `scale`, then moved by `dx` of its width and `dy` of its height: the
/// picture and its text boxes together. In a pair, `gutter` is its side that faces the gutter
/// ("left" or "right"): it is cut off there, and `backing` ([x0, y0, x1, y1], fractions of the
/// page's box) is painted `fill` ("#rrggbb") behind it.
public struct MangaPageTransform: Codable, Equatable, Sendable {
    public let angle: Double
    public let scale: Double
    public let dx: Double
    public let dy: Double
    public let gutter: String?
    public let backing: [Double]?
    public let fill: String?

    public init(angle: Double = 0, scale: Double = 1, dx: Double = 0, dy: Double = 0,
                gutter: String? = nil, backing: [Double]? = nil, fill: String? = nil) {
        self.angle = angle; self.scale = scale; self.dx = dx; self.dy = dy
        self.gutter = gutter; self.backing = backing; self.fill = fill
    }

    private enum CodingKeys: String, CodingKey { case angle, scale, dx, dy, gutter, backing, fill }

    /// Lenient: a missing or odd value is the one that changes nothing.
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        angle = (try? values.decodeIfPresent(Double.self, forKey: .angle)) ?? 0
        scale = (try? values.decodeIfPresent(Double.self, forKey: .scale)) ?? 1
        dx = (try? values.decodeIfPresent(Double.self, forKey: .dx)) ?? 0
        dy = (try? values.decodeIfPresent(Double.self, forKey: .dy)) ?? 0
        gutter = try? values.decodeIfPresent(String.self, forKey: .gutter)
        backing = try? values.decodeIfPresent([Double].self, forKey: .backing)
        fill = try? values.decodeIfPresent(String.self, forKey: .fill)
    }

    /// The scale, 1 when it makes no sense.
    public var safeScale: Double { scale.isFinite && scale > 0 ? scale : 1 }

    /// Where a point of the page (fractions of its box, y down) is drawn, in the same fractions.
    /// `aspect` is the box's width / height.
    public func drawn(_ point: CGPoint, aspect: Double) -> CGPoint {
        let ratio = aspect > 0 && aspect.isFinite ? aspect : 1
        let radians = angle * Double.pi / 180
        let px = (Double(point.x) - 0.5) * ratio, py = Double(point.y) - 0.5
        let rx = safeScale * (px * cos(radians) - py * sin(radians))
        let ry = safeScale * (px * sin(radians) + py * cos(radians))
        return CGPoint(x: 0.5 + rx / ratio + dx, y: 0.5 + ry + dy)
    }

    /// The point of the page drawn at `point` of its box (`drawn` backwards: taps, text boxes).
    public func page(at point: CGPoint, aspect: Double) -> CGPoint {
        let ratio = aspect > 0 && aspect.isFinite ? aspect : 1
        let radians = angle * Double.pi / 180
        let px = (Double(point.x) - 0.5 - dx) * ratio, py = Double(point.y) - 0.5 - dy
        let rx = (px * cos(radians) + py * sin(radians)) / safeScale
        let ry = (-px * sin(radians) + py * cos(radians)) / safeScale
        return CGPoint(x: 0.5 + rx / ratio, y: 0.5 + ry)
    }

    /// A point of the page drawn in `frame` (screen points), where it is on the screen.
    public func screenPoint(_ point: CGPoint, in frame: CGRect) -> CGPoint {
        guard frame.width > 0, frame.height > 0 else { return point }
        let local = CGPoint(x: (point.x - frame.minX) / frame.width, y: (point.y - frame.minY) / frame.height)
        let moved = drawn(local, aspect: Double(frame.width / frame.height))
        return CGPoint(x: frame.minX + moved.x * frame.width, y: frame.minY + moved.y * frame.height)
    }

    /// A point on the screen as the point of the page drawn in `frame` (where a tap is on it).
    public func pagePoint(_ point: CGPoint, in frame: CGRect) -> CGPoint {
        guard frame.width > 0, frame.height > 0 else { return point }
        let local = CGPoint(x: (point.x - frame.minX) / frame.width, y: (point.y - frame.minY) / frame.height)
        let mapped = self.page(at: local, aspect: Double(frame.width / frame.height))
        return CGPoint(x: frame.minX + mapped.x * frame.width, y: frame.minY + mapped.y * frame.height)
    }

    /// Whether a screen point is on this page's side of its gutter (the rest is cut off).
    public func keeps(_ point: CGPoint, in frame: CGRect) -> Bool {
        switch gutter ?? "" {
        case "right": return point.x <= frame.maxX
        case "left": return point.x >= frame.minX
        default: return true
        }
    }

    /// The backing, in points of a page box of `size` (from its top left corner); nil without one.
    public func backingRect(in size: CGSize) -> CGRect? {
        guard let box = backing, box.count == 4, box[2] > box[0], box[3] > box[1] else { return nil }
        return CGRect(x: CGFloat(box[0]) * size.width, y: CGFloat(box[1]) * size.height,
                      width: CGFloat(box[2] - box[0]) * size.width, height: CGFloat(box[3] - box[1]) * size.height)
    }

    /// The backing's colour as red, green and blue 0...1 (white when not given or not "#rrggbb").
    public var fillComponents: [Double] {
        guard let text = fill, text.count == 7, text.hasPrefix("#"), let value = Int(text.dropFirst(), radix: 16) else {
            return [1, 1, 1]
        }
        return [Double((value >> 16) & 0xFF) / 255, Double((value >> 8) & 0xFF) / 255, Double(value & 0xFF) / 255]
    }
}

/// One transform of the Mac's answer, nil when it is null or cannot be read (that page is
/// drawn as it is).
private struct MangaLenientTransform: Decodable {
    let value: MangaPageTransform?
    init(from decoder: Decoder) throws { value = try? MangaPageTransform(from: decoder) }
}

/// `alignment` of media/manga/<id>/spreads (and a downloaded volume's package.json): how the
/// Mac straightens and aligns the scanned pages of the two-page view (pudge/manga_align_store.py).
public struct MangaAlignment: Codable, Equatable, Sendable {
    public let version: Int?
    /// Pages and pairs the Mac is still working out (ask again a little later).
    public let pending: Int?
    /// Pages shown alone: straightened only, by page index.
    public let single: [String: MangaPageTransform]?
    /// "low-high" (page indices) → "rtl" / "ltr" → the two pages' transforms in page order
    /// (null: that page is left as it is).
    public let pairs: [String: [String: [MangaPageTransform?]]]?

    public init(version: Int? = nil, pending: Int? = nil, single: [String: MangaPageTransform]? = nil,
                pairs: [String: [String: [MangaPageTransform?]]]? = nil) {
        self.version = version; self.pending = pending; self.single = single; self.pairs = pairs
    }

    private enum CodingKeys: String, CodingKey { case version, pending, single, pairs }

    /// Lenient: an odd transform is that page left as it is, an odd entry is no alignment, so a
    /// downloaded volume always opens.
    public init(from decoder: Decoder) throws {
        guard let values = try? decoder.container(keyedBy: CodingKeys.self) else {
            version = nil; pending = nil; single = nil; pairs = nil
            return
        }
        version = try? values.decodeIfPresent(Int.self, forKey: .version)
        pending = try? values.decodeIfPresent(Int.self, forKey: .pending)
        let pages = try? values.decodeIfPresent([String: MangaLenientTransform].self, forKey: .single)
        single = pages?.compactMapValues { $0.value }
        let spreads = try? values.decodeIfPresent([String: [String: [MangaLenientTransform]]].self, forKey: .pairs)
        pairs = spreads?.mapValues { directions in directions.mapValues { list in list.map { $0.value } } }
    }

    /// The transforms of the pages of `spread` (reading order, the lower page first), in that
    /// order; nil when nothing changes: a pair by its pages and the reading direction, a page
    /// shown alone by itself.
    public func transforms(for spread: [Int], rightToLeft: Bool) -> [MangaPageTransform?]? {
        if spread.count == 2, let found = pairs?["\(spread[0])-\(spread[1])"]?[rightToLeft ? "rtl" : "ltr"],
           found.count == 2, found.contains(where: { $0 != nil }) {
            return found
        }
        if spread.count == 1, let one = single?[String(spread[0])] { return [one] }
        return nil
    }
}
