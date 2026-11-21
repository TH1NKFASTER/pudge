import Foundation
import AVFoundation
import PudgeCore

/// Plays a prepared episode straight from the Mac (Watch): AVPlayer gets a `pudge-stream://`
/// address and asks this loader for byte ranges, which come over the Mac's pinned connection
/// with the device's token (AVPlayer could neither trust the Mac's certificate nor send the
/// token). The bytes come in blocks (StreamBlockSource): the next minutes ahead of where the
/// player reads are fetched into a bounded cache on the device (StreamBlockCache, the oldest
/// blocks go first), a seek fetches at the new place at once and stops fetches of the old one,
/// and blocks already here (also from an earlier watch of the same copy) are not fetched again.
///
/// The Mac keeps its copy while the player is open (its stream, renewed every 30 s and closed
/// with the player). When the Mac stops answering, a chunk is asked for again a few times; then
/// the player is told (`onEvent`), pauses and offers to try again or to download.
final class MacVideoStream: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    enum Event: Sendable {
        /// A chunk did not come at once: the Mac is asked again.
        case waiting
        /// Bytes come again after waiting.
        case flowing
        /// The Mac did not answer after a few tries (or refused): playback cannot go on.
        case failed(VideoStreamFailure)
    }

    let episodeId: Int
    let video: ResourceRef
    let url: URL
    /// Called from any thread.
    var onEvent: (@Sendable (Event) -> Void)?
    private let connection: MacConnection
    private let session: URLSession
    private let queue = DispatchQueue(label: "pudge.video-stream")
    private let lock = NSLock()
    private var lease: VideoStreamLease?
    private var tasks: [ObjectIdentifier: Task<Void, Never>] = [:]
    private var blocks: StreamBlockSource?
    private let durationMs: Int64?
    /// The copy streamed (a stream opened again asks for the same one).
    private let quality: VideoQuality
    private var keepAlive: Task<Void, Never>?
    private var waiting = false
    private var closed = false

    /// The blocks of every episode watched from the Mac, in Caches (iOS may also empty it).
    static let blockCache: StreamBlockCache = {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        return StreamBlockCache(folder: base.appendingPathComponent("PudgeStream", isDirectory: true),
                                limitBytes: StreamCacheSetting.limitBytes(settingMB: StreamCacheSetting.storedMB(), freeBytes: nil))
    }()

    /// The cache's limit from Settings, within the device's free space now.
    static func updateCacheLimit() {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        let free = (try? base.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage).map { Int64($0) }
        blockCache.limitBytes = StreamCacheSetting.limitBytes(settingMB: StreamCacheSetting.storedMB(), freeBytes: free.map { $0 + blockCache.totalBytes })
    }

    init(episodeId: Int, video: ResourceRef, lease: VideoStreamLease?, connection: MacConnection, durationMs: Int64? = nil,
         quality: VideoQuality = .original) {
        self.episodeId = episodeId; self.video = video; self.lease = lease; self.connection = connection; self.durationMs = durationMs
        self.quality = quality
        url = VideoStreamAddress.assetURL(resourceId: video.resourceId)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = VideoStreamRetry.timeout
        config.timeoutIntervalForResource = 120
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpMaximumConnectionsPerHost = VideoStreamRanges.readAhead + StreamPrefetch.parallel + 1
        session = URLSession(configuration: config,
                             delegate: PinnedDelegate(pin: connection.delegate.pin, origin: connection.delegate.origin),
                             delegateQueue: nil)
        super.init()
        Self.updateCacheLimit()
        // An episode downloading here meanwhile (the same copy): its part already written plays
        // from the device, only the rest comes from the Mac (play while downloading).
        blocks = StreamBlockSource(key: video.sha256, fileSize: video.sizeBytes, durationMs: durationMs, cache: Self.blockCache,
                                   local: PartialFileShare.shared) { [weak self] range in
            guard let self else { throw CancellationError() }
            return try await self.fetch(range)
        }
    }

    deinit {
        // Only here: a chunk still starting after close() would crash on an invalidated session.
        session.invalidateAndCancel()
    }

    /// A player item for this stream (a new one after a failed item; the loader stays the same).
    /// The loader is held weakly by AVFoundation: the player's owner keeps this object.
    func playerItem() -> AVPlayerItem {
        let asset = AVURLAsset(url: url)
        asset.resourceLoader.setDelegate(self, queue: queue)
        let item = AVPlayerItem(asset: asset)
        // About half a minute ahead over the local network, not the whole episode.
        item.preferredForwardBufferDuration = 30
        startKeepAlive()
        // Minutes more ahead go to the device's cache (a Wi-Fi drop does not stop the video).
        blocks?.startReadAhead()
        return item
    }

    /// The bytes on this device (the download's part, the cached blocks): the position bar's
    /// lighter track. Called about once a second.
    func availableBytes() -> [ClosedRange<Int64>] {
        locked { blocks }?.availableBytes() ?? []
    }

    /// The player went away: chunks on their way stop and the Mac may let its copy go.
    func close() {
        let (current, running, renewal, source): (VideoStreamLease?, [Task<Void, Never>], Task<Void, Never>?, StreamBlockSource?) = locked {
            defer { closed = true; lease = nil; tasks = [:]; keepAlive = nil }
            return (lease, Array(tasks.values), keepAlive, blocks)
        }
        running.forEach { $0.cancel() }
        source?.stop()
        renewal?.cancel()
        // Requests on their way stop; the session itself is invalidated in deinit.
        session.getAllTasks { $0.forEach { $0.cancel() } }
        guard let current else { return }
        let connection = self.connection
        Task {
            do { _ = try await connection.media("streams/\(current.id)/close", post: EmptyRequest(), as: OkResponse.self) }
            catch { AppLog.write("video", "stream of episode \(self.episodeId) not closed on the Mac: \(error.localizedDescription)") }
        }
    }

    // MARK: AVAssetResourceLoaderDelegate (called on `queue`)

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader,
                        shouldWaitForLoadingOfRequestedResource loadingRequest: AVAssetResourceLoadingRequest) -> Bool {
        guard loadingRequest.request.url?.scheme == VideoStreamAddress.scheme else { return false }
        let size = video.sizeBytes
        if let info = loadingRequest.contentInformationRequest {
            // Known from the Mac's answer: no request needed for it.
            info.contentType = AVFileType.mp4.rawValue
            info.contentLength = size
            info.isByteRangeAccessSupported = true
        }
        let box = LoadingRequestBox(loadingRequest)
        guard let data = loadingRequest.dataRequest,
              let wanted = VideoStreamRanges.requested(offset: data.requestedOffset, length: data.requestedLength,
                                                       toEnd: data.requestsAllDataToEndOfResource, size: size) else {
            finish(box, nil)
            return true
        }
        let key = ObjectIdentifier(loadingRequest)
        let accepted: Bool = locked {
            guard !closed else { return false }
            tasks[key] = Task { [weak self] in
                guard let self else { return }
                await self.serve(wanted, to: box)
                self.locked { self.tasks[key] = nil }
            }
            return true
        }
        if !accepted { finish(box, .unreachable) }
        return true
    }

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, didCancel loadingRequest: AVAssetResourceLoadingRequest) {
        let key = ObjectIdentifier(loadingRequest)
        let task: Task<Void, Never>? = locked { tasks.removeValue(forKey: key) }
        task?.cancel()
    }

    // MARK: Serving AVPlayer's requests

    private func serve(_ wanted: ClosedRange<Int64>, to box: LoadingRequestBox) async {
        do {
            try await deliver(wanted, to: box)
            finish(box, nil)
        } catch {
            // Cancelled by AVPlayer (a seek) or the player closed: it expects nothing more. Any
            // other cancel (the pinned certificate refused) fails the request, never left waiting.
            guard let failure = VideoStreamFailure.from(error: error, cancelled: Task.isCancelled) else { return }
            if isClosed { finish(box, failure); return }
            AppLog.write("video", "stream of episode \(episodeId) stopped at byte \(wanted.lowerBound): \(failure)")
            // The reason first: the player item fails right after, without it.
            onEvent?(.failed(failure))
            finish(box, failure)
        }
    }

    /// The request's blocks in order from where it starts (the read-ahead moves there first: a
    /// seek fetches here at once), `readAhead` of the next ones already on their way. At most
    /// VideoStreamRanges.blocksPerRequest blocks: AVPlayer takes whatever is handed over (no
    /// back-pressure) and asks again for the rest of a request finished early.
    private func deliver(_ wanted: ClosedRange<Int64>, to box: LoadingRequestBox) async throws {
        guard let source = locked({ blocks }) else { throw CancellationError() }
        let indices = StreamBlocks.covering(wanted, fileSize: video.sizeBytes, blockSize: source.blockSize,
                                            limit: VideoStreamRanges.blocksPerRequest)
        source.moved(to: wanted.lowerBound)
        for (position, index) in indices.enumerated() {
            source.warm(Array(indices.dropFirst(position + 1).prefix(VideoStreamRanges.readAhead)))
            let data = try await source.block(index)
            try Task.checkCancellation()
            if let part = StreamBlocks.slice(data, block: index, wanted: wanted, blockSize: source.blockSize) {
                respond(box, part)
            }
            // The player has these bytes now: the read-ahead follows.
            source.moved(to: max(wanted.lowerBound, Int64(index) * source.blockSize))
        }
    }

    /// One chunk, asked for again when the Mac does not answer for a moment.
    private func fetch(_ range: ClosedRange<Int64>) async throws -> Data {
        var attempt = 1
        while true {
            do {
                let data = try await fetchOnce(range)
                markWaiting(false)
                return data
            } catch {
                if Task.isCancelled || isClosed { throw CancellationError() }
                guard let failure = VideoStreamFailure.from(error: error, cancelled: false) else { throw CancellationError() }
                guard VideoStreamRetry.shouldRetry(failure, attempt: attempt), !Task.isCancelled else { throw failure }
                if VideoStreamRetry.showsWaiting(after: attempt) { markWaiting(true) }
                try await Task.sleep(nanoseconds: UInt64(VideoStreamRetry.delay(after: attempt) * 1_000_000_000))
                attempt += 1
            }
        }
    }

    private func fetchOnce(_ range: ClosedRange<Int64>) async throws -> Data {
        try Task.checkCancellation()
        let (stream, stopped): (String?, Bool) = locked { (lease?.id, closed) }
        if stopped { throw CancellationError() }
        var request = try connection.request(VideoStreamAddress.route(resourceId: video.resourceId, sha256: video.sha256, stream: stream))
        request.setValue(VideoStreamRanges.header(range), forHTTPHeaderField: "Range")
        request.timeoutInterval = VideoStreamRetry.timeout
        let (body, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw VideoStreamFailure.interrupted }
        try VideoStreamResponse.check(status: http.statusCode, contentRange: http.value(forHTTPHeaderField: "Content-Range"),
                                      received: Int64(body.count), requested: range, size: video.sizeBytes)
        return body
    }

    private func respond(_ box: LoadingRequestBox, _ data: Data) {
        queue.async {
            let loading = box.request
            guard !loading.isCancelled, !loading.isFinished else { return }
            loading.dataRequest?.respond(with: data)
        }
    }

    private func finish(_ box: LoadingRequestBox, _ failure: VideoStreamFailure?) {
        queue.async {
            let loading = box.request
            guard !loading.isCancelled, !loading.isFinished else { return }
            if let failure { loading.finishLoading(with: failure) } else { loading.finishLoading() }
        }
    }

    private func markWaiting(_ value: Bool) {
        let changed: Bool = locked {
            guard waiting != value else { return false }
            waiting = value
            return true
        }
        if changed { onEvent?(value ? .waiting : .flowing) }
    }

    // MARK: The Mac's stream (keeps its copy)

    private func startKeepAlive() {
        locked {
            guard keepAlive == nil, !closed, lease != nil else { return }
            keepAlive = Task { [weak self] in
                while !Task.isCancelled {
                    guard let every = self?.renewInterval() else { return }
                    do { try await Task.sleep(nanoseconds: UInt64(every * 1_000_000_000)) } catch { return }
                    await self?.renew()
                }
            }
        }
    }

    private func renewInterval() -> Double? { locked { lease?.renewEvery } }

    private var isClosed: Bool { locked { closed } }

    private func renew() async {
        guard let current: VideoStreamLease = locked({ lease }) else { return }
        do {
            _ = try await connection.media("streams/\(current.id)/keepalive", post: EmptyRequest(), as: OkResponse.self)
        } catch let refusal as MacRefusal where refusal.status == 422 {
            // The Mac let it go (this device was away for a while): a new one for the same copy.
            guard let opened = try? await connection.media("episodes/\(episodeId)/stream", post: VideoQualityRequest(quality), as: VideoStreamOpened.self),
                  let fresh = opened.stream else { return }
            // Another copy now (the episode changed): this one is not kept for it, the chunks
            // fail as "changed" and Try again prepares the new one.
            let sameCopy = opened.video?.sha256 == video.sha256
            let stale: Bool = locked {
                guard sameCopy, !closed else { return true }
                lease = fresh
                return false
            }
            if stale {
                _ = try? await connection.media("streams/\(fresh.id)/close", post: EmptyRequest(), as: OkResponse.self)
            } else {
                AppLog.write("video", "stream of episode \(episodeId) opened again on the Mac")
            }
        } catch {
            // Not answering now: the chunks say so if it lasts.
        }
    }

    @discardableResult
    private func locked<T>(_ body: () -> T) -> T {
        lock.lock(); defer { lock.unlock() }
        return body()
    }
}

/// AVFoundation's loading request, handed between the loader's queue and its tasks.
private final class LoadingRequestBox: @unchecked Sendable {
    let request: AVAssetResourceLoadingRequest
    init(_ request: AVAssetResourceLoadingRequest) { self.request = request }
}
