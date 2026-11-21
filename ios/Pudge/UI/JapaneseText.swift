import SwiftUI
import UIKit
import CoreGraphics
import CoreText
import QuartzCore
import PudgeCore

/// Japanese text in the app. iOS picks the glyphs of a CJK ideograph by the text's language: a
/// font without kanji (SF, the UI font) falls back to PingFang, the Chinese font, unless the text
/// says it is Japanese, so 直 骨 角 化 次 came out in their Chinese forms on a device whose
/// languages do not include Japanese (Han unification). Every view that shows Japanese uses this:
/// SwiftUI views `.japaneseTypesetting()` (set once at the root, again where Japanese is the
/// content) and `JapaneseType.font` for a word shown large; CoreText strings `JapaneseType.attributes`.
enum JapaneseType {
    static let languageCode = "ja"
    static let language = Locale.Language(identifier: languageCode)
    /// For an attributed string drawn by CoreText or UIKit: its runs (and the fonts CoreText falls
    /// back to for them) are Japanese.
    static let languageKey = NSAttributedString.Key(kCTLanguageAttributeName as String)
    static func tag(_ text: NSMutableAttributedString) {
        text.addAttribute(languageKey, value: languageCode, range: NSRange(location: 0, length: text.length))
    }
    /// Hiragino Sans (the Mac's Japanese UI font) at a text style's size, scaling with Dynamic Type
    /// as the style does (`size` is the style's size at the default text size; SwiftUI scales it).
    static func font(_ style: Font.TextStyle, bold: Bool = false) -> Font {
        let standard = UITraitCollection(preferredContentSizeCategory: .large)
        let size = UIFont.preferredFont(forTextStyle: uiStyle(style), compatibleWith: standard).pointSize
        return Font.custom(bold ? "HiraginoSans-W6" : "HiraginoSans-W3", size: size, relativeTo: style)
    }
    /// The same at a fixed size (the video's subtitles, the review card's word).
    static func font(size: CGFloat, bold: Bool = false) -> Font {
        Font.custom(bold ? "HiraginoSans-W6" : "HiraginoSans-W3", fixedSize: size)
    }
    /// A Japanese font by PostScript name, else the system's Japanese font (never PingFang).
    static func uiFont(_ names: [String], size: CGFloat) -> UIFont {
        for name in names { if let value = UIFont(name: name, size: size) { return value } }
        return UIFont(name: "HiraginoSans-W3", size: size) ?? UIFont.systemFont(ofSize: size)
    }
    /// A font family the Mac's theme names (JitenMPV's FontFamily) when this device has it.
    static func uiFont(family: String, size: CGFloat, bold: Bool) -> UIFont? {
        let wanted = family.trimmingCharacters(in: .whitespaces)
        guard !wanted.isEmpty, let name = UIFont.familyNames.first(where: { $0.caseInsensitiveCompare(wanted) == .orderedSame }) else { return nil }
        var descriptor = UIFontDescriptor(fontAttributes: [.family: name])
        if bold, let heavier = descriptor.withSymbolicTraits(.traitBold) { descriptor = heavier }
        return UIFont(descriptor: descriptor, size: size)
    }
    private static func uiStyle(_ style: Font.TextStyle) -> UIFont.TextStyle {
        switch style {
        case .largeTitle: return .largeTitle
        case .title: return .title1
        case .title2: return .title2
        case .title3: return .title3
        case .headline: return .headline
        case .subheadline: return .subheadline
        case .callout: return .callout
        case .footnote: return .footnote
        case .caption: return .caption1
        case .caption2: return .caption2
        default: return .body
        }
    }
}

extension View {
    /// Text in this view is typeset as Japanese: kanji in their Japanese forms (see JapaneseType).
    /// It changes no localisation, date or number format.
    func japaneseTypesetting() -> some View { typesettingLanguage(JapaneseType.language) }
    /// An inline navigation title that may be Japanese (a series, a volume): the bar's own title
    /// is UIKit's, without the language, so the title is a Japanese-typeset Text in its place
    /// (the back button and VoiceOver keep `title`).
    func japaneseNavigationTitle(_ title: String) -> some View {
        navigationTitle(title).navigationBarTitleDisplayMode(.inline).toolbar {
            ToolbarItem(placement: .principal) {
                Text(title).font(.headline).lineLimit(1).truncationMode(.tail).japaneseTypesetting()
                    .accessibilityAddTraits(.isHeader)
            }
        }
    }
}

/// How words are drawn in the reader (from the Mac's reader settings, overridable on the iPhone).
struct ReaderStyle: Equatable {
    enum Marks: String, CaseIterable { case underline, color, highlight, off }
    enum FuriganaMode: String, CaseIterable { case mac, all, off }
    /// `.mac`: the Mac reader's colours; `.jiten`: Jiten's (new red, young amber, due orange, mature plain).
    enum Palette { case mac, jiten }
    var fontSize: CGFloat = 21
    var lineHeight: CGFloat = 1.9
    var font = "mincho"
    var marks: Marks = .underline
    var furigana: FuriganaMode = .mac
    var text = UIColor(white: 0.79, alpha: 1)
    var look = ReaderLook()
    var palette: Palette = .mac
    var centered = false
    var shadow = false
    /// The user's JitenMPV word styles (subtitles): per-state colour, outline, bold, underline.
    var subtitleLook: SubtitleLook?

    func uiFont(_ size: CGFloat, bold: Bool = false) -> UIFont {
        // Subtitles: the JitenMPV theme's font when this device has it (CoreText falls back to a
        // Japanese font for what it lacks: the strings are tagged Japanese), else Hiragino Sans.
        if font == "subtitle", let family = subtitleLook?.fontFamily, !family.lowercased().hasPrefix("hiragino"),
           let themed = JapaneseType.uiFont(family: family, size: size, bold: bold) {
            return themed
        }
        let names = font == "subtitle" ? (bold ? ["HiraginoSans-W8", "HiraginoSans-W7", "HiraKakuProN-W6"] : ["HiraginoSans-W6", "HiraKakuProN-W6"])
            : font == "gothic" || font.contains("sans") ? ["HiraginoSans-W3", "HiraKakuProN-W3"] : ["HiraMinProN-W3", "HiraMinProN-W6"]
        return JapaneseType.uiFont(names, size: size)
    }
    func stateColor(_ token: StudyToken) -> UIColor { look.color(for: token).flatMap { UIColor(hex: $0) } ?? text }
    /// Colour of a word in colour/highlight mode.
    func color(for token: StudyToken) -> UIColor {
        guard palette == .jiten else { return stateColor(token) }
        // A newer Mac sends the word's Jiten state: Jiten's own default colours.
        if token.wordState != nil { return ReaderLook().color(for: token).flatMap { UIColor(hex: $0) } ?? text }
        if token.state == 4 { return UIColor(hex: "#9ca3af")! }
        if token.isMature { return UIColor(hex: ReaderLook.defaultColors["mature"]!)! }
        switch token.state {
        case 0: return UIColor(hex: "#f43f5e")!
        case 1: return UIColor(hex: "#f59e0b")!
        case 2: return UIColor(hex: "#f97316")!
        default: return text
        }
    }
    /// A JitenMPV style's text colour at its opacity, exactly as the Mac's theme has it.
    func subtitleFill(_ word: JitenWordStyle) -> CGColor {
        (UIColor(hex: word.text) ?? .white).withAlphaComponent(CGFloat(min(255, max(0, word.opacity ?? 255))) / 255).cgColor
    }
}

extension UIColor {
    convenience init?(hex: String) {
        var value = hex.trimmingCharacters(in: .whitespaces)
        if value.hasPrefix("#") { value.removeFirst() }
        guard value.count == 6, let number = UInt32(value, radix: 16) else { return nil }
        self.init(red: CGFloat((number >> 16) & 0xff) / 255, green: CGFloat((number >> 8) & 0xff) / 255,
                  blue: CGFloat(number & 0xff) / 255, alpha: 1)
    }
}

/// Japanese paragraph drawn with CoreText: furigana (ruby), word colours, underlines or
/// highlights like the Mac reader, and the tapped word reported by index.
/// Selecting text (TextSelectionController): a long press selects the word (drag to extend),
/// two taps the word, three the sentence; handles move either end, the menu offers Copy /
/// Look Up / Listen from here / Read up to here. In the reader one selection spans paragraphs.
final class JapaneseTextView: UIView, UIGestureRecognizerDelegate {
    var paragraph = TextParagraph(text: "", tokens: []) {
        didSet {
            guard paragraph != oldValue else { return }
            invalidate()
            // Another text (the next subtitle line) drops its own selection; fresh word states
            // keep it. The reader's selection stays when a paragraph view is made again or reused
            // (it starts with no text); the reader drops it when it opens another chapter.
            if shared == nil, paragraph.text != oldValue.text { ownSelection?.textChanged(paragraph: 0) }
        }
    }
    var style = ReaderStyle() { didSet { if style != oldValue { invalidate() } } }
    var current: Int? { didSet { if current != oldValue { setNeedsDisplay() } } }
    var onTap: ((Int) -> Void)?
    /// A tap that is not on a word (the reader shows or hides its controls).
    var onTapEmpty: (() -> Void)?
    /// A selection started (the reader keeps its controls visible).
    var onSelect: (() -> Void)?
    /// This view's own selection went (a subtitle line: the player lets the clock pick the line again).
    var onDeselect: (() -> Void)?
    /// Room above and below the text inside the view (points), for the selection handles' knobs:
    /// SwiftUI hit-tests a hosted view only inside its frame, so a subtitle line takes its
    /// vertical padding here and a finger on a knob lands on this view.
    var handleRoom: CGFloat = 0 {
        didSet {
            guard handleRoom != oldValue else { return }
            invalidateIntrinsicContentSize()
            setNeedsLayout()
            setNeedsDisplay()
        }
    }
    /// "Listen from here" in the selection menu, with the selection's UTF-16 start.
    var onListenFrom: ((Int) -> Void)?
    /// The "read up to here" marker: a thin red caret right before this UTF-16 index.
    var marker: Int? { didSet { if marker != oldValue { setNeedsDisplay() } } }
    /// The marker's line box (view coordinates) after drawing, nil without one: the reader puts
    /// its ribbon in the margin there.
    var onMarkerLine: ((CGRect?) -> Void)?
    /// The line of the `current` word (view coordinates, the view's whole width) after drawing,
    /// nil without one; reported once per line, not per word: the reader keeps it in the middle.
    var onCurrentLine: ((CGRect?) -> Void)?
    /// "Read up to here" in the selection menu, with the selection's UTF-16 start.
    var onMarkHere: ((Int) -> Void)?
    /// What three taps select: the sentence, or the whole text (a subtitle line).
    var tripleTap: TextUnits.Triple = .sentence
    private var reportedMarkerLine: CGRect?
    private var reportedCurrentLine: CGRect?

