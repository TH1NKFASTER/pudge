import SwiftUI
import UIKit
import CoreText
import PudgeCore

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
        let names = font == "subtitle" ? (bold ? ["HiraginoSans-W8", "HiraginoSans-W7", "HiraKakuProN-W6"] : ["HiraginoSans-W6", "HiraKakuProN-W6"])
            : font == "gothic" || font.contains("sans") ? ["HiraginoSans-W3", "HiraKakuProN-W3"] : ["HiraMinProN-W3", "HiraMinProN-W6"]
        for name in names { if let value = UIFont(name: name, size: size) { return value } }
        return UIFont.systemFont(ofSize: size)
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
/// A long press selects text (drag to extend) and shows Copy / Look Up / Listen from here.
final class JapaneseTextView: UIView, UIEditMenuInteractionDelegate {
    var paragraph = TextParagraph(text: "", tokens: []) { didSet { if paragraph != oldValue { invalidate(); clearSelection() } } }
    var style = ReaderStyle() { didSet { if style != oldValue { invalidate() } } }
    var current: Int? { didSet { if current != oldValue { setNeedsDisplay() } } }
    var onTap: ((Int) -> Void)?
    /// A tap that is not on a word (the reader shows or hides its controls).
    var onTapEmpty: (() -> Void)?
    /// A selection started (the reader keeps its controls visible).
    var onSelect: (() -> Void)?
    /// "Listen from here" in the selection menu, with the selection's UTF-16 start.
    var onListenFrom: ((Int) -> Void)?

    /// Selected UTF-16 range; one paragraph view holds a selection at a time.
    private var selection: NSRange? { didSet { if selection != oldValue { setNeedsDisplay() } } }
    private var anchor: NSRange?
    /// Bumped by every new selection, so dismissing an older menu never clears a newer selection.
    private var selectionGeneration = 0
    private static weak var selecting: JapaneseTextView?
    private lazy var editMenu = UIEditMenuInteraction(delegate: self)

    private var framesetter: CTFramesetter?
    private var frameCache: (width: CGFloat, frame: CTFrame, height: CGFloat)?
    private var outlineFrame: CTFrame?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        isOpaque = false
        contentMode = .redraw
        addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
        addGestureRecognizer(UILongPressGestureRecognizer(target: self, action: #selector(pressed(_:))))
        addInteraction(editMenu)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    private func invalidate() {
        framesetter = nil; frameCache = nil; outlineFrame = nil
        invalidateIntrinsicContentSize(); setNeedsDisplay()
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
                result.addAttribute(strokeColorKey, value: Self.color(word.outline, opacity: word.opacity), range: range)
            } else {
                result.addAttribute(colorKey, value: Self.color(word.text, opacity: word.opacity), range: range)
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
                if style.furigana == .mac {
                    guard let index = paragraph.token(at: ruby.start), paragraph.tokens[index].wantsFurigana else { continue }
                }
                guard let range = clamp(ruby.start, ruby.end, full.length) else { continue }
                let rubyColor = outline ? UIColor.clear.cgColor : style.text.withAlphaComponent(0.85).cgColor
                let attributes: [String: Any] = [kCTRubyAnnotationSizeFactorAttributeName as String: 0.5,
                                                 kCTForegroundColorAttributeName as String: rubyColor]
                let annotation = CTRubyAnnotationCreateWithAttributes(.auto, .auto, .before, ruby.reading as CFString, attributes as CFDictionary)
                result.addAttribute(rubyKey, value: annotation, range: range)
            }
        }
        return result
    }

    private func clamp(_ start: Int, _ end: Int, _ length: Int) -> NSRange? {
        let low = max(0, start), high = min(length, end)
        return high > low ? NSRange(location: low, length: high - low) : nil
    }

