import SwiftUI
import Charts
import PudgeCore

struct StatsScreen: View {
    @ObservedObject var media: MediaModel
    @AppStorage("pudge.statsPeriod") private var period = "30d"
    @State private var selectedDay: String?
    /// Stacking order (bottom to top) and colours of the content types; the Mac's Statistics page uses the same.
    static let kindOrder = ["anime", "light_novel", "manga", "audiobook", "visual_novel", "light_novel+audiobook", "simultaneous", "review", "unknown"]
    static func kindColor(_ kind: String) -> Color {
        switch kind {
        case "anime": return Color(red: 0x39/255, green: 0x87/255, blue: 0xe5/255)
        case "light_novel": return Color(red: 0xd9/255, green: 0x59/255, blue: 0x26/255)
        case "manga": return Color(red: 0x19/255, green: 0x9e/255, blue: 0x70/255)
        case "audiobook": return Color(red: 0xc9/255, green: 0x85/255, blue: 0x00/255)
        case "visual_novel": return Color(red: 0xd5/255, green: 0x51/255, blue: 0x81/255)
        case "light_novel+audiobook": return Color(red: 0x00/255, green: 0x83/255, blue: 0x00/255)
        case "simultaneous": return Color(red: 0x90/255, green: 0x85/255, blue: 0xe9/255)
        case "review": return Color(red: 0xe6/255, green: 0x67/255, blue: 0x67/255)
        default: return Color(red: 0x8a/255, green: 0x9b/255, blue: 0xb3/255)
        }
    }
    struct DayPart: Identifiable {
        let day: String
        let kind: String
        let seconds: Double
        var id: String { day + "|" + kind }
    }
    /// Per-day time by content type; time the Mac sent without a type is shown as "unknown".
    static func dayParts(_ days: [StatsDay]) -> [DayPart] {
        days.flatMap { day -> [DayPart] in
            var byKind: [String: Double] = [:]
            for (kind, seconds) in day.breakdown where seconds > 0 {
                byKind[kindOrder.contains(kind) ? kind : "unknown", default: 0] += seconds
            }
            let rest = day.seconds - byKind.values.reduce(0, +)
            if rest > 1 { byKind["unknown", default: 0] += rest }
            return kindOrder.compactMap { kind in byKind[kind].map { DayPart(day: day.day, kind: kind, seconds: $0) } }
        }
    }
    static func kindName(_ kind: String) -> String {
        switch kind {
        case "anime": return "Anime"
        case "light_novel": return "Light novels"
        case "audiobook": return "Audiobooks"
        case "light_novel+audiobook": return "Read along"
        case "manga": return "Manga"
        case "visual_novel": return "Visual novels"
        case "review", "study": return "Reviews"
        case "simultaneous": return "Simultaneous"
        case "unknown": return "Other"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    var body: some View {
        NavigationStack {
            List {
                Picker("Period", selection: $period) {
                    Text("7 days").tag("7d"); Text("30 days").tag("30d"); Text("Year").tag("year"); Text("All").tag("all")
                }.pickerStyle(.segmented)
                if let error = media.statsError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
                if let stats = media.stats {
                    Section {
                        HStack {
                            tile(PudgeFormat.text(stats.totalSeconds), "Total")
                            tile("\(stats.activeDays)", "Active days")
                            tile("\(stats.streak)", "Day streak")
                        }
                        if stats.days.count > 1 {
                            let parts = Self.dayParts(stats.days)
                            let kinds = Self.kindOrder.filter { kind in parts.contains { $0.kind == kind } }
                            // Each day's bar is split into blocks by content type, like on the Mac.
                            Chart(parts) { part in
                                BarMark(x: .value("Day", part.day), y: .value("Minutes", part.seconds / 60))
                                    .foregroundStyle(by: .value("Type", Self.kindName(part.kind)))
                                    .opacity(selectedDay == nil || selectedDay == part.day ? 1 : 0.45)
                            }
                            .chartForegroundStyleScale(domain: kinds.map { Self.kindName($0) }, range: kinds.map { Self.kindColor($0) })
                            .chartLegend(position: .bottom, alignment: .leading)
                            .chartXScale(domain: stats.days.map(\.day))
                            .chartXSelection(value: $selectedDay)
                            .chartXAxis(.hidden)
                            .chartYAxisLabel("min")
                            .frame(height: 170)
                            if let day = selectedDay, let picked = stats.days.first(where: { $0.day == day }) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(day + " · " + PudgeFormat.text(picked.seconds)).font(.caption.bold())
                                    ForEach(parts.filter { $0.day == day }) { part in
                                        Text(Self.kindName(part.kind) + ": " + PudgeFormat.text(part.seconds)).font(.caption2).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }
                    if let highlights = stats.highlights {
                        let rows = Self.highlightRows(highlights)
                        if !rows.isEmpty {
                            // The same stats cards as on the Mac's Statistics Overview.
                            Section("Stats") {
                                LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 8)], alignment: .leading, spacing: 8) {
                                    ForEach(rows) { row in highlightCard(row) }
                                }
                                .listRowInsets(EdgeInsets(top: 8, leading: 8, bottom: 8, trailing: 8))
                            }
                        }
                    }
                    // Only types with time in the period (nothing for what was not done).
                    let active = stats.breakdown.filter { $0.value >= 1 }.sorted { $0.value > $1.value }
                    if !active.isEmpty {
                        Section("By type") {
                            ForEach(active, id: \.key) { kind, seconds in
                                LabeledContent(Self.kindName(kind), value: PudgeFormat.text(seconds))
                            }
                        }
                    }
                    if !stats.works.isEmpty {
                        Section("Most time") {
                            ForEach(stats.works.prefix(10)) { work in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(work.title).lineLimit(1)
                                    Text(Self.kindName(work.kind) + " · " + PudgeFormat.text(work.seconds)).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                } else if media.loadingStats {
                    ProgressView()
                }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Statistics")
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .refreshable { await media.refreshStats(period: period) }
            .task(id: period) { await media.refreshStats(period: period) }
        }
    }
    struct HighlightRow: Identifiable {
        let title: String
        let value: String
        let note: String
        var id: String { title }
    }
    static func count(_ value: Double) -> String { Int(value.rounded()).formatted() }
    /// Cards in the Mac's order; estimates say so, and nothing is shown for what is unknown.
    static func highlightRows(_ h: StatsHighlights) -> [HighlightRow] {
        var rows: [HighlightRow] = []
        if h.longestStreakDays > 0 {
            rows.append(HighlightRow(title: "Longest streak", value: "\(h.longestStreakDays) " + (h.longestStreakDays == 1 ? "day" : "days"), note: "active days in a row"))
        }
        if let day = h.busiestDay, let seconds = day.seconds {
            rows.append(HighlightRow(title: "Busiest day", value: PudgeFormat.text(seconds), note: day.day ?? ""))
        }
        if let session = h.longestSession, let seconds = session.seconds {
            rows.append(HighlightRow(title: "Longest session", value: PudgeFormat.text(seconds), note: (session.title ?? "") + " · " + kindName(session.kind ?? "")))
        }
        if let work = h.topWork, let seconds = work.seconds {
            rows.append(HighlightRow(title: "Most time", value: work.title ?? "", note: kindName(work.kind ?? "") + " · " + PudgeFormat.text(seconds)))
        }
        if h.animeEpisodes > 0 {
            rows.append(HighlightRow(title: "Anime episodes", value: "\(h.animeEpisodes)", note: "with watched time"))
        }
        if let speed = h.lnCharactersPerMinute {
            rows.append(HighlightRow(title: "Reading speed, light novels", value: "≈ \(count(speed)) chars/min", note: "estimate"))
        }
        if let pace = h.mangaPagesPerHour {
            rows.append(HighlightRow(title: "Manga pace", value: "≈ \(count(pace)) pages/h", note: "estimate"))
        }
        if let words = h.words {
            var notes = ["estimate"]
            if words.pending > 0 { notes.append("still counting \(words.pending) " + (words.pending == 1 ? "item" : "items")) }
            if words.missing > 0 { notes.append("\(words.missing) without text yet") }
            rows.append(HighlightRow(title: "Words met", value: words.total.map { "≈ " + count($0) } ?? "—", note: notes.joined(separator: " · ")))
            if let unique = words.unique {
                rows.append(HighlightRow(title: "Different words", value: "≈ " + count(unique), note: "estimate"))
            }
            if let new = words.new {
                rows.append(HighlightRow(title: "New words met", value: "≈ " + count(new), note: "not met before"))
            }
            for kind in kindOrder {
                guard let counts = words.byKind[kind] else { continue }
                rows.append(HighlightRow(title: "Words, " + kindName(kind).lowercased(), value: "≈ " + count(counts.total), note: "\(count(counts.unique)) different · \(count(counts.new)) new"))
            }
        }
        return rows
    }
    private func highlightCard(_ row: HighlightRow) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(row.title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(row.value).font(.headline).lineLimit(1).minimumScaleFactor(0.7)
            if !row.note.isEmpty { Text(row.note).font(.caption2).foregroundStyle(.secondary).lineLimit(2) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(PudgeTheme.panel))
    }
    private func tile(_ value: String, _ title: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.title3.bold()).lineLimit(1).minimumScaleFactor(0.6)
            Text(title).font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity)
    }
}

struct PlanningRow: View {
    @ObservedObject var media: MediaModel
    let item: PlanningItem
    var body: some View {
        HStack(spacing: 12) {
            RemoteCover(media: media, path: "cover/anime/\(item.mediaId)", width: 40, symbol: "play.rectangle")
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title).font(.subheadline.bold()).lineLimit(2)
                Text(progressText).font(.caption).foregroundStyle(.secondary)
                if let next = item.nextEpisode, let at = item.nextAiringAt {
                    Text("Episode \(next) · " + Date(timeIntervalSince1970: Double(at)).formatted(date: .abbreviated, time: .shortened))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if item.readyEpisodes > 0 {
                Text("\(item.readyEpisodes) ready").font(.caption.bold()).padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(PudgeTheme.good.opacity(0.25)))
            }
        }
    }
    private var progressText: String {
        if let total = item.episodes { return "\(item.progress) / \(total) episodes" }
        return "\(item.progress) episodes watched"
    }
}