    /// The reader's selection over all its paragraphs (this one is `paragraphIndex` in its
    /// chapter), or without the reader a selection of this view's own (paragraph 0).
    private var shared: TextSelectionController?
    private var ownSelection: TextSelectionController?
    private(set) var paragraphIndex = 0
    var selection: TextSelectionController {
        if let shared { return shared }
        if let ownSelection { return ownSelection }
        let made = TextSelectionController(single: self)
        ownSelection = made
        return made
    }
    private var handleViews: [TextSelectionHandle: SelectionHandleView] = [:]
    private weak var tapRecognizer: UITapGestureRecognizer?
    /// The view's own long press; in the reader the reader's scroll view holds it instead, so a
    /// drag goes on when this paragraph scrolls out of the window.
    private weak var pressRecognizer: UILongPressGestureRecognizer?

    private var framesetter: CTFramesetter?
    /// Laid-out text per width: the view's own width and the widths SwiftUI asks about while
    /// sizing (a probe never replaces the layout that drawing and touches use).
    private var frameCache: [(width: CGFloat, frame: CTFrame, height: CGFloat, outline: CTFrame?)] = []
    /// What the last draw put on screen: its text, CoreText frame and CoreText origin (flipY).
    /// Touches map to characters with exactly this geometry until the next draw (a rotation, a
    /// new size or font is on screen only once it is drawn); another text uses the current layout.
    private var drawn: (text: String, frame: CTFrame, flipY: CGFloat)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        isOpaque = false
        contentMode = .redraw
        // A single tap acts at once on its touch's end (the lookup waits 0.25 s for a second
        // tap); two and three taps are counted from the same recognizer (TapSequence).
        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped(_:)))
        tap.delegate = self
        addGestureRecognizer(tap)
        tapRecognizer = tap
        let press = UILongPressGestureRecognizer(target: self, action: #selector(pressed(_:)))
        press.delegate = self
        addGestureRecognizer(press)
        pressRecognizer = press
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    /// Joins the reader's selection as paragraph `index` of its chapter (nil: selects on its own).
    func use(_ controller: TextSelectionController?, paragraph index: Int) {
        let number = controller == nil ? 0 : index
        if controller === shared && number == paragraphIndex { return }
        (shared ?? ownSelection)?.unregister(self)
        if controller != nil, let ownSelection {
            ownSelection.detach()
            self.ownSelection = nil
        }
        shared = controller
        paragraphIndex = number
        pressRecognizer?.isEnabled = controller == nil
        if window != nil { selection.register(self) }
        selectionChanged()
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil { selection.register(self) } else { (shared ?? ownSelection)?.unregister(self) }
    }

    private var laidOutSize = CGSize.zero

    override func layoutSubviews() {
        super.layoutSubviews()
        placeHandles()
        // Rotated or resized: the selection redraws at the new width and the menu follows.
        if bounds.size != laidOutSize {
            laidOutSize = bounds.size
            setNeedsDisplay()
            if selectedRange != nil { selection.layoutChanged() }
        }
    }

    // The handles' 44-point targets reach past the text's edges.
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if super.point(inside: point, with: event) { return true }
        guard window != nil, selection.hasSelection else { return false }
        return selection.handle(at: inWindow(point)) != nil
    }

    private func invalidate() {
        framesetter = nil; frameCache = []
        invalidateIntrinsicContentSize(); setNeedsDisplay()
        setNeedsLayout()
    }

    private static func color(_ hex: String?, opacity: Int?) -> CGColor {
        let base = UIColor(hex: hex ?? "") ?? .white
        return base.withAlphaComponent(CGFloat(min(255, max(0, opacity ?? 255))) / 255).cgColor
    }

    /// JitenMPV look: fill pass (colours, bold) or outline pass (stroke only, same layout).
    private func applySubtitleLook(_ look: SubtitleLook, to result: NSMutableAttributedString, full: NSRange, outline: Bool) {
        let fontKey = NSAttributedString.Key(kCTFontAttributeName as String)
        let colorKey = NSAttributedString.Key(kCTForegroundColorAttributeName as String)
        let strokeWidthKey = NSAttributedString.Key(kCTStrokeWidthAttributeName as String)
        let strokeColorKey = NSAttributedString.Key(kCTStrokeColorAttributeName as String)
        func apply(_ word: JitenWordStyle, _ range: NSRange) {
            let bold = word.bold == true
            result.addAttribute(fontKey, value: style.uiFont(style.fontSize, bold: bold), range: range)
            if outline {
                let width = max(0, word.outlineSize) / max(1, look.fontSize) * 200
                result.addAttribute(colorKey, value: UIColor.clear.cgColor, range: range)
                result.addAttribute(strokeWidthKey, value: width, range: range)
                // The outline stays solid; only the text takes the style's opacity. This assumes
                // JitenMPV's text_opacity is the fill's alone (ASS \1a, not \alpha); JitenMPV is
                // not in this repository. The solid outline is what keeps its dimmed words
                // (Blacklisted, Redundant, Suspended) readable over the video, as in mpv; with
                // the opacity on the outline too they were grey on grey on the phone.
                result.addAttribute(strokeColorKey, value: Self.color(word.outline, opacity: nil), range: range)
            } else {
                result.addAttribute(colorKey, value: style.subtitleFill(word), range: range)
            }
        }
        apply(look.unparsed, full)
        for token in paragraph.tokens where token.hasEntry {
            guard let range = clamp(token.start, token.end, full.length) else { continue }
            apply(look.style(token), range)
        }
    }

    private func attributed(outline: Bool = false) -> NSAttributedString {
        let text = paragraph.text
        let result = NSMutableAttributedString(string: text)
        let full = NSRange(location: 0, length: (text as NSString).length)
        let font = style.uiFont(style.fontSize)
        let fontKey = NSAttributedString.Key(kCTFontAttributeName as String)
        let colorKey = NSAttributedString.Key(kCTForegroundColorAttributeName as String)
        result.addAttribute(fontKey, value: font, range: full)
        result.addAttribute(colorKey, value: style.text.cgColor, range: full)
        // Japanese: kanji the font lacks fall back to a Japanese font, not PingFang (Chinese forms).
        JapaneseType.tag(result)
        var minimum = style.fontSize * max(1.2, style.lineHeight)
        var alignment: CTTextAlignment = style.centered ? .center : .natural
        let paragraphStyle: CTParagraphStyle = withUnsafeBytes(of: &minimum) { height in
            withUnsafeBytes(of: &alignment) { align in
                var settings = [
                    CTParagraphStyleSetting(spec: .minimumLineHeight, valueSize: MemoryLayout<CGFloat>.size, value: height.baseAddress!),
                    CTParagraphStyleSetting(spec: .alignment, valueSize: MemoryLayout<CTTextAlignment>.size, value: align.baseAddress!),
                ]
                return CTParagraphStyleCreate(&settings, settings.count)
            }
        }
        result.addAttribute(NSAttributedString.Key(kCTParagraphStyleAttributeName as String), value: paragraphStyle, range: full)
        if let look = style.subtitleLook {
            applySubtitleLook(look, to: result, full: full, outline: outline)
        } else if style.marks == .color {
            // As on the Mac: colour mode colours every word by its state.
            for token in paragraph.tokens where token.hasEntry {
                guard let range = clamp(token.start, token.end, full.length) else { continue }
                result.addAttribute(colorKey, value: style.color(for: token).cgColor, range: range)
            }
        }
        if style.furigana != .off && (style.furigana == .all || style.look.showFurigana) {
            let rubyKey = NSAttributedString.Key(kCTRubyAnnotationAttributeName as String)
            for ruby in paragraph.ruby ?? [] {
                let owner = paragraph.token(at: ruby.start)
                if style.furigana == .mac {
                    guard let index = owner, paragraph.tokens[index].wantsFurigana else { continue }
                }
                guard let range = clamp(ruby.start, ruby.end, full.length) else { continue }
                // Furigana in the colour of its word when the word's state colours it (subtitles,
                // colour mode); otherwise as before.
                let ownColor = owner.flatMap { paragraph.tokens[safe: $0] }.flatMap { wordColor($0) }
                let rubyColor = outline ? UIColor.clear.cgColor : ownColor ?? style.text.withAlphaComponent(0.85).cgColor
                let attributes: [String: Any] = [kCTRubyAnnotationSizeFactorAttributeName as String: 0.5,
                                                 kCTForegroundColorAttributeName as String: rubyColor,
                                                 kCTLanguageAttributeName as String: JapaneseType.languageCode]
                let annotation = CTRubyAnnotationCreateWithAttributes(.auto, .auto, .before, ruby.reading as CFString, attributes as CFDictionary)
                result.addAttribute(rubyKey, value: annotation, range: range)
            }
        }
        return result
    }

    /// The colour of a word's characters when its state colours them (the JitenMPV look, colour
    /// mode); nil when they are in the text colour (underline, highlight, no entry).
    private func wordColor(_ token: StudyToken) -> CGColor? {
        guard token.hasEntry else { return nil }
        if let look = style.subtitleLook { return style.subtitleFill(look.style(token)) }
        return style.marks == .color ? style.color(for: token).cgColor : nil
    }

    private func clamp(_ start: Int, _ end: Int, _ length: Int) -> NSRange? {
        let low = max(0, start), high = min(length, end)
        return high > low ? NSRange(location: low, length: high - low) : nil
    }

    private func layout(width: CGFloat) -> (frame: CTFrame, height: CGFloat, outline: CTFrame?)? {
        guard width > 1 else { return nil }
        if let cache = frameCache.first(where: { abs($0.width - width) < 0.5 }) { return (cache.frame, cache.height, cache.outline) }
        let setter = framesetter ?? CTFramesetterCreateWithAttributedString(attributed() as CFAttributedString)
        framesetter = setter
        let size = CTFramesetterSuggestFrameSizeWithConstraints(setter, CFRange(location: 0, length: 0), nil,
                                                                CGSize(width: width, height: .greatestFiniteMagnitude), nil)
        let height = ceil(size.height) + 2
        let path = CGPath(rect: CGRect(x: 0, y: 0, width: width, height: height), transform: nil)
        let frame = CTFramesetterCreateFrame(setter, CFRange(location: 0, length: 0), path, nil)
        var outline: CTFrame?
        if style.subtitleLook != nil {
            // Same text, fonts and ruby: the stroke-only frame lays out exactly like the fill frame.
            let outlineSetter = CTFramesetterCreateWithAttributedString(attributed(outline: true) as CFAttributedString)
            outline = CTFramesetterCreateFrame(outlineSetter, CFRange(location: 0, length: 0), path, nil)
        }
        // The view's own width stays cached next to a few recent ones.
        if frameCache.count >= 3 {
            let own = bounds.width
            if let stale = frameCache.firstIndex(where: { abs($0.width - own) >= 0.5 }) { frameCache.remove(at: stale) } else { frameCache.removeFirst() }
        }
        frameCache.append((width, frame, height, outline))
        return (frame, height, outline)
    }

    func height(for width: CGFloat) -> CGFloat { (layout(width: width)?.height ?? style.fontSize * style.lineHeight) + handleRoom * 2 }

    /// Where CoreText's origin (the text's bottom edge) is in view coordinates: CoreText's y
    /// runs up from there (the view's bottom less the handle room).
    private var flipY: CGFloat { bounds.height - handleRoom }

    override var intrinsicContentSize: CGSize { CGSize(width: UIView.noIntrinsicMetric, height: height(for: bounds.width)) }

    private struct Line { let line: CTLine; let origin: CGPoint; let ascent: CGFloat; let descent: CGFloat; let range: NSRange }

    private func lines(_ frame: CTFrame) -> [Line] {
        guard let values = CTFrameGetLines(frame) as? [CTLine], !values.isEmpty else { return [] }
        var origins = [CGPoint](repeating: .zero, count: values.count)
        CTFrameGetLineOrigins(frame, CFRange(location: 0, length: 0), &origins)
        return values.enumerated().map { index, line in
            var ascent: CGFloat = 0, descent: CGFloat = 0, leading: CGFloat = 0
            _ = CTLineGetTypographicBounds(line, &ascent, &descent, &leading)
            let range = CTLineGetStringRange(line)
            return Line(line: line, origin: origins[index], ascent: ascent, descent: descent,
                        range: NSRange(location: range.location, length: range.length))
        }
    }

    /// Rectangles (CoreText coordinates) covering a word, one per line it spans.
    private func rects(_ token: StudyToken, in items: [Line]) -> [CGRect] {
        rects(NSRange(location: token.start, length: max(0, token.end - token.start)), in: items)
    }

    /// Rectangles (CoreText coordinates) covering a UTF-16 range, one per line it spans.
    private func rects(_ range: NSRange, in items: [Line]) -> [CGRect] {
        var result: [CGRect] = []
        let glyphHeight = style.fontSize
        for item in items {
            let low = max(range.location, item.range.location)
            let high = min(range.location + range.length, item.range.location + item.range.length)
            guard high > low else { continue }
            let x1 = CTLineGetOffsetForStringIndex(item.line, low, nil), x2 = CTLineGetOffsetForStringIndex(item.line, high, nil)
            result.append(CGRect(x: item.origin.x + min(x1, x2), y: item.origin.y - item.descent,
                                 width: abs(x2 - x1), height: item.descent + glyphHeight * 1.02))
        }
        return result
    }

    /// A 2-pt caret right before a UTF-16 index, on the line that holds it (CoreText coordinates).
    private func caretRect(_ index: Int, in items: [Line]) -> CGRect? {
        var found = items.first { index >= $0.range.location && index < $0.range.location + $0.range.length }
        if found == nil, let last = items.last, index >= last.range.location + last.range.length { found = last }
        guard let item = found else { return nil }
        let x = CTLineGetOffsetForStringIndex(item.line, index, nil)
        return CGRect(x: item.origin.x + x - 1, y: item.origin.y - item.descent, width: 2, height: item.descent + style.fontSize * 1.02)
    }

    private func reportMarkerLine(_ caret: CGRect?) {
        // CoreText coordinates are flipped.
        let line = caret.map { CGRect(x: $0.minX, y: flipY - $0.maxY, width: $0.width, height: $0.height) }
        guard line != reportedMarkerLine else { return }
        reportedMarkerLine = line
        if let report = onMarkerLine { DispatchQueue.main.async { report(line) } }
    }

    private func reportCurrentLine(_ box: CGRect?) {
        // CoreText coordinates are flipped; the line counts, not where the word sits on it.
        let line = box.map { CGRect(x: 0, y: flipY - $0.maxY, width: bounds.width, height: $0.height) }
        guard line != reportedCurrentLine else { return }
        reportedCurrentLine = line
        if let report = onCurrentLine { DispatchQueue.main.async { report(line) } }
    }

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext(), let laid = layout(width: bounds.width) else { return }
        let frame = laid.frame
        drawn = (paragraph.text, frame, flipY)
        context.saveGState()
        context.textMatrix = .identity
        context.translateBy(x: 0, y: flipY)
        context.scaleBy(x: 1, y: -1)
        let items = lines(frame)
        if let selected = selectedRange {
            // The system's selection look: the tint colour behind the text.
            context.setFillColor(tintColor.withAlphaComponent(0.3).cgColor)
            for box in rects(selected, in: items) { context.addPath(UIBezierPath(roundedRect: box, cornerRadius: 2).cgPath) }
            context.fillPath()
        }
        // Drawn under the text, between two characters: the layout never moves.
        let caret = marker.flatMap { caretRect($0, in: items) }
        if let caret {
            context.setFillColor(UIColor.systemRed.cgColor)
            context.fill(caret)
        }
        reportMarkerLine(caret)
        reportCurrentLine(current.flatMap { index in
            paragraph.tokens.indices.contains(index) ? rects(paragraph.tokens[index], in: items).first : nil
        })
        if let look = style.subtitleLook {
            // The outline is a stroke around each glyph: with the default miter joins a sharp
            // corner (the bottom left of に) grew a spike of the outline colour; round joins and
            // caps follow the glyph's shape, as mpv/libass outlines do.
            context.setLineJoin(.round)
            context.setLineCap(.round)
            if let outline = laid.outline { CTFrameDraw(outline, context) }
            for (index, token) in paragraph.tokens.enumerated() where token.hasEntry {
                let word = look.style(token)
                let boxes = rects(token, in: items)
                if index == current {
                    context.setFillColor(UIColor(red: 0.43, green: 0.55, blue: 1, alpha: 0.38).cgColor)
                    for box in boxes { context.addPath(UIBezierPath(roundedRect: box, cornerRadius: 3).cgPath) }
                    context.fillPath()
                }
                guard word.underline == true else { continue }
                context.setStrokeColor(Self.color(word.underlineColor ?? word.text, opacity: nil))
                context.setLineWidth(max(2, CGFloat(word.underlineThickness ?? 3) / CGFloat(max(1, look.fontSize)) * style.fontSize))
                for box in boxes {
                    let y = box.minY + max(1, box.height * 0.04)
                    context.move(to: CGPoint(x: box.minX, y: y)); context.addLine(to: CGPoint(x: box.maxX, y: y))
                }
                context.strokePath()
            }
            CTFrameDraw(frame, context)
            context.restoreGState()
            return
        }
        for (index, token) in paragraph.tokens.enumerated() where token.hasEntry {
            let mark = token.wantsMark
            let boxes = rects(token, in: items)
            if index == current {
                context.setFillColor(UIColor(red: 0.43, green: 0.55, blue: 1, alpha: 0.38).cgColor)
                for box in boxes { context.addPath(UIBezierPath(roundedRect: box.insetBy(dx: -1, dy: 0), cornerRadius: 3).cgPath) }
                context.fillPath()
            } else if style.marks == .highlight {
                context.setFillColor(style.color(for: token).withAlphaComponent(0.26).cgColor)
                for box in boxes { context.addPath(UIBezierPath(roundedRect: box, cornerRadius: 3).cgPath) }
                context.fillPath()
            }
            if mark && style.marks == .underline {
                context.setStrokeColor(style.stateColor(token).cgColor)
                context.setLineWidth(max(1.5, style.fontSize / 12))
                for box in boxes {
                    let y = box.minY + max(1, box.height * 0.04)
                    context.move(to: CGPoint(x: box.minX, y: y)); context.addLine(to: CGPoint(x: box.maxX, y: y))
                }
                context.strokePath()
            }
        }
        if style.shadow { context.setShadow(offset: .zero, blur: 4, color: UIColor.black.cgColor) }
        CTFrameDraw(frame, context)
        context.restoreGState()
    }

    /// The geometry touches are mapped with: the one on screen (`drawn`), else the current layout.
    private func hitGeometry() -> (frame: CTFrame, flipY: CGFloat)? {
        if let drawn, drawn.text == paragraph.text { return (drawn.frame, drawn.flipY) }
        guard let laid = layout(width: bounds.width) else { return nil }
        return (laid.frame, flipY)
    }

    /// The line under a height in view coordinates (TextLineGeometry, PudgeCore).
    private func hitLine(_ y: CGFloat) -> Line? {
        guard let hit = hitGeometry() else { return nil }
        let items = lines(hit.frame)
        let boxes = items.map { TextLineGeometry.Line(originY: Double($0.origin.y), ascent: Double($0.ascent), descent: Double($0.descent)) }
        guard let row = TextLineGeometry.nearest(boxes, flipY: Double(hit.flipY), y: Double(y)) else { return nil }
        return items[row]
    }

    /// UTF-16 index of the character under a point in view coordinates.
    func characterIndex(at point: CGPoint) -> Int? {
        guard let item = hitLine(point.y) else { return nil }
        let x = point.x - item.origin.x
        var index = CTLineGetStringIndexForPosition(item.line, CGPoint(x: x, y: 0))
        if index > item.range.location, CTLineGetOffsetForStringIndex(item.line, index, nil) > x { index -= 1 }
        if index >= item.range.location + item.range.length { index = item.range.location + item.range.length - 1 }
        return index < item.range.location ? nil : index
    }

    /// The box of the character at a UTF-16 index in view coordinates, as it is on screen
    /// (selection diagnostics).
    func glyphRect(at index: Int) -> CGRect? {
        guard let hit = hitGeometry(), index >= 0,
              let box = rects(NSRange(location: index, length: 1), in: lines(hit.frame)).first else { return nil }
        // CoreText coordinates are flipped.
        return CGRect(x: box.minX, y: hit.flipY - box.maxY, width: box.width, height: box.height)
    }

    /// The place between two characters nearest to a point in view coordinates (a dragged handle
    /// snaps to characters): never inside a character, before the break that ends a line.
    func boundary(at point: CGPoint) -> Int? {
        guard let item = hitLine(point.y) else { return nil }
        var index = CTLineGetStringIndexForPosition(item.line, CGPoint(x: point.x - item.origin.x, y: 0))
        guard index != kCFNotFound else { return nil }
        let low = item.range.location, high = item.range.location + item.range.length
        index = min(max(index, low), high)
        let text = paragraph.text as NSString
        if index == high, index > low, index <= text.length, Self.isLineBreak(text.character(at: index - 1)) { index -= 1 }
        if index > 0, index < text.length {
            let whole = text.rangeOfComposedCharacterSequence(at: index)
            if whole.location < index {
                index = (index - whole.location) * 2 < whole.length ? whole.location : whole.location + whole.length
            }
        }
        return index
    }

    private static func isLineBreak(_ unit: unichar) -> Bool { unit == 0x0A || unit == 0x0D || unit == 0x2028 || unit == 0x2029 }

    /// Token under a point in view coordinates.
    func token(at point: CGPoint) -> Int? {
        guard let index = characterIndex(at: point) else { return nil }
        if let found = paragraph.token(at: index) { return found }
        return paragraph.token(at: index - 1)
    }

    // MARK: Selection

    /// This paragraph's part of the selection.
    private var selectedRange: NSRange? {
        guard let range = selection.range,
              let part = range.part(ofParagraph: paragraphIndex, length: (paragraph.text as NSString).length) else { return nil }
        return NSRange(location: part.lowerBound, length: part.count)
    }

    /// The selected part's boxes in view coordinates (the menu points at them).
    func selectionBoxes() -> [CGRect] {
        guard let selected = selectedRange, let laid = layout(width: bounds.width) else { return [] }
        let height = flipY
        // CoreText coordinates are flipped.
        return rects(selected, in: lines(laid.frame)).map { CGRect(x: $0.minX, y: height - $0.maxY, width: $0.width, height: $0.height) }
    }

    /// The laid-out text, one box per line in view coordinates (its glyphs' width without
    /// trailing spaces, from the descent up to the ascent and the furigana above it).
    func textBoxes() -> [CGRect] {
        guard let laid = layout(width: bounds.width) else { return [] }
        let ruby = style.furigana != .off && !(paragraph.ruby ?? []).isEmpty ? style.fontSize * 0.55 : 0
        return lines(laid.frame).compactMap { item in
            let width = CGFloat(CTLineGetTypographicBounds(item.line, nil, nil, nil)) - CGFloat(CTLineGetTrailingWhitespaceWidth(item.line))
            guard width > 0 else { return nil }
            // CoreText coordinates are flipped.
            let top = item.origin.y + max(item.ascent, style.fontSize * 1.02) + ruby
            let bottom = item.origin.y - item.descent
            return CGRect(x: item.origin.x, y: flipY - top, width: width, height: top - bottom)
        }
    }

    /// A point (view coordinates) on the text itself or within `slop` of it (SubtitleHitArea).
    func textContains(_ point: CGPoint, slop: Double = SubtitleHitArea.slop) -> Bool {
        let boxes = textBoxes().map { TextBox(x: Double($0.minX), y: Double($0.minY), width: Double($0.width), height: Double($0.height)) }
        return SubtitleHitArea.hits(x: Double(point.x), y: Double(point.y), boxes: boxes, slop: slop)
    }

    /// A selection end's caret in view coordinates: the start before its character, the end
    /// after the last selected character, on that character's line.
    func selectionCaret(_ end: TextSelectionHandle, at index: Int) -> CGRect? {
        guard let laid = layout(width: bounds.width) else { return nil }
        let items = lines(laid.frame)
        let found: Line?
        if end == .start {
            found = items.first { index >= $0.range.location && index < $0.range.location + $0.range.length } ?? items.last
        } else {
            found = items.first { index > $0.range.location && index <= $0.range.location + $0.range.length } ?? items.first
        }
        guard let item = found else { return nil }
        let x = item.origin.x + CTLineGetOffsetForStringIndex(item.line, index, nil)
        let height = item.descent + style.fontSize * 1.02
        // CoreText coordinates are flipped.
        return CGRect(x: x - 1, y: flipY - (item.origin.y - item.descent) - height, width: 2, height: height)
    }

    /// A handle's frame in window coordinates when its end is in this paragraph.
    func handleFrame(_ end: TextSelectionHandle) -> CGRect? {
        guard window != nil, let range = selection.range else { return nil }
        let place = range.position(of: end)
        guard place.paragraph == paragraphIndex, let bar = selectionCaret(end, at: place.offset) else { return nil }
        return inWindow(SelectionHandleView.area(around: bar, end: end))
    }

    func handleView(_ end: TextSelectionHandle) -> UIView? { handleViews[end] }

    /// Redraws this paragraph's part of the selection and places the handles that fall in it.
    func selectionChanged() {
        setNeedsDisplay()
        placeHandles()
    }

    private func placeHandles() {
        let range = selection.range
        for end in [TextSelectionHandle.start, .end] {
            var caretBox: CGRect?
            if let place = range?.position(of: end), place.paragraph == paragraphIndex {
                caretBox = selectionCaret(end, at: place.offset)
            }
            guard let box = caretBox else {
                handleViews[end]?.isHidden = true
                continue
            }
            let handle: SelectionHandleView
            if let existing = handleViews[end] {
                handle = existing
            } else {
                handle = SelectionHandleView(end: end)
                addSubview(handle)
                handleViews[end] = handle
            }
            handle.isHidden = false
            handle.frame = SelectionHandleView.area(around: box, end: end)
        }
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        let point = touch.windowPoint(in: self)
        // A finger on a selection handle moves the handle (TextSelectionController), unless it
        // taps on: the third tap of a triple tap lands on a short word's handles.
        if selection.takesHandle(at: point) {
            if shared == nil { SelectionTrace.log("text view's \(SelectionTrace.name(gestureRecognizer)) leaves a handle touch") }
            return false
        }
        if gestureRecognizer === tapRecognizer { selection.touchBegan(at: point) }
        return true
    }

    @objc private func tapped(_ recognizer: UITapGestureRecognizer) {
        selection.tapped(self, at: recognizer.location(in: self), window: recognizer.windowPoint)
    }

    /// What a single tap here does once no second tap came, decided when the tap lands (the
    /// subtitle line may change before it runs): the word's lookup, or the reader's bars.
    func singleTapAction(at point: CGPoint) -> () -> Void {
        if let index = token(at: point), paragraph.tokens[index].hasEntry, let lookUp = onTap { return { lookUp(index) } }
        let empty = onTapEmpty
        return { empty?() }
    }

    @objc private func pressed(_ recognizer: UILongPressGestureRecognizer) {
        let point = recognizer.windowPoint
        switch recognizer.state {
        case .began:
            guard let index = characterIndex(at: recognizer.location(in: self)),
                  let unit = TextUnits.unit(paragraph, at: index) else { selection.clear(); return }
            selection.beginPress(TextSelectionRange(paragraph: paragraphIndex, range: unit), at: point)
        case .changed:
            selection.movePress(to: point)
        case .ended, .cancelled:
            selection.endPress()
        default:
            break
        }
    }

    /// Drops the reader's current text selection (if any), e.g. when it goes back to the audio.
    static func clearActiveSelection() {
        TextSelectionController.clearActive()
    }
}