    private func layout(width: CGFloat) -> (frame: CTFrame, height: CGFloat)? {
        guard width > 1 else { return nil }
        if let cache = frameCache, abs(cache.width - width) < 0.5 { return (cache.frame, cache.height) }
        let setter = framesetter ?? CTFramesetterCreateWithAttributedString(attributed() as CFAttributedString)
        framesetter = setter
        let size = CTFramesetterSuggestFrameSizeWithConstraints(setter, CFRange(location: 0, length: 0), nil,
                                                                CGSize(width: width, height: .greatestFiniteMagnitude), nil)
        let height = ceil(size.height) + 2
        let path = CGPath(rect: CGRect(x: 0, y: 0, width: width, height: height), transform: nil)
        let frame = CTFramesetterCreateFrame(setter, CFRange(location: 0, length: 0), path, nil)
        frameCache = (width, frame, height)
        if style.subtitleLook != nil {
            // Same text, fonts and ruby: the stroke-only frame lays out exactly like the fill frame.
            let outlineSetter = CTFramesetterCreateWithAttributedString(attributed(outline: true) as CFAttributedString)
            outlineFrame = CTFramesetterCreateFrame(outlineSetter, CFRange(location: 0, length: 0), path, nil)
        } else {
            outlineFrame = nil
        }
        return (frame, height)
    }

