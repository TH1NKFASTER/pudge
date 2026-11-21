import SwiftUI
import UIKit
import PudgeCore

// MARK: Cover-card grids of the libraries (the Mac library's card style)

/// Columns and card width for the width the grid gets (decided in PudgeCore's `LibraryGrid`).
struct LibraryLayout {
    let columns: [GridItem]
    let card: CGFloat
    init(width: CGFloat) {
        let count = LibraryGrid.columns(width: Double(width))
        card = CGFloat(LibraryGrid.cardWidth(width: Double(width)))
        columns = Array(repeating: GridItem(.fixed(card), spacing: CGFloat(LibraryGrid.spacing), alignment: .top), count: count)
    }
}

/// Width of the app's content area, measured once at the tab view (never inside a scroll view, whose
/// frame followed its landscape-wide content when the iPad turned back to portrait). 0 = unknown.
private struct LibraryContainerWidthKey: EnvironmentKey {
    static let defaultValue: CGFloat = 0
}

extension EnvironmentValues {
    var libraryContainerWidth: CGFloat {
        get { self[LibraryContainerWidthKey.self] }
        set { self[LibraryContainerWidthKey.self] = newValue }
    }
}

/// Keeps `width` at the app's content width (rotation, Split View, Stage Manager); falls back to the
/// view's own width only where no container width is known (previews).
private struct LibraryWidthModifier: ViewModifier {
    @Environment(\.libraryContainerWidth) private var container
    @Binding var width: CGFloat
    @State private var own: CGFloat = 0
    func body(content: Content) -> some View {
        content
            .onGeometryChange(for: CGFloat.self) { proxy in proxy.size.width } action: { value in
                own = value
                apply()
            }
            .onAppear { apply() }
            .onChange(of: container) { _, _ in apply() }
    }
    private func apply() {
        let value = container > 0 ? container : own
        if value > 0, abs(value - width) >= 1 { width = value }
    }
}

extension View {
    /// Keeps `width` at the width the library grids lay out for.
    func libraryWidth(_ width: Binding<CGFloat>) -> some View {
        modifier(LibraryWidthModifier(width: width))
    }

    /// Publishes this view's width as the library container width (the root tab view).
    func libraryContainer(_ width: Binding<CGFloat>) -> some View {
        onGeometryChange(for: CGFloat.self) { proxy in proxy.size.width } action: { value in
            if value > 0, abs(value - width.wrappedValue) >= 1 { width.wrappedValue = value }
        }
        .environment(\.libraryContainerWidth, width.wrappedValue)
    }

    /// A grid built anew when its column layout changes: LazyVGrid keeps stale cells (blank or
    /// missing cards) when the column count changes under it after a rotation.
    func libraryGridIdentity(_ layout: LibraryLayout) -> some View {
        id("\(layout.columns.count)x\(Int(layout.card))")
    }
}

/// The library screens' header (Anime, Light novels, Manga, Audiobooks, the same everywhere): hidden
/// until a slight pull-down (the navigation bar's search drawer), with the search field (titles)
/// and, under it while searching, "All | Only on iPhone/iPad" (items stored on this device only).
/// Closing the search (Cancel) shows everything again, as it clears the text: nothing stays
/// filtered without the header on screen.
private struct LibraryFilterHeader: ViewModifier {
    @Binding var filter: LibraryFilter
    let prompt: String
    /// "iPhone" or "iPad" (PudgeTheme.device, read by the screen's body on the main actor).
    let device: String
    @State private var presented = false
    func body(content: Content) -> some View {
        content
            .searchable(text: $filter.search, isPresented: $presented,
                        placement: .navigationBarDrawer(displayMode: .automatic), prompt: Text(prompt))
            .searchScopes($filter.scope, activation: .onSearchPresentation) {
                ForEach(LibraryScope.allCases, id: \.self) { scope in
                    Text(scope.title(device: device)).tag(scope)
                }
            }
            .onChange(of: presented) { _, open in if !open { filter = LibraryFilter() } }
    }
}