/// A text selection: in the reader one over the whole chapter (paragraph + UTF-16 offset at both
/// ends, TextSelectionRange in PudgeCore), otherwise a text view's own (a subtitle line, the word
/// card). Each paragraph view on screen draws its part and the handles that fall in it. Dragging
/// a handle, the menu and the loupe live in the reader's scroll view (or the text view), so a
/// drag goes on while the paragraph it started in scrolls away; near the reader's top or bottom
/// edge the reader scrolls along. Nothing here lays out paragraphs that are not on screen.
@MainActor
final class TextSelectionController: NSObject, UIGestureRecognizerDelegate, UIEditMenuInteractionDelegate {
    private struct Visible { weak var view: JapaneseTextView? }
    private enum Drag {
        /// A handle under the finger; `grab`: from the finger to the middle of its caret.
        case handle(TextSelectionHandle, grab: CGPoint)
        /// A long press growing by words from what it selected first.
        case press(TextSelectionRange)
    }

    /// The selection (both ends inside the paragraphs they select), nil when there is none.
    private(set) var range: TextSelectionRange?
    var hasSelection: Bool { range != nil }
    /// A paragraph by index: the reader's chapter, or a single view's own text as paragraph 0.
    var paragraph: (Int) -> TextParagraph? = { _ in nil }
    /// A selection started or a handle was taken (the reader keeps its bars visible).
    var onSelect: (() -> Void)?
    /// The selection went (a single view's: the subtitle line is free to change again).
    var onDeselect: (() -> Void)?
    var onLookUp: ((TextWord) -> Void)?
    var canListen: () -> Bool = { true }
    var onListenFrom: ((TextPosition) -> Void)?
    var canMarkHere: () -> Bool = { true }
    var onMarkHere: ((TextPosition) -> Void)?
    /// "Explain Grammar" in the menu (the reader sets it): the selection.
    var onExplain: ((TextSelectionRange) -> Void)?