    func height(for width: CGFloat) -> CGFloat { layout(width: width)?.height ?? style.fontSize * style.lineHeight }

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

    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext(), let laid = layout(width: bounds.width) else { return }
        let frame = laid.frame
        context.saveGState()
        context.textMatrix = .identity
        context.translateBy(x: 0, y: bounds.height)
        context.scaleBy(x: 1, y: -1)
        let items = lines(frame)
        if let selection {
            context.setFillColor(UIColor.systemBlue.withAlphaComponent(0.3).cgColor)
            for box in rects(selection, in: items) { context.addPath(UIBezierPath(roundedRect: box, cornerRadius: 2).cgPath) }
            context.fillPath()
        }
        if let look = style.subtitleLook {
            if let outlineFrame { CTFrameDraw(outlineFrame, context) }
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

    /// UTF-16 index of the character under a point in view coordinates.
    private func characterIndex(at point: CGPoint) -> Int? {
        guard let laid = layout(width: bounds.width) else { return nil }
        let items = lines(laid.frame)
        guard !items.isEmpty else { return nil }
        let y = bounds.height - point.y
        let item = items.min { abs($0.origin.y + ($0.ascent - $0.descent) / 2 - y) < abs($1.origin.y + ($1.ascent - $1.descent) / 2 - y) }!
        let x = point.x - item.origin.x
        var index = CTLineGetStringIndexForPosition(item.line, CGPoint(x: x, y: 0))
        if index > item.range.location, CTLineGetOffsetForStringIndex(item.line, index, nil) > x { index -= 1 }
        if index >= item.range.location + item.range.length { index = item.range.location + item.range.length - 1 }
        return index < item.range.location ? nil : index
    }

    /// Token under a point in view coordinates.
    func token(at point: CGPoint) -> Int? {
        guard let index = characterIndex(at: point) else { return nil }
        if let found = paragraph.token(at: index) { return found }
        return paragraph.token(at: index - 1)
    }

    /// The word under a point, or the single character when it is not part of a word.
    private func unit(at point: CGPoint) -> NSRange? {
        let length = (paragraph.text as NSString).length
        guard let index = characterIndex(at: point), index < length else { return nil }
        if let token = paragraph.token(at: index).flatMap({ paragraph.tokens[safe: $0] }), token.end > token.start {
            return NSRange(location: token.start, length: token.end - token.start)
        }
        return (paragraph.text as NSString).rangeOfComposedCharacterSequence(at: index)
    }

    private func clearSelection() {
        anchor = nil
        if selection != nil { selection = nil; editMenu.dismissMenu() }
    }

    /// Drops the reader's current text selection (if any), e.g. when it goes back to the audio.
    static func clearActiveSelection() {
        selecting?.clearSelection()
    }

    @objc private func tapped(_ recognizer: UITapGestureRecognizer) {
        if selection != nil { clearSelection(); return }
        if let index = token(at: recognizer.location(in: self)), paragraph.tokens[index].hasEntry { onTap?(index) }
        else { onTapEmpty?() }
    }

    @objc private func pressed(_ recognizer: UILongPressGestureRecognizer) {
        let point = recognizer.location(in: self)
        switch recognizer.state {
        case .began:
            guard let range = unit(at: point) else { clearSelection(); return }
            if let other = Self.selecting, other !== self { other.clearSelection() }
            Self.selecting = self
            selectionGeneration &+= 1
            editMenu.dismissMenu()
            anchor = range; selection = range
            UISelectionFeedbackGenerator().selectionChanged()
            onSelect?()
        case .changed:
            guard let anchor, let range = unit(at: point) else { return }
            let low = min(anchor.location, range.location)
            let high = max(anchor.location + anchor.length, range.location + range.length)
            selection = NSRange(location: low, length: high - low)
        case .ended:
            guard selection != nil else { return }
            editMenu.presentEditMenu(with: UIEditMenuConfiguration(identifier: nil, sourcePoint: point))
        case .cancelled, .failed:
            clearSelection()
        default:
            break
        }
    }

    private var selectedText: String? {
        guard let selection, selection.location + selection.length <= (paragraph.text as NSString).length else { return nil }
        return (paragraph.text as NSString).substring(with: selection)
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, menuFor configuration: UIEditMenuConfiguration,
                             suggestedActions: [UIMenuElement]) -> UIMenu? {
        guard let selection, let text = selectedText else { return nil }
        var actions: [UIMenuElement] = [
            UIAction(title: "Copy", image: UIImage(systemName: "doc.on.doc")) { [weak self] _ in
                UIPasteboard.general.string = text; self?.clearSelection()
            },
        ]
        if let word = paragraph.tokens.indices.first(where: { index in
            let token = paragraph.tokens[index]
            return token.hasEntry && token.start < selection.location + selection.length && token.end > selection.location
        }) {
            actions.append(UIAction(title: "Look Up", image: UIImage(systemName: "character.book.closed")) { [weak self] _ in
                self?.clearSelection(); self?.onTap?(word)
            })
        }
        if let listen = onListenFrom {
            actions.append(UIAction(title: "Listen from Here", image: UIImage(systemName: "headphones")) { [weak self] _ in
                self?.clearSelection(); listen(selection.location)
            })
        }
        return UIMenu(children: actions)
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, targetRectFor configuration: UIEditMenuConfiguration) -> CGRect {
        guard let selection, let laid = layout(width: bounds.width) else { return .null }
        let boxes = rects(selection, in: lines(laid.frame))
        guard let first = boxes.first else { return .null }
        let union = boxes.dropFirst().reduce(first) { $0.union($1) }
        // CoreText coordinates are flipped.
        return CGRect(x: union.minX, y: bounds.height - union.maxY, width: union.width, height: union.height)
    }

    func editMenuInteraction(_ interaction: UIEditMenuInteraction, willDismissMenuFor configuration: UIEditMenuConfiguration,
                             animator: UIEditMenuInteractionAnimating) {
        // Dismissed by a tap elsewhere: the selection goes with the menu (unless a new one started since).
        let generation = selectionGeneration
        animator.addCompletion { [weak self] in
            guard let self, self.selectionGeneration == generation else { return }
            self.clearSelection()
        }
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

    func makeUIView(context: Context) -> JapaneseTextView {
        let view = JapaneseTextView()
        view.setContentCompressionResistancePriority(.required, for: .vertical)
        return view
    }
    func updateUIView(_ view: JapaneseTextView, context: Context) {
        view.paragraph = paragraph; view.style = style; view.current = current
        view.onTap = onTap; view.onTapEmpty = onTapEmpty; view.onSelect = onSelect; view.onListenFrom = onListenFrom
    }
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: JapaneseTextView, context: Context) -> CGSize? {
        var width = proposal.width ?? 0
        if !width.isFinite || width <= 1 { width = (uiView.window?.windowScene?.screen.bounds.width ?? 390) - 40 }
        uiView.paragraph = paragraph; uiView.style = style
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