extension View {
    /// The pull-down header with the search field and "Only on iPhone/iPad" (LibraryFilterHeader).
    func libraryFilterHeader(_ filter: Binding<LibraryFilter>, prompt: String, device: String) -> some View {
        modifier(LibraryFilterHeader(filter: filter, prompt: prompt, device: device))
    }
}

extension PudgeTheme {
    static let panel2 = Color(red: 22/255, green: 36/255, blue: 56/255)
    static let cardMeta = Color(red: 24/255, green: 38/255, blue: 58/255).opacity(0.96)
    static let line = Color(red: 37/255, green: 54/255, blue: 77/255)
    static let warn = Color(red: 244/255, green: 189/255, blue: 99/255)
    static let danger = Color(red: 1, green: 107/255, blue: 125/255)
    static let muted = Color(red: 148/255, green: 166/255, blue: 189/255)
    static func tone(_ tone: CardTone) -> Color {
        switch tone {
        case .good: return good
        case .accent: return accent
        case .warn: return warn
        case .danger: return danger
        case .muted: return muted
        }
    }
    /// Card border: the status colour; a quiet line when there is nothing to do.
    static func border(_ tone: CardTone) -> Color { tone == .muted ? line : self.tone(tone) }
}

/// A section title above a grid (the Mac's home sections).
struct LibrarySectionHeader: View {
    let title: String
    var count: Int? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(title).font(.headline)
            if let count { Text("\(count)").font(.subheadline).foregroundStyle(.secondary) }
            Spacer(minLength: 0)
        }
        .padding(.top, 10)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// Corner badge of a cover: where the item is kept (top-left, as on the Mac).
enum CardBadge: Equatable {
    /// On this device; with a count when several episodes or volumes are.
    case onDevice(Int?)
    /// Deleted on the Mac, still here.
    case deletedOnMac

    @MainActor var text: String {
        switch self {
        case .onDevice(let count): return count.map { "\($0) on \(PudgeTheme.device)" } ?? "On \(PudgeTheme.device)"
        case .deletedOnMac: return "Deleted on the Mac"
        }
    }
    var symbol: String {
        switch self {
        case .onDevice: return "checkmark.circle.fill"
        case .deletedOnMac: return "trash.slash"
        }
    }
    var color: Color { self == .deletedOnMac ? PudgeTheme.warn : PudgeTheme.good }
}

/// Cover served by the Mac (the iPhone's cover cache), filling the card's width.
struct CardCover: View {
    @ObservedObject var media: MediaModel
    let path: String
    var symbol = "book.closed"
    @State private var image: UIImage?
    var body: some View {
        GeometryReader { proxy in
            Group {
                if let image { Image(uiImage: image).resizable().scaledToFill() }
                else {
                    ZStack {
                        PudgeTheme.panel
                        Image(systemName: symbol).font(.system(size: proxy.size.width * 0.3)).foregroundStyle(PudgeTheme.accent)
                    }
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height).clipped()
        }
        .accessibilityHidden(true)
        .task(id: path) { image = await media.image(path) }
    }
}

/// One library item as a cover card with the Mac card's lines (PudgeCore `CardStatusText`):
/// the title (two lines at most), the Mac's middle line when it has one (`status.detail`, or a
/// phone-only `subtitle`), and the state in its colour (`status.lines` lines), each ending in "…"
/// when it does not fit; every card of a row has the same height. The ⋯ menu sits at the top
/// right (also the long-press menu) and an optional round action at the bottom left of the
/// cover (Play). The card itself opens the item.
struct LibraryCard<Cover: View, Actions: View>: View {
    let title: String
    var subtitle: String? = nil
    /// A symbol after the title (the Mac's 🎧 of a light novel with an audiobook).
    var titleSymbol: String? = nil
    let status: CardStatus
    var badge: CardBadge? = nil
    /// Finished (an audiobook listened to the end): a "✓ Finished" mark on a dimmed cover.
    var done = false
    /// Volumes on a shelf (a pill at the bottom right of the cover).
    var count: Int? = nil
    let width: CGFloat
    var showsMenu = true
    var accessory: AnyView? = nil
    var identifier: String? = nil
    var open: (() -> Void)? = nil
    @ViewBuilder let cover: () -> Cover
    @ViewBuilder let actions: () -> Actions