    private let reader: Bool
    /// The paragraph views in the window, by view (each knows its paragraph): a view SwiftUI
    /// keeps for reuse off screen, or a second view of the same paragraph, never hides the one
    /// on screen.
    private var views: [ObjectIdentifier: Visible] = [:]
    private weak var host: UIView?
    private var handlePress: UILongPressGestureRecognizer?
    /// Without the reader the handles' drag on the text view as well: a knob inside its frame
    /// (a subtitle line's handle room) is grabbed by the view the finger is on, whatever the
    /// window's recognizer gets from the video's gesture layer and SwiftUI around it.
    private var viewHandlePress: UILongPressGestureRecognizer?
    /// The reader's long press, on its scroll view: it goes on while its paragraph scrolls away.
    private var textPress: UILongPressGestureRecognizer?
    /// The paragraph view the long press's touch landed on (UIKit's hit-testing): it begins there.
    private weak var pressedView: JapaneseTextView?
    private var outsideTap: UITapGestureRecognizer?
    private weak var scrollPan: UIPanGestureRecognizer?
    private lazy var editMenu = UIEditMenuInteraction(delegate: self)
    private var menuShown = false
    private var menuHiddenOnPurpose = false
    /// Bumped by every new selection and handle drag: an older menu's dismissal never clears a newer selection.
    private var generation = 0
    private var drag: Drag?
    /// The dragging finger in window coordinates.
    private var finger = CGPoint.zero
    private var handleTouched: CFTimeInterval = 0
    /// Moves of the handle drag so far (diagnostics).
    private var dragMoves = 0
    private var loupe: UITextLoupeSession?
    private var autoscroll: CADisplayLink?
    private var taps = TapSequence()
    /// Bumped to drop a waiting single tap (a second tap came, a selection started).
    private var tapGeneration = 0
    private static weak var active: TextSelectionController?

