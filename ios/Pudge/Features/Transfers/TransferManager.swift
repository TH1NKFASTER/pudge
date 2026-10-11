import Foundation
import UIKit
import PudgeCore

final class TransferManager: PinnedDelegate, URLSessionDownloadDelegate, @unchecked Sendable {
    let store: LocalStore
    private var connection: MacConnection
    var onChange: (() -> Void)?
    private let stateLock = NSLock()
    private var downloading = Set<String>()
    private var restored = false
    private var lastProgress: [String: Double] = [:]
    lazy var transfers: URLSession = {
        let id = (Bundle.main.bundleIdentifier ?? "Pudge") + ".audio-transfers." + connection.mac.serverId + "." + String(pin.prefix(12))
        let config = URLSessionConfiguration.background(withIdentifier: id)
        config.sessionSendsLaunchEvents = true; config.isDiscretionary = false
        config.httpMaximumConnectionsPerHost = 2; config.timeoutIntervalForResource = 7 * 86400
        let queue = OperationQueue(); queue.maxConcurrentOperationCount = 1
        return URLSession(configuration: config, delegate: self, delegateQueue: queue)
    }()
    init(store: LocalStore, connection: MacConnection) throws {
        self.store = store; self.connection = connection
        guard let origin = URL(string: connection.mac.baseURL) else { throw PudgeError.invalid("Wrong Mac") }
        super.init(pin: connection.mac.certificateSHA256, origin: origin)
        restore()
    }
    func restore() {
        transfers.getAllTasks { [weak self] tasks in
            guard let self else { return }
            self.stateLock.lock(); self.downloading = Set(tasks.compactMap(\.taskDescription)); self.restored = true; self.stateLock.unlock()
            let active = Set(tasks.compactMap(\.taskDescription))
            for var record in (try? self.store.transfers()) ?? [] where record.status == "running" || record.status == "verifying" {
                if !active.contains(record.id) { record.status = "paused"; try? self.store.saveTransfer(record) }
            }
            self.notify()
        }
    }
    func updateConnection(_ client: MacConnection) {
        stateLock.lock(); connection = client; stateLock.unlock()
    }
    func cancelAll() { transfers.getAllTasks { tasks in tasks.forEach { $0.cancel() } } }
    func notify() { DispatchQueue.main.async { [weak self] in self?.onChange?() } }
    func download(_ book: AudioManifest) throws {
        try book.validate()
        let newParts = try book.resources.filter { part in
            guard let local = try store.resource(part.resourceId) else { return true }
            return !local.state.verified || local.state.sha256 != part.sha256 || !FileManager.default.fileExists(atPath: local.url.path)
        }
        let stats = try FileManager.default.attributesOfFileSystem(forPath: store.directory.path)
        let total = (stats[.systemSize] as? NSNumber)?.int64Value ?? 0
        let free = (stats[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        let records = try store.transfers()
        let pending = records.filter { ["queued", "running", "verifying"].contains($0.status) }
        let existing = Set(pending.map(\.id))
        let required = newParts.filter { !existing.contains($0.resourceId) }.reduce(Int64(0)) { $0 + $1.sizeBytes }
        let reserved = pending.reduce(Int64(0)) { $0 + 2 * $1.part.sizeBytes }
        guard StoragePolicy.canDownload(totalBytes: total, freeBytes: free, queuedBytes: reserved, newBytes: required) else {
            throw PudgeError.invalid("Not enough space including temporary files and the reserve. Free up space on this device.")
        }
        for part in newParts {
            var record = records.first(where: { $0.id == part.resourceId }) ?? TransferRecord(assetId: book.assetId, part: part)
            guard record.part.sha256 == part.sha256 else { throw PudgeError.invalid("The resource edition changed") }
            record.status = "queued"; try store.saveTransfer(record); try start(record)
        }
        notify()
    }
    func retry(_ id: String) throws {
        guard let record = try store.transfers().first(where: { $0.id == id }) else { return }
        try start(record)
    }
    private func start(_ value: TransferRecord) throws {
        stateLock.lock()
        guard restored else { stateLock.unlock(); throw PudgeError.invalid("Restoring background downloads; try again in a few seconds") }
        let client = connection
        let inserted = downloading.insert(value.id).inserted
        stateLock.unlock()
        guard inserted else { return }
        var record = value
        do {
            var request = try client.request("/api/v2/resources/\(record.part.resourceId)/content?sha256=\(record.part.sha256)")
            // Large audio waits for Wi-Fi unless cellular downloads are enabled in Settings.
            let cellular = UserDefaults.standard.bool(forKey: "pudge.cellularDownloads")
            request.allowsCellularAccess = cellular; request.allowsExpensiveNetworkAccess = cellular
            // Restarting from a fresh authenticated URL also recovers sessions
            // whose opaque resume data no longer refers to the current LAN IP.
            let task: URLSessionDownloadTask
            if record.canResume(identity: client.taskIdentity), let resumeData = record.resumeData {
                task = transfers.downloadTask(withResumeData: resumeData)
            } else { task = transfers.downloadTask(with: request); record.receivedBytes = 0 }
            task.taskDescription = record.id
            record.status = "running"; record.resumeData = nil; record.message = nil; record.resumeIdentity = client.taskIdentity
            try store.saveTransfer(record); task.resume()
        } catch {
            stateLock.lock(); downloading.remove(value.id); stateLock.unlock(); throw error
        }
    }
    func pause(_ id: String) {
        transfers.getAllTasks { [weak self] tasks in
            guard let self else { return }
            for case let task as URLSessionDownloadTask in tasks where task.taskDescription == id {
                task.cancel { resumeData in
                    guard var record = try? self.store.transfers().first(where: { $0.id == id }) else { return }
                    record.status = "paused"
                    if let resumeData { record.resumeData = resumeData }
                    try? self.store.saveTransfer(record)
                    self.notify()
                }
            }
        }
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard let id = downloadTask.taskDescription,
              var record = try? store.transfers().first(where: { $0.id == id }) else { return }
        do {
            guard let response = downloadTask.response as? HTTPURLResponse, [200, 206].contains(response.statusCode) else { throw PudgeError.invalid("The Mac did not send the file") }
            record.status = "verifying"; try store.saveTransfer(record); notify()
            let folder = store.directory.appendingPathComponent("resources", isDirectory: true)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let temporary = folder.appendingPathComponent(UUID().uuidString + ".partial")
            defer { try? FileManager.default.removeItem(at: temporary) }
            try FileManager.default.moveItem(at: location, to: temporary)
            try FileIntegrity.verify(temporary, sha256: record.part.sha256, sizeBytes: record.part.sizeBytes)
            let ext = (record.part.fileName as NSString).pathExtension
            var final = folder.appendingPathComponent(record.part.resourceId + "." + ext)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: final.path) {
                do {
                    try FileIntegrity.verify(final, sha256: record.part.sha256, sizeBytes: record.part.sizeBytes)
                } catch {
                    try FileManager.default.removeItem(at: final)
                    try FileManager.default.moveItem(at: temporary, to: final)
                }
            } else {
                try FileManager.default.moveItem(at: temporary, to: final)
            }
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            try final.setResourceValues(values)
            try store.markVerified(record.part, at: final)
            record.status = "complete"; record.resumeData = nil; record.receivedBytes = record.part.sizeBytes; try store.saveTransfer(record)
        } catch {
            record.status = "failed"; record.message = error.localizedDescription; try? store.saveTransfer(record)
            AppLog.write("transfer", record.part.fileName + " failed: " + error.localizedDescription)
        }
        notify()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let id = task.taskDescription {
            stateLock.lock(); downloading.remove(id); lastProgress.removeValue(forKey: id); stateLock.unlock()
            if let error, var record = try? store.transfers().first(where: { $0.id == id }) {
                record.status = (error as NSError).code == NSURLErrorCancelled ? "paused" : "failed"
                record.message = error.localizedDescription
                record.resumeData = (error as NSError).userInfo[NSURLSessionDownloadTaskResumeData] as? Data
                try? store.saveTransfer(record)
                AppLog.write("transfer", record.part.fileName + " " + record.status + ": " + error.localizedDescription)
            }
        }
        notify()
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard let id = downloadTask.taskDescription else { return }
        let now = ProcessInfo.processInfo.systemUptime
        stateLock.lock()
        let previous = lastProgress[id] ?? 0
        if now - previous < 0.5 { stateLock.unlock(); return }
        lastProgress[id] = now; stateLock.unlock()
        if var record = try? store.transfers().first(where: { $0.id == id }) {
            record.receivedBytes = max(0, min(record.part.sizeBytes, totalBytesWritten))
            try? store.saveTransfer(record); notify()
        }
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            let completion = PudgeAppDelegate.backgroundCompletion
            PudgeAppDelegate.backgroundCompletion = nil; completion?()
        }
    }
}