    static var aspect: CGFloat { 1.4 }
    private var coverHeight: CGFloat { (width * Self.aspect).rounded() }

    var body: some View {
        if showsMenu {
            card.contextMenu { actions() }
        } else {
            card
        }
    }

    private var card: some View {
        ZStack(alignment: .topTrailing) {
            if let open {
                Button(action: open) { face }
                    .buttonStyle(.plain)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(Text(title))
                    .accessibilityValue(Text(accessibilityValue))
                    .accessibilityAddTraits(.isButton)
                    .accessibilityIdentifier(identifier ?? "")
            } else {
                face
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(Text(title))
                    .accessibilityValue(Text(accessibilityValue))
            }
            if showsMenu {
                Menu { actions() } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 30, height: 26)
                        .background(RoundedRectangle(cornerRadius: 7).fill(Color(red: 8/255, green: 15/255, blue: 26/255).opacity(0.82)))
                        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.white.opacity(0.22), lineWidth: 1))
                        .padding(6)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("More actions for \(title)")
            }
        }
        .overlay(alignment: .top) {
            if let accessory {
                accessory.padding(8).frame(width: width, height: coverHeight, alignment: .bottomLeading)
            }
        }
        .frame(width: width)
    }

    private var titleText: Text {
        guard let titleSymbol else { return Text(title) }
        return Text(title) + Text(" ") + Text(Image(systemName: titleSymbol))
    }

    private var accessibilityValue: String {
        ([subtitle ?? status.detail, status.text, badge?.text, count.map { "\($0) volumes" }].compactMap { $0 }).joined(separator: ", ")
            + (done && !status.text.hasPrefix("Finished") ? ", Finished" : "")
    }

    private var face: some View {
        VStack(spacing: 0) {
            cover()
                .frame(width: width, height: coverHeight).clipped()
                .opacity(done ? 0.55 : 1)
                .overlay(alignment: .topLeading) {
                    if let badge {
                        Image(systemName: badge.symbol).font(.caption.bold()).foregroundStyle(badge.color)
                            .padding(5).background(Circle().fill(Color.black.opacity(0.72)))
                            .padding(6)
                    }
                }
                .overlay(alignment: .center) {
                    if done {
                        Label("Finished", systemImage: "checkmark.circle.fill")
                            .font(.caption2.bold()).foregroundStyle(PudgeTheme.good).lineLimit(1)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Capsule().fill(Color.black.opacity(0.78)))
                            .overlay(Capsule().stroke(PudgeTheme.good.opacity(0.6), lineWidth: 1))
                            .accessibilityHidden(true)
                    }
                }
                .overlay(alignment: .bottomTrailing) {
                    if let count, count > 1 {
                        Text("\(count)").font(.caption2.bold()).foregroundStyle(.white)
                            .frame(minWidth: 26, minHeight: 20).padding(.horizontal, 4)
                            .background(Capsule().fill(Color(red: 5/255, green: 12/255, blue: 22/255).opacity(0.88)))
                            .overlay(Capsule().stroke(Color.white.opacity(0.38), lineWidth: 1))
                            .padding(6)
                    }
                }
                .overlay(alignment: .bottom) {
                    if let progress = status.progress, progress > 0 {
                        GeometryReader { proxy in
                            ZStack(alignment: .leading) {
                                Rectangle().fill(Color.white.opacity(0.18))
                                Rectangle().fill(done ? PudgeTheme.good : PudgeTheme.tone(status.tone == .muted ? .accent : status.tone))
                                    .frame(width: proxy.size.width * CGFloat(progress))
                            }
                        }
                        .frame(height: 3)
                    }
                }
            VStack(spacing: 3) {
                titleText.font(.caption.bold()).foregroundStyle(.white)
                    .lineLimit(2).truncationMode(.tail).multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity, minHeight: 30, alignment: .top)
                if let line = subtitle ?? status.detail {
                    Text(line).font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color(red: 219/255, green: 231/255, blue: 1))
                        .lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 0)
                Text(status.text).font(.system(size: 10, weight: .semibold)).foregroundStyle(PudgeTheme.tone(status.tone))
                    .lineLimit(status.lines).truncationMode(.tail).multilineTextAlignment(.center)
            }
            .padding(.horizontal, 5).padding(.top, 6).padding(.bottom, 7)
            .frame(width: width, height: 78)
            .background(PudgeTheme.cardMeta)
        }
        .background(PudgeTheme.panel2)
        .clipShape(RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(PudgeTheme.border(status.tone), lineWidth: 2))
        .shadow(color: .black.opacity(0.2), radius: 7, y: 3)
        .contentShape(RoundedRectangle(cornerRadius: 9))
    }
}