    /// `acrossParagraphs`: the reader's selection over its chapter (the reader sets the closures above).
    init(acrossParagraphs: Bool) {
        reader = acrossParagraphs
        super.init()
    }

    /// One text view's own selection; the menu uses the view's closures.
    convenience init(single view: JapaneseTextView) {
        self.init(acrossParagraphs: false)
        paragraph = { [weak view] index in index == 0 ? view?.paragraph : nil }
        onSelect = { [weak view] in view?.onSelect?() }
        onDeselect = { [weak view] in view?.onDeselect?() }
        onLookUp = { [weak view] word in view?.onTap?(word.token) }
        canListen = { [weak view] in view?.onListenFrom != nil }
        onListenFrom = { [weak view] place in view?.onListenFrom?(place.offset) }
        canMarkHere = { [weak view] in view?.onMarkHere != nil }
        onMarkHere = { [weak view] place in view?.onMarkHere?(place.offset) }
        attach(to: view)
    }

    // MARK: Paragraph views

    /// A paragraph view came into the window.
    func register(_ view: JapaneseTextView) {
        views = views.filter { $0.value.view != nil }
        views[ObjectIdentifier(view)] = Visible(view: view)
        if reader { reachHost(from: view) } else { placeHandlePress(in: view.window) }
        view.selectionChanged()
    }

    func unregister(_ view: JapaneseTextView) {
        views[ObjectIdentifier(view)] = nil
        if !reader {
            endDrag()
            placeHandlePress(in: nil)
        }
    }

    /// Without the reader the handles' drag listens on the window: SwiftUI's hit-testing stops at
    /// the text view's frame, and the end handle's knob hangs below the last line.
    private func placeHandlePress(in window: UIWindow?) {
        guard let press = handlePress, press.view !== window else { return }
        press.view?.removeGestureRecognizer(press)
        window?.addGestureRecognizer(press)
    }

    /// The reader's scroll view holds the handles' drag, the menu and the loupe.
    private func reachHost(from view: JapaneseTextView) {
        if let host, host.window != nil, host.window === view.window { return }
        var parent = view.superview
        while let current = parent {
            if let scroll = current as? UIScrollView {
                attach(to: scroll)
                return
            }
            parent = current.superview
        }
        if let window = view.window { attach(to: window) }
    }

