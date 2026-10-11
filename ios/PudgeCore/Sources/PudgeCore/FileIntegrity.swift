import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

public enum FileIntegrity {
    public static func sha256(_ url: URL) throws -> String {
        #if canImport(CryptoKit)
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        var hash = SHA256()
        while let chunk = try file.read(upToCount: 1024 * 1024), !chunk.isEmpty { hash.update(data: chunk) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
        #else
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
        process.arguments = ["dgst", "-sha256", url.path]
        let pipe = Pipe(); process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
        guard process.terminationStatus == 0,
              let hash = String(data: data, encoding: .utf8)?.split(separator: " ").last?.trimmingCharacters(in: .whitespacesAndNewlines),
              hash.count == 64 else { throw PudgeError.invalid("SHA-256 mismatch") }
        return hash
        #endif
    }
    public static func verify(_ url: URL, sha256 expected: String, sizeBytes: Int64) throws {
        let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? -1
        guard Int64(size) == sizeBytes, try sha256(url) == expected else { throw PudgeError.invalid("File size or SHA-256 does not match") }
    }
}