extension LibraryCard where Actions == EmptyView {
    /// A card without a ⋯ menu.
    init(title: String, subtitle: String? = nil, status: CardStatus, badge: CardBadge? = nil, width: CGFloat,
         open: (() -> Void)? = nil, @ViewBuilder cover: @escaping () -> Cover) {
        self.init(title: title, subtitle: subtitle, status: status, badge: badge, width: width, showsMenu: false,
                  open: open, cover: cover, actions: { EmptyView() })
    }
}

/// The round Play on a card's cover. Never `.disabled`: a disabled button passes the tap to the
/// card under it (which opens the item); `dimmed` only greys it while the action would be ignored.
struct CardPlayButton: View {
    let label: String
    var hint: String? = nil
    var busy = false
    var dimmed = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().fill(PudgeTheme.accent)
                if busy { ProgressView().tint(.white) }
                else { Image(systemName: "play.fill").font(.system(size: 15, weight: .bold)).foregroundStyle(.white).offset(x: 1) }
            }
            .frame(width: 38, height: 38)
            .opacity(dimmed && !busy ? 0.55 : 1)
            .shadow(color: .black.opacity(0.4), radius: 4, y: 2)
            .frame(width: 44, height: 44)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityHint(hint ?? "")
    }
}

/// "On this device" state of a light novel or manga volume for its card.
extension MediaModel {
    func cardDownload(_ kind: String, _ id: Int) -> CardDownload {
        if let job = readingJobs["\(kind):\(id)"] { return .running(phase: job.phase, fraction: job.fraction) }
        return isDownloaded(kind, id) ? .downloaded : .none
    }
    func cardBadge(_ kind: String, _ id: Int) -> CardBadge? {
        if let record = readingDownloads["\(kind):\(id)"] { return record.deletedOnMacAt != nil ? .deletedOnMac : .onDevice(nil) }
        return nil
    }
    /// Volumes of a shelf kept here (nil when none).
    func cardBadge(_ kind: String, ids: [Int]) -> CardBadge? {
        let kept = ids.filter { isDownloaded(kind, $0) }.count
        guard kept > 0 else { return nil }
        if ids.contains(where: { readingDownloads["\(kind):\($0)"]?.deletedOnMacAt != nil }) { return .deletedOnMac }
        return .onDevice(ids.count > 1 ? kept : nil)
    }
}

/// The "Deleted on the Mac" explanation inside a volume's ⋯ menu (was under the row).
struct DeletedOnMacNote: View {
    @ObservedObject var media: MediaModel
    let kind: String
    let id: Int
    var body: some View {
        if let record = media.readingDownloads["\(kind):\(id)"], let deleted = record.deletedOnMacAt {
            if (record.openedAt ?? 0) >= deleted {
                Text("Deleted on the Mac · kept on this \(PudgeTheme.device) until you remove it")
            } else {
                Text("Deleted on the Mac · removed from this \(PudgeTheme.device) on \(Date(timeIntervalSince1970: deleted + ReadingRevisions.keepDeleted).formatted(date: .abbreviated, time: .omitted)) unless you open it")
            }
        }
    }
}