    private func attach(to view: UIView) {
        if view === host { return }
        detach()
        host = view
        // Begins as the finger touches a handle (only then does it take touches): the scroll
        // view under it does not move.
        let press = makeHandlePress()
        handlePress = press
        if !reader {
            placeHandlePress(in: view.window)
            let own = makeHandlePress()
            view.addGestureRecognizer(own)
            viewHandlePress = own
        } else {
            view.addGestureRecognizer(press)
            // The long press on the text (the paragraph views leave theirs off in the reader).
            let hold = UILongPressGestureRecognizer(target: self, action: #selector(textPressed(_:)))
            hold.delegate = self
            view.addGestureRecognizer(hold)
            textPress = hold
            // A tap outside the paragraphs (an illustration, the margins) drops the selection too.
            let tap = UITapGestureRecognizer(target: self, action: #selector(tappedOutside(_:)))
            tap.cancelsTouchesInView = false
            tap.delaysTouchesEnded = false
            tap.delegate = self
            view.addGestureRecognizer(tap)
            outsideTap = tap
            if let scroll = view as? UIScrollView {
                scroll.panGestureRecognizer.addTarget(self, action: #selector(scrolled(_:)))
                scrollPan = scroll.panGestureRecognizer
            }
        }
        view.addInteraction(editMenu)
    }

    private func makeHandlePress() -> UILongPressGestureRecognizer {
        let press = UILongPressGestureRecognizer(target: self, action: #selector(handleMoved(_:)))
        press.minimumPressDuration = 0
        press.allowableMovement = .greatestFiniteMagnitude
        press.delegate = self
        return press
    }

    /// Takes the gestures and the menu off their view (a text view joined the reader's selection).
    func detach() {
        endDrag()
        if let handlePress { handlePress.view?.removeGestureRecognizer(handlePress) }
        if let viewHandlePress { viewHandlePress.view?.removeGestureRecognizer(viewHandlePress) }
        if let textPress { textPress.view?.removeGestureRecognizer(textPress) }
        if let outsideTap { outsideTap.view?.removeGestureRecognizer(outsideTap) }
        host?.removeInteraction(editMenu)
        scrollPan?.removeTarget(self, action: #selector(scrolled(_:)))
        handlePress = nil; viewHandlePress = nil; textPress = nil; outsideTap = nil; scrollPan = nil; host = nil
    }

    private var resolvingCopies = false

    /// The view on screen showing paragraph `index` (nil while it is scrolled away). Of two views
    /// of one paragraph (SwiftUI can keep the copy laid out before a rotation) the one the window
    /// shows: its hit-testing finds it over its own middle.
    private func visibleView(_ index: Int) -> JapaneseTextView? {
        let candidates = shownViews().filter { $0.paragraphIndex == index }
        // Hit-testing asks the text views about their handles (point(inside:)), which comes back here.
        guard candidates.count > 1, !resolvingCopies else { return candidates.first }
        resolvingCopies = true
        defer { resolvingCopies = false }
        return candidates.first { view in
            guard let window = view.window else { return false }
            let middle = view.inWindow(CGPoint(x: view.bounds.midX, y: view.bounds.midY))
            return Self.textView(window.hitTest(middle, with: nil)) === view
        } ?? candidates.first
    }

    /// The paragraph view a view is part of (itself, a handle in it), nil outside the text.
    private static func textView(_ view: UIView?) -> JapaneseTextView? {
        var current = view
        while let candidate = current {
            if let text = candidate as? JapaneseTextView { return text }
            current = candidate.superview
        }
        return nil
    }

    /// The paragraph views a finger can reach: in the window, not hidden, and on screen. All
    /// the geometry here (touches, handles, frames) is in window coordinates.
    private func shownViews() -> [JapaneseTextView] {
        views.values.compactMap(\.view).filter { view in
            guard let window = view.window, !view.isHidden, view.alpha > 0.01, view.bounds.width > 1 else { return false }
            var parent = view.superview
            while let current = parent, current !== window {
                if current.isHidden || current.alpha <= 0.01 { return false }
                parent = current.superview
            }
            return view.convert(view.bounds, to: window).intersects(window.bounds)
        }
        .sorted { $0.paragraphIndex < $1.paragraphIndex }
    }

    /// The window changed size (rotation, a split view, Stage Manager): the handles moved with
    /// their paragraphs (they are laid out with them); the menu follows the selection.
    func layoutChanged() {
        guard range != nil, menuShown, drag == nil else { return }
        editMenu.updateVisibleMenuPosition(animated: false)
    }

    private func length(of index: Int) -> Int {
        guard let item = paragraph(index), !item.image else { return 0 }
        return (item.text as NSString).length
    }

    // MARK: Selection

    /// The selection, normalised; the paragraphs it leaves or covers redraw.
    private func set(_ value: TextSelectionRange?) {
        let normal = value?.normalized(length: { self.length(of: $0) })
        guard normal != range else { return }
        let old = range
        range = normal
        if old != nil, normal == nil, let report = onDeselect {
            // After the event or view update that dropped it, unless a new selection came since.
            Task { @MainActor [weak self] in
                guard let self, self.range == nil else { return }
                report()
            }
        }
        if normal != nil, Self.active !== self {
            // One selection at a time on screen.
            Self.active?.clear()
            Self.active = self
        }
        // Only the paragraphs whose selected part or handles moved redraw.
        for item in views.values {
            guard let view = item.view else { continue }
            let index = view.paragraphIndex
            guard TextSelectionRange.changes(old, normal, paragraph: index, length: length(of: index)) else { continue }
            view.selectionChanged()
        }
    }

    /// Drops the selection (a tap, a menu item, another chapter, back to the audio).
    func clear() {
        endDrag()
        guard range != nil else { return }
        tapGeneration &+= 1
        hideMenu()
        set(nil)
    }

    static func clearActive() { active?.clear() }

    /// The reader went away: the selection and the closures (they hold the reader's state) go.
    func forgetActions() {
        clear()
        paragraph = { _ in nil }
        onSelect = nil; onLookUp = nil; onListenFrom = nil; onMarkHere = nil
        onExplain = nil
        canListen = { true }; canMarkHere = { true }
    }

    /// A paragraph's text changed (the next subtitle line): its selection goes.
    func textChanged(paragraph index: Int) {
        if range?.contains(paragraph: index) == true { clear() }
    }

    /// A new selection: two or three taps, a long press.
    private func begin(_ value: TextSelectionRange) {
        endDrag()
        tapGeneration &+= 1
        generation &+= 1
        hideMenu()
        set(value)
        if range != nil { onSelect?() }
    }

    // MARK: Taps and the long press

    /// A finger touched a text view (window coordinates): near the last tap it may continue the
    /// row. Only the next tap drops a waiting single tap (a scroll that starts here does not).
    func touchBegan(at point: CGPoint) {
        taps.touchBegan(time: CACurrentMediaTime(), x: Double(point.x), y: Double(point.y))
    }

    /// A touch here takes a handle: it is on one, and it is not the next tap of a double or
    /// triple tap (after two taps a short word's handles cover it).
    func takesHandle(at point: CGPoint) -> Bool {
        guard range != nil else { return false }
        return !taps.wouldContinue(time: CACurrentMediaTime(), x: Double(point.x), y: Double(point.y)) && handle(at: point) != nil
    }

    /// One tap acts after `TapSequence.singleTapDelay` unless another follows (with text
    /// selected it only drops the selection); two select the word, three the sentence (or the
    /// whole subtitle line).
    func tapped(_ view: JapaneseTextView, at point: CGPoint, window: CGPoint) {
        let count = taps.tapEnded(time: CACurrentMediaTime(), x: Double(window.x), y: Double(window.y))
        tapGeneration &+= 1
        guard count == 1 else {
            let index = view.characterIndex(at: point)
            logStart(count == 2 ? "two taps" : "three taps", at: window, view: view, index: index, how: "tapped view")
            guard let index, let unit = TextUnits.taps(count, in: view.paragraph, at: index, triple: view.tripleTap) else { return }
            begin(TextSelectionRange(paragraph: view.paragraphIndex, range: unit))
            showMenu()
            return
        }
        if range != nil { clear(); return }
        let waiting = tapGeneration
        // Decided now: the word under the finger in this text (the next subtitle line may be
        // showing when it runs, or the view may be gone).
        let action = view.singleTapAction(at: point)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(TapSequence.singleTapDelay * 1_000_000_000))
            guard let self, self.tapGeneration == waiting else { return }
            self.taps.reset()
            guard self.range == nil else { return }
            action()
        }
    }

    /// The reader's long press: the word under the finger (one character outside words),
    /// growing by words while the finger moves, over paragraphs and while the reader scrolls.
    @objc private func textPressed(_ recognizer: UILongPressGestureRecognizer) {
        let point = recognizer.windowPoint
        switch recognizer.state {
        case .began:
            // Where the touch landed (the view UIKit hit-tested), mapped with that view's drawing.
            let touched = pressedView
            pressedView = nil
            let found = hit(at: point, boundary: false, under: touched)
            logStart("long press", at: point, view: found?.view, index: found?.place.offset, how: found?.how ?? "no text", recognizer: recognizer)
            guard let place = found?.place, let item = paragraph(place.paragraph),
                  let unit = TextUnits.unit(item, at: place.offset) else { clear(); return }
            beginPress(TextSelectionRange(paragraph: place.paragraph, range: unit), at: point)
        case .changed:
            movePress(to: point)
        case .ended, .cancelled:
            endPress()
        default:
            break
        }
    }

    func beginPress(_ unit: TextSelectionRange, at point: CGPoint) {
        begin(unit)
        guard range != nil else { return }
        drag = .press(unit)
        finger = point
        UISelectionFeedbackGenerator().selectionChanged()
    }

    func movePress(to point: CGPoint) {
        guard case .press = drag else { return }
        finger = point
        follow()
        updateAutoscroll()
    }

    func endPress() {
        guard case .press = drag else { return }
        endDrag()
        showMenu()
    }

    // MARK: Handles

    /// The handle under a point in window coordinates, with a target of at least 44 × 44 points.
    func handle(at point: CGPoint) -> TextSelectionHandle? {
        guard let selected = range else { return nil }
        var best: TextSelectionHandle?
        var nearest = CGFloat.greatestFiniteMagnitude
        for end in [TextSelectionHandle.start, .end] {
            guard let frame = visibleView(selected.position(of: end).paragraph)?.handleFrame(end) else { continue }
            let area = TextSelectionGeometry.handleTarget(minX: Double(frame.minX), minY: Double(frame.minY),
                                                          width: Double(frame.width), height: Double(frame.height))
            let target = CGRect(x: area.minX, y: area.minY, width: area.width, height: area.height)
            guard target.contains(point) else { continue }
            let dx = point.x - frame.midX, dy = point.y - frame.midY
            if dx * dx + dy * dy < nearest {
                best = end
                nearest = dx * dx + dy * dy
            }
        }
        return best
    }

    /// A selection end's caret in window coordinates (while its paragraph is on screen).
    private func caretFrame(_ end: TextSelectionHandle) -> CGRect? {
        guard let selected = range else { return nil }
        let place = selected.position(of: end)
        guard let view = visibleView(place.paragraph), let bar = view.selectionCaret(end, at: place.offset) else { return nil }
        return view.inWindow(bar)
    }

    private func handleView(_ end: TextSelectionHandle) -> UIView? {
        guard let selected = range else { return nil }
        return visibleView(selected.position(of: end).paragraph)?.handleView(end)
    }

    /// Dragging a handle: the menu hides, the loupe follows the caret, the end snaps to
    /// characters; the menu comes back when the finger lifts.
    @objc private func handleMoved(_ recognizer: UILongPressGestureRecognizer) {
        let point = recognizer.windowPoint
        switch recognizer.state {
        case .began:
            handleTouched = CACurrentMediaTime()
            guard let end = handle(at: point), let host, let bar = caretFrame(end) else {
                trace(recognizer, "began without a drag (handle \(handle(at: point) != nil), host \(host != nil))")
                return
            }
            dragMoves = 0
            trace(recognizer, "began: dragging the \(end == .start ? "start" : "end") handle")
            endDrag()
            stopScrolling()
            tapGeneration &+= 1
            generation &+= 1
            hideMenu()
            let middle = CGPoint(x: bar.midX, y: bar.midY)
            drag = .handle(end, grab: CGPoint(x: middle.x - point.x, y: middle.y - point.y))
            finger = point
            UISelectionFeedbackGenerator().selectionChanged()
            loupe = UITextLoupeSession.begin(at: host.fromWindow(middle), fromSelectionWidgetView: handleView(end), in: host)
            onSelect?()
        case .changed:
            guard case .handle = drag else { return }
            dragMoves += 1
            finger = point
            follow()
            updateAutoscroll()
        case .ended, .cancelled, .failed:
            handleTouched = CACurrentMediaTime()
            guard case .handle = drag else { return }
            trace(recognizer, "\(recognizer.state == .ended ? "ended" : recognizer.state == .cancelled ? "cancelled" : "failed") after \(dragMoves) moves")
            endDrag()
            showMenu()
        default:
            break
        }
    }

    /// The dragged end follows the finger, or the long press grows to the word under it.
    private func follow() {
        guard let current = drag, let selected = range else { return }
        switch current {
        case .handle(let end, let grab):
            let target = CGPoint(x: finger.x + grab.x, y: finger.y + grab.y)
            var dragged = end
            if let place = position(at: target, boundary: true) {
                let moved = selected.moving(end, to: place)
                if let value = moved.range.normalized(length: { self.length(of: $0) }), value != selected {
                    if moved.handle != end || value.position(of: moved.handle).paragraph != selected.position(of: end).paragraph {
                        UISelectionFeedbackGenerator().selectionChanged()
                    }
                    dragged = moved.handle
                    drag = .handle(dragged, grab: grab)
                    set(value)
                }
            }
            if let host, let bar = caretFrame(dragged) {
                loupe?.move(to: host.fromWindow(target), withCaretRect: host.fromWindow(bar), trackingCaret: true)
            }
        case .press(let anchor):
            guard let place = position(at: finger, boundary: false), let item = paragraph(place.paragraph),
                  let unit = TextUnits.unit(item, at: place.offset) else { return }
            set(anchor.union(TextSelectionRange(paragraph: place.paragraph, range: unit)))
        }
    }

    /// The place under a point in window coordinates, in the paragraph on screen it falls in or
    /// the nearest one: between two characters (`boundary`) or the character under it.
    private func position(at point: CGPoint, boundary: Bool) -> TextPosition? {
        hit(at: point, boundary: boundary)?.place
    }

    private struct Hit { let place: TextPosition; let view: JapaneseTextView; let how: String }

    /// `position(at:boundary:)` with the view it was mapped in. The view under the finger is the
    /// one UIKit's hit-testing finds there (`under`: the one a touch landed on), whose drawing
    /// is what the finger is on; otherwise the paragraph on screen nearest to it in both
    /// directions (beside the reader's centred column, an iPad in landscape, the finger is in
    /// the margin). The point is mapped in exactly the chosen view (PudgeCore's `pick` gives
    /// the view, not its paragraph: a second view of the same paragraph has its lines elsewhere).
    private func hit(at point: CGPoint, boundary: Bool, under touched: JapaneseTextView? = nil) -> Hit? {
        let shown = shownViews()
        let spans = shown.map { view -> TextSelectionGeometry.Span in
            let frame = view.inWindow(view.bounds)
            return TextSelectionGeometry.Span(paragraph: view.paragraphIndex, minX: Double(frame.minX), minY: Double(frame.minY),
                                              maxX: Double(frame.maxX), maxY: Double(frame.maxY))
        }
        let found = touched ?? Self.textView(shown.first?.window?.hitTest(point, with: nil))
        let under = found.flatMap { view in shown.firstIndex { $0 === view } }
        guard let chosen = TextSelectionGeometry.pick(spans, x: Double(point.x), y: Double(point.y), under: under) else { return nil }
        let view = shown[chosen]
        let local = view.fromWindow(point)
        guard let offset = boundary ? view.boundary(at: local) : view.characterIndex(at: local) else { return nil }
        return Hit(place: TextPosition(paragraph: view.paragraphIndex, offset: offset), view: view,
                   how: chosen == under ? "hit-test" : "nearest")
    }

    /// Diagnostics of where a reader selection starts (Settings → Export diagnostics, the
    /// "[reader-selection]" lines): the finger, the scroll page, the column, the paragraph and
    /// character it went to, that character's box on screen, the view's frame and size, the
    /// window, how the view was found and how many views (and copies of one paragraph) are shown.
    private func logStart(_ gesture: String, at point: CGPoint, view: JapaneseTextView?, index: Int?, how: String,
                          recognizer: UIGestureRecognizer? = nil) {
        guard reader else { return }
        func text(_ rect: CGRect?) -> String {
            guard let rect else { return "-" }
            return String(format: "%.0f,%.0f %.0fx%.0f", rect.minX, rect.minY, rect.width, rect.height)
        }
        let scroll = host as? UIScrollView
        var page = "-"
        if let scroll, scroll.bounds.height > 0 {
            page = String(format: "%.2f (offset %.0f of %.0f)", (scroll.contentOffset.y + scroll.adjustedContentInset.top) / scroll.bounds.height,
                          scroll.contentOffset.y, scroll.contentSize.height)
        }
        let frame = view.map { $0.inWindow($0.bounds) }
        let column = frame.map { String(format: "x %.0f in %.0f…%.0f", point.x - $0.minX, $0.minX, $0.maxX) } ?? "-"
        var glyph: CGRect?
        if let view, let index, let box = view.glyphRect(at: index) { glyph = view.inWindow(box) }
        let size = view.map { String(format: "%.0fx%.0f", $0.bounds.width, $0.bounds.height) } ?? "-"
        let window = (view?.window ?? host?.window).map { String(format: "%.0fx%.0f", $0.bounds.width, $0.bounds.height) } ?? "-"
        let shown = shownViews()
        let copies = shown.count - Set(shown.map(\.paragraphIndex)).count
        // The point UIKit gives for `nil` (the window the touch is in) next to the one used, and
        // the view the recognizer reports in (its bounds; a scroll view's offset), to compare.
        var raw = "-", reference = "-"
        if let recognizer {
            let unnamed = recognizer.location(in: nil)
            raw = String(format: "%.0f,%.0f", unnamed.x, unnamed.y)
            if let source = recognizer.view {
                reference = String(describing: type(of: source)) + " " + text(source.bounds)
                    + (source.window == nil ? " no window" : " in window " + text(source.inWindow(source.bounds)))
            }
        }
        let parts: [String] = [
            gesture, "point " + String(format: "%.0f,%.0f", point.x, point.y) + " (window)", "nil-point " + raw,
            "reference " + reference, "page " + page, "column " + column,
            "paragraph " + (view.map { String($0.paragraphIndex) } ?? "-"), "index " + (index.map { String($0) } ?? "-"),
            "glyph " + text(glyph), "view " + text(frame), "size " + size, "window " + window, "via " + how,
            "shown " + String(shown.count), "copies " + String(copies),
        ]
        AppLog.write("reader-selection", parts.joined(separator: " "))
    }

    private func endDrag() {
        drag = nil
        loupe?.invalidate()
        loupe = nil
        autoscroll?.invalidate()
        autoscroll = nil
    }

    /// The finger on a handle moves the handle, not the text: the scroll view lets this touch go.
    private func stopScrolling() {
        var current: UIView? = host
        while let view = current {
            if let scroll = view as? UIScrollView {
                scroll.panGestureRecognizer.isEnabled = false
                scroll.panGestureRecognizer.isEnabled = true
                return
            }
            current = view.superview
        }
    }

    // MARK: Scrolling

    private func updateAutoscroll() {
        guard reader, drag != nil, let scroll = host as? UIScrollView, autoscrollStep(scroll) != 0 else {
            autoscroll?.invalidate()
            autoscroll = nil
            return
        }
        guard autoscroll == nil else { return }
        let link = CADisplayLink(target: self, selector: #selector(autoscrolled(_:)))
        link.add(to: .main, forMode: .common)
        autoscroll = link
    }

    private func autoscrollStep(_ scroll: UIScrollView) -> CGFloat {
        let point = scroll.fromWindow(finger)
        let top = scroll.bounds.minY + scroll.adjustedContentInset.top
        let bottom = scroll.bounds.maxY - scroll.adjustedContentInset.bottom
        return CGFloat(TextSelectionAutoscroll.step(y: Double(point.y), top: Double(top), bottom: Double(bottom)))
    }

    @objc private func autoscrolled(_ link: CADisplayLink) {
        guard drag != nil, let scroll = host as? UIScrollView else {
            autoscroll?.invalidate()
            autoscroll = nil
            return
        }
        let frames = link.targetTimestamp - link.timestamp
        let step = autoscrollStep(scroll) * (frames > 0 ? CGFloat(frames * 60) : 1)
        guard step != 0 else {
            autoscroll?.invalidate()
            autoscroll = nil
            return
        }
        let lowest = -scroll.adjustedContentInset.top
        let highest = max(lowest, scroll.contentSize.height + scroll.adjustedContentInset.bottom - scroll.bounds.height)
        let y = min(highest, max(lowest, scroll.contentOffset.y + step))
        guard y != scroll.contentOffset.y else { return }
        scroll.setContentOffset(CGPoint(x: scroll.contentOffset.x, y: y), animated: false)
        follow()
    }

    /// The menu hides while the reader scrolls and comes back once the text stops.
    @objc private func scrolled(_ recognizer: UIPanGestureRecognizer) {
        guard range != nil, drag == nil else { return }
        switch recognizer.state {
        case .began:
            hideMenu()
        case .ended, .cancelled, .failed:
            menuAfterScrolling()
        default:
            break
        }
    }

    private func menuAfterScrolling() {
        let waiting = generation
        Task { @MainActor [weak self] in
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 150_000_000)
                guard let self, self.generation == waiting, self.range != nil, self.drag == nil, !self.menuShown else { return }
                if let scroll = self.host as? UIScrollView, scroll.isDragging || scroll.isDecelerating { continue }
                self.showMenu()
                return
            }
        }
    }

    // MARK: Menu

    private func hideMenu() {
        guard menuShown else { return }
        menuShown = false
        menuHiddenOnPurpose = true
        editMenu.dismissMenu()
    }

    private func showMenu() {
        guard let host, host.window != nil, range != nil, let target = menuTarget() else { return }
        menuHiddenOnPurpose = false
        menuShown = true
        editMenu.presentEditMenu(with: UIEditMenuConfiguration(identifier: nil, sourcePoint: CGPoint(x: target.midX, y: target.minY)))
    }

    /// What is on screen of the selection, in the host's coordinates: the menu points at it.
    private func menuTarget() -> CGRect? {
        guard let host, range != nil else { return nil }
        var covered: CGRect?
        for view in shownViews() {
            for box in view.selectionBoxes() {
                let frame = view.convert(box, to: host)
                covered = covered.map { $0.union(frame) } ?? frame
            }
        }
        guard let found = covered else { return nil }
        let visible = found.intersection(host.bounds)
        return visible.isNull || visible.isEmpty ? nil : visible
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, menuFor configuration: UIEditMenuConfiguration,
                             suggestedActions: [UIMenuElement]) -> UIMenu? {
        guard let selected = range else { return nil }
        let text = selected.text(paragraph)
        let offers = selected.actions(paragraph, listen: onListenFrom != nil && canListen(),
                                      markHere: onMarkHere != nil && canMarkHere(), explain: onExplain != nil)
        var actions: [UIMenuElement] = []
        for offer in offers {
            switch offer {
            case .copy:
                actions.append(UIAction(title: "Copy", image: UIImage(systemName: "doc.on.doc")) { [weak self] _ in
                    UIPasteboard.general.string = text; self?.clear()
                })
            case .lookUp(let word):
                actions.append(UIAction(title: "Look Up", image: UIImage(systemName: "character.book.closed")) { [weak self] _ in
                    self?.clear(); self?.onLookUp?(word)
                })
            case .explain:
                actions.append(UIAction(title: "Explain Grammar", image: UIImage(systemName: "text.bubble")) { [weak self] _ in
                    self?.clear(); self?.onExplain?(selected)
                })
            case .listen(let place):
                actions.append(UIAction(title: "Listen from Here", image: UIImage(systemName: "headphones")) { [weak self] _ in
                    self?.clear(); self?.onListenFrom?(place)
                })
            case .markHere(let place):
                actions.append(UIAction(title: "Read up to Here", image: UIImage(systemName: "bookmark")) { [weak self] _ in
                    self?.clear(); self?.onMarkHere?(place)
                })
            }
        }
        return UIMenu(children: actions)
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, targetRectFor configuration: UIEditMenuConfiguration) -> CGRect {
        menuTarget() ?? .null
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, willDismissMenuFor configuration: UIEditMenuConfiguration,
                             animator: UIEditMenuInteractionAnimating) {
        menuShown = false
        guard !menuHiddenOnPurpose else { return }
        // Dismissed by a tap elsewhere: the selection goes with the menu (not when the finger took
        // a handle or scrolls the reader, or a newer selection started since).
        let waiting = generation
        animator.addCompletion { [weak self] in
            guard let self, self.generation == waiting, self.drag == nil, !self.menuHiddenOnPurpose,
                  CACurrentMediaTime() - self.handleTouched > 0.5 else { return }
            if let scroll = self.host as? UIScrollView, scroll.isTracking || scroll.isDragging || scroll.isDecelerating { return }
            self.clear()
        }
    }

    // MARK: Gestures on the host

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        let point = touch.windowPoint(in: gestureRecognizer.view ?? host)
        // The reader's long press: on the text, not on a handle.
        if gestureRecognizer === textPress {
            let takes = isOnText(touch.view) && !takesHandle(at: point)
            pressedView = takes ? Self.textView(touch.view) : nil
            return takes
        }
        guard range != nil else { return false }
        if gestureRecognizer === handlePress || gestureRecognizer === viewHandlePress {
            let takes = takesHandle(at: point)
            // Diagnostics of a touch on a handle outside the reader: who got it and who else is on it.
            if !reader, handle(at: point) != nil {
                trace(gestureRecognizer, "touch on a handle: takes \(takes), on \(touch.view.map { String(describing: type(of: $0)) } ?? "-"), recognizers [\(SelectionTrace.recognizers(touch))]")
            }
            return takes
        }
        // Taps on the text are the paragraph views' own.
        if gestureRecognizer === outsideTap { return handle(at: point) == nil && !isOnText(touch.view) }
        return true
    }

    private func isOnText(_ view: UIView?) -> Bool { Self.textView(view) != nil }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        gestureRecognizer === outsideTap
    }

    /// Without the reader a finger on a handle is the handle's before anything else's (the
    /// video's tap, pinch and pan, SwiftUI's, the slider): they wait for the handles' drag, which
    /// only takes touches on a handle and begins on the touch. The two handle drags race.
    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldBeRequiredToFailBy otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        guard !reader, gestureRecognizer === handlePress || gestureRecognizer === viewHandlePress else { return false }
        return otherGestureRecognizer !== handlePress && otherGestureRecognizer !== viewHandlePress
    }

    /// Whether a touch at a point in window coordinates belongs to the selection on screen: on a
    /// handle's target or the selected text, or any touch while a handle is dragged. The video's
    /// gestures leave such a touch to the text (SubtitleSelectionTouches).
    func ownsTouch(at point: CGPoint) -> Bool {
        guard range != nil else { return false }
        let onText = shownViews().contains { view in
            view.selectionBoxes().contains { view.inWindow($0).insetBy(dx: -6, dy: -6).contains(point) }
        }
        return SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: handle(at: point) != nil,
                                                     onSelectedText: onText, dragging: drag != nil)
    }

    /// The selection on screen (if any) owns a touch here (window coordinates).
    static func activeOwnsTouch(at point: CGPoint) -> Bool { active?.ownsTouch(at: point) ?? false }

    /// A diagnostics line about one of the handles' drags (outside the reader).
    private func trace(_ recognizer: UIGestureRecognizer, _ text: String) {
        guard !reader else { return }
        SelectionTrace.log((recognizer === handlePress ? "window" : recognizer === viewHandlePress ? "text view" : SelectionTrace.name(recognizer))
                           + " handle drag " + text)
    }

    @objc private func tappedOutside(_ recognizer: UITapGestureRecognizer) {
        if recognizer.state == .ended { clear() }
    }
}

/// Diagnostics of the selection handles in the video player (Settings → Export diagnostics, the
/// "selection" lines): which view and which recognizers a touch on a handle reached, and how the
/// handle's drag began and ended. Only touches on or near a selection write a line.
@MainActor
enum SelectionTrace {
    static func log(_ text: String) { AppLog.write("selection", text) }
    static func name(_ recognizer: UIGestureRecognizer) -> String {
        String(describing: type(of: recognizer)) + (recognizer.view.map { " on " + String(describing: type(of: $0)) } ?? "")
    }
    static func recognizers(_ touch: UITouch) -> String {
        (touch.gestureRecognizers ?? []).map { name($0) + " (\($0.state.rawValue))" }.joined(separator: ", ")
    }
}

/// A selection handle like the system's: a bar as tall as the line in the tint colour with a
/// round knob above the start or below the end. It only draws; TextSelectionController takes
/// the touches (44-point targets).
final class SelectionHandleView: UIView {
    static let knob: CGFloat = 11
    static let bar: CGFloat = 2
    let end: TextSelectionHandle

    init(end: TextSelectionHandle) {
        self.end = end
        super.init(frame: .zero)
        backgroundColor = .clear
        isOpaque = false
        isUserInteractionEnabled = false
        contentMode = .redraw
        accessibilityElementsHidden = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    /// The handle's frame around a caret (both in the text view's coordinates).
    static func area(around caret: CGRect, end: TextSelectionHandle) -> CGRect {
        CGRect(x: caret.midX - knob / 2, y: end == .start ? caret.minY - knob : caret.minY, width: knob, height: caret.height + knob)
    }

    override func draw(_ rect: CGRect) {
        tintColor.setFill()
        let knob = Self.knob
        let x = (bounds.width - Self.bar) / 2
        if end == .start {
            UIBezierPath(ovalIn: CGRect(x: 0, y: 0, width: knob, height: knob)).fill()
            UIBezierPath(rect: CGRect(x: x, y: knob / 2, width: Self.bar, height: bounds.height - knob / 2)).fill()
        } else {
            UIBezierPath(rect: CGRect(x: x, y: 0, width: Self.bar, height: bounds.height - knob / 2)).fill()
            UIBezierPath(ovalIn: CGRect(x: 0, y: bounds.height - knob, width: knob, height: knob)).fill()
        }
    }

    override func tintColorDidChange() {
        super.tintColorDidChange()
        setNeedsDisplay()
    }
}

/// Selection geometry is in window coordinates, always of the selection's own window named
/// explicitly. `location(in: nil)` is the window the touch is in, which on a rotated iPhone was
/// not the reader's: points came out in portrait (up to 852 pt down a 393 pt tall landscape
/// window) while the paragraph frames were landscape, so every touch went to a paragraph far
/// above the finger. A recognizer's or touch's point is taken in a view and converted from it.
extension UIView {
    /// A point or rectangle of this view in its window's coordinates.
    func inWindow(_ point: CGPoint) -> CGPoint { window.map { convert(point, to: $0) } ?? point }
    func inWindow(_ rect: CGRect) -> CGRect { window.map { convert(rect, to: $0) } ?? rect }
    /// A point in this view's window's coordinates in this view's own.
    func fromWindow(_ point: CGPoint) -> CGPoint { window.map { convert(point, from: $0) } ?? point }
    func fromWindow(_ rect: CGRect) -> CGRect { window.map { convert(rect, from: $0) } ?? rect }
}

extension UITouch {
    /// The touch in the window of `view` (taken in `view`, then converted), never `nil`'s window.
    func windowPoint(in view: UIView?) -> CGPoint {
        guard let view else { return location(in: nil) }
        return view.inWindow(location(in: view))
    }
}

extension UIGestureRecognizer {
    /// The gesture's point in its view's window (taken in its view, then converted).
    var windowPoint: CGPoint {
        guard let view else { return location(in: nil) }
        return view.inWindow(location(in: view))
    }
}

struct JapaneseText: UIViewRepresentable {
    let paragraph: TextParagraph
    let style: ReaderStyle
    var current: Int? = nil
    var onTap: (Int) -> Void
    var onTapEmpty: (() -> Void)? = nil
    var onSelect: (() -> Void)? = nil
    var onListenFrom: ((Int) -> Void)? = nil
    /// The read marker's UTF-16 index in this paragraph, its line after drawing, and "Read up to here".
    var marker: Int? = nil
    var onMarkerLine: ((CGRect?) -> Void)? = nil
    var onMarkHere: ((Int) -> Void)? = nil
    /// The reader's selection over all its paragraphs and this paragraph's index in the chapter;
    /// nil: the text selects on its own and its menu uses the closures above.
    var selection: TextSelectionController? = nil
    var paragraphIndex = 0
    /// What three taps select: the sentence, or the whole text (a subtitle line).
    var tripleTap: TextUnits.Triple = .sentence
    /// This text's own selection went (nil `selection` only).
    var onDeselect: (() -> Void)? = nil
    /// Room above and below the text for the handles' knobs (JapaneseTextView.handleRoom).
    var handleRoom: CGFloat = 0
    /// The line of the `current` word after drawing (JapaneseTextView.onCurrentLine).
    var onCurrentLine: ((CGRect?) -> Void)? = nil

    func makeUIView(context: Context) -> JapaneseTextView {
        let view = JapaneseTextView()
        view.setContentCompressionResistancePriority(.required, for: .vertical)
        view.use(selection, paragraph: paragraphIndex)
        return view
    }
    func updateUIView(_ view: JapaneseTextView, context: Context) {
        view.paragraph = paragraph; view.style = style
        view.use(selection, paragraph: paragraphIndex)
        view.onCurrentLine = onCurrentLine
        view.current = current
        view.onTap = onTap; view.onTapEmpty = onTapEmpty; view.onSelect = onSelect; view.onListenFrom = onListenFrom
        view.onMarkerLine = onMarkerLine; view.onMarkHere = onMarkHere; view.marker = marker
        view.tripleTap = tripleTap
        view.onDeselect = onDeselect; view.handleRoom = handleRoom
    }
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: JapaneseTextView, context: Context) -> CGSize? {
        var width = proposal.width ?? 0
        // An ideal-size question: the width the view has, else its window's (not the screen's:
        // an iPad window in landscape, a split view, Stage Manager), less the reader's margins.
        if !width.isFinite || width <= 1 {
            width = uiView.bounds.width > 1 ? uiView.bounds.width : (uiView.window?.bounds.width ?? 390) - 40
        }
        uiView.paragraph = paragraph; uiView.style = style; uiView.handleRoom = handleRoom
        return CGSize(width: width, height: uiView.height(for: width))
    }
}

extension ReaderStyle {
    /// The Mac's reader settings plus the iPhone's choices ("mac" keeps the Mac's).
    static func make(_ look: ReaderLook?, fontSize: Double, marks: String, furigana: String) -> ReaderStyle {
        let look = look ?? ReaderLook()
        var style = ReaderStyle()
        style.look = look
        style.fontSize = CGFloat(fontSize)
        style.lineHeight = CGFloat(min(2.6, max(1.4, look.lineHeight)))
        style.font = look.font
        style.text = UIColor(hex: look.textColor) ?? style.text
        style.marks = marks == "mac" ? (look.markStyle == "color" ? .color : .underline) : (ReaderStyle.Marks(rawValue: marks) ?? .underline)
        style.furigana = ReaderStyle.FuriganaMode(rawValue: furigana) ?? .mac
        return style
    }
}
