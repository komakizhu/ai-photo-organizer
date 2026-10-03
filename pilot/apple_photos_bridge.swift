import AppKit
import Foundation
import Photos
import Darwin

private let socketPath = CommandLine.arguments.dropFirst().first ?? ""
private let lock = NSLock()
private var queries: [String: [PHAsset]] = [:]
private var queryOrder: [String] = []
private var imageRequests: [String: PHImageRequestID] = [:]
private let imageManager = PHCachingImageManager()
private let thumbnailCache = NSCache<NSString, NSData>()
private var generation = UUID().uuidString
private var listenerSocket: Int32 = -1

private func statusName(_ status: PHAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .authorized: return "authorized"
    case .limited: return "limited"
    @unknown default: return "unknown"
    }
}

private func reply(_ value: [String: Any]) -> [String: Any] {
    value.merging(["generation": generation]) { _, new in new }
}

private func localDay(_ text: String, end: Bool) throws -> Date {
    let pieces = text.split(separator: "-").compactMap { Int($0) }
    guard pieces.count == 3,
          let date = Calendar.autoupdatingCurrent.date(from: DateComponents(
            year: pieces[0], month: pieces[1], day: pieces[2]
          )),
          Calendar.autoupdatingCurrent.component(.year, from: date) == pieces[0],
          Calendar.autoupdatingCurrent.component(.month, from: date) == pieces[1],
          Calendar.autoupdatingCurrent.component(.day, from: date) == pieces[2]
    else { throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "日期格式须为 YYYY-MM-DD"]) }
    return end ? Calendar.autoupdatingCurrent.date(byAdding: .day, value: 1, to: date)! : date
}

private func clearQueries() {
    lock.lock()
    let requests = Array(imageRequests.values)
    imageRequests.removeAll()
    queries.removeAll()
    queryOrder.removeAll()
    generation = UUID().uuidString
    lock.unlock()
    requests.forEach { imageManager.cancelImageRequest($0) }
    thumbnailCache.removeAllObjects()
}

private final class LibraryObserver: NSObject, PHPhotoLibraryChangeObserver {
    func photoLibraryDidChange(_ changeInstance: PHChange) { clearQueries() }
}
private let libraryObserver = LibraryObserver()

private func makeQuery(_ request: [String: Any]) throws -> (String, Int, [PHAsset]) {
    let pageSize = 60
    let cursor = request["cursor"] as? [String: Any]
    let queryID = cursor?["query"] as? String ?? UUID().uuidString
    let offset = cursor?["offset"] as? Int ?? 0
    let cursorGeneration = cursor?["generation"] as? String
    if let cursorGeneration {
        lock.lock()
        let cached = queries[queryID]
        let currentGeneration = generation
        lock.unlock()
        guard cursorGeneration == currentGeneration, let cached else {
            throw NSError(domain: "ApplePhotos", code: 409, userInfo: [NSLocalizedDescriptionKey: "图库已变化或分页已过期，请重新加载"])
        }
        guard offset % pageSize == 0 else {
            throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "分页游标无效"])
        }
        return (queryID, offset, cached)
    }

    guard offset == 0 else { throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "分页游标无效"]) }
    let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    guard status == .authorized || status == .limited else {
        throw NSError(domain: "ApplePhotos", code: 403, userInfo: [NSLocalizedDescriptionKey: "照片访问权限不可用：\(statusName(status))"])
    }
    lock.lock()
    let startingGeneration = generation
    lock.unlock()
    let options = PHFetchOptions()
    options.predicate = NSPredicate(format: "hidden == NO")
    let screenshotsOnly = request["screenshots"] as? Bool == true
    let startText = request["startDate"] as? String
    let endText = request["endDate"] as? String
    if let startText, !startText.isEmpty, let endText, !endText.isEmpty {
        let start = try localDay(startText, end: false)
        let end = try localDay(endText, end: true)
        guard start < end else { throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "开始日期不能晚于结束日期"]) }
        options.predicate = NSCompoundPredicate(andPredicateWithSubpredicates: [
            options.predicate!, NSPredicate(format: "creationDate >= %@ AND creationDate < %@", start as NSDate, end as NSDate)
        ])
    } else if (startText != nil && !(startText ?? "").isEmpty) || (endText != nil && !(endText ?? "").isEmpty) {
        throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "请同时选择开始和结束日期"])
    }
    options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
    let fetched = PHAsset.fetchAssets(with: .image, options: options)
    var assets: [PHAsset] = []
    assets.reserveCapacity(fetched.count)
    fetched.enumerateObjects { asset, _, _ in
        if !screenshotsOnly || asset.mediaSubtypes.contains(.photoScreenshot) { assets.append(asset) }
    }
    assets.sort {
        switch ($0.creationDate, $1.creationDate) {
        case let (left?, right?) where left != right: return left > right
        case (_?, nil): return true
        case (nil, _?): return false
        default: return $0.localIdentifier < $1.localIdentifier
        }
    }
    lock.lock()
    guard generation == startingGeneration else {
        lock.unlock()
        throw NSError(domain: "ApplePhotos", code: 409, userInfo: [NSLocalizedDescriptionKey: "照片读取已取消或图库已变化"])
    }
    queries[queryID] = assets
    queryOrder.append(queryID)
    while queryOrder.count > 3 {
        queries.removeValue(forKey: queryOrder.removeFirst())
    }
    lock.unlock()
    return (queryID, 0, assets)
}

private func run(_ request: [String: Any]) -> [String: Any] {
    let command = request["command"] as? String ?? ""
    switch command {
    case "status":
        return ["ok": true, "status": statusName(PHPhotoLibrary.authorizationStatus(for: .readWrite))]
    case "connect":
        let current = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if current != .notDetermined { return ["ok": true, "status": statusName(current)] }
        let semaphore = DispatchSemaphore(value: 0)
        var result = current
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { value in result = value; semaphore.signal() }
        _ = semaphore.wait(timeout: .now() + 300)
        return ["ok": true, "status": statusName(result)]
    case "assets":
        do {
            let (queryID, offset, assets) = try makeQuery(request)
            guard offset >= 0, offset <= assets.count else {
                throw NSError(domain: "ApplePhotos", code: 400, userInfo: [NSLocalizedDescriptionKey: "分页游标无效"])
            }
            let end = min(offset + 60, assets.count)
            let rows = assets[offset..<end].map { asset in
                ["assetId": asset.localIdentifier,
                 "creationDate": asset.creationDate.map { ISO8601DateFormatter().string(from: $0) } as Any? ?? NSNull(),
                 "width": asset.pixelWidth, "height": asset.pixelHeight,
                 "screenshot": asset.mediaSubtypes.contains(.photoScreenshot)] as [String: Any]
            }
            let next: Any = end < assets.count ? ["query": queryID, "offset": end, "generation": generation] : NSNull()
            return reply(["ok": true, "assets": rows, "nextCursor": next, "hasMore": end < assets.count])
        } catch {
            return ["ok": false, "error": error.localizedDescription, "code": (error as NSError).code]
        }
    case "thumbnail":
        guard let identifier = request["assetId"] as? String, !identifier.isEmpty, identifier.count < 1024 else {
            return ["ok": false, "error": "照片标识无效", "code": 400]
        }
        guard PHPhotoLibrary.authorizationStatus(for: .readWrite) == .authorized ||
                PHPhotoLibrary.authorizationStatus(for: .readWrite) == .limited else {
            return ["ok": false, "error": "照片访问权限已撤销", "code": 403]
        }
        if let cached = thumbnailCache.object(forKey: identifier as NSString) {
            return ["ok": true, "data": cached.base64EncodedString()]
        }
        let matches = PHAsset.fetchAssets(withLocalIdentifiers: [identifier], options: nil)
        guard let asset = matches.firstObject else { return ["ok": false, "error": "照片已从图库移除", "code": 404] }
        let semaphore = DispatchSemaphore(value: 0)
        var imageData: Data?
        var cloudOnly = false
        let options = PHImageRequestOptions()
        options.isNetworkAccessAllowed = false
        options.deliveryMode = .highQualityFormat
        options.resizeMode = .fast
        var requestID: PHImageRequestID = PHInvalidImageRequestID
        requestID = imageManager.requestImage(for: asset, targetSize: CGSize(width: 320, height: 320),
                                              contentMode: .aspectFill, options: options) { image, info in
            if info?[PHImageCancelledKey] as? Bool == true { semaphore.signal(); return }
            if info?[PHImageResultIsInCloudKey] as? Bool == true && image == nil { cloudOnly = true; semaphore.signal(); return }
            if let image, let tiff = image.tiffRepresentation,
               let bitmap = NSBitmapImageRep(data: tiff) {
                imageData = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.78])
                semaphore.signal()
            } else { semaphore.signal() }
        }
        lock.lock()
        let imageGeneration = generation
        imageRequests[identifier] = requestID
        lock.unlock()
        let finished = semaphore.wait(timeout: .now() + 45) == .success
        lock.lock()
        imageRequests.removeValue(forKey: identifier)
        let stillCurrent = generation == imageGeneration
        lock.unlock()
        if !finished || !stillCurrent { imageManager.cancelImageRequest(requestID) }
        guard stillCurrent else { return ["ok": false, "error": "照片读取已取消或图库已变化", "code": 409] }
        guard finished, let imageData else {
            return ["ok": false, "error": cloudOnly ? "照片尚未下载到本机" : "本地缩略图暂不可用", "code": cloudOnly ? 424 : 503]
        }
        thumbnailCache.countLimit = 100
        thumbnailCache.setObject(imageData as NSData, forKey: identifier as NSString)
        return ["ok": true, "data": imageData.base64EncodedString()]
    case "cancel", "disconnect":
        lock.lock()
        let requests = Array(imageRequests.values)
        imageRequests.removeAll()
        queries.removeAll(); queryOrder.removeAll(); generation = UUID().uuidString
        lock.unlock()
        requests.forEach { imageManager.cancelImageRequest($0) }
        thumbnailCache.removeAllObjects()
        return ["ok": true, "status": statusName(PHPhotoLibrary.authorizationStatus(for: .readWrite))]
    default:
        return ["ok": false, "error": "未知命令", "code": 400]
    }
}

private func handleClient(_ client: Int32) {
    defer { Darwin.close(client) }
    var input = Data()
    var buffer = [UInt8](repeating: 0, count: 16_384)
    while input.count < 8_000_000 {
        let count = recv(client, &buffer, buffer.count, 0)
        if count <= 0 { return }
        if let newline = buffer[..<count].firstIndex(of: 10) {
            input.append(contentsOf: buffer[..<newline]); break
        }
        input.append(contentsOf: buffer[..<count])
    }
    let result: [String: Any]
    var command = ""
    do {
        guard let request = try JSONSerialization.jsonObject(with: input) as? [String: Any] else { throw NSError(domain: "ApplePhotos", code: 400) }
        command = request["command"] as? String ?? ""
        result = run(request)
    } catch { result = ["ok": false, "error": "请求格式无效", "code": 400] }
    let data = (try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])) ?? Data("{}".utf8)
    let output = data + Data([10])
    output.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return }
        var sent = 0
        while sent < bytes.count {
            let count = send(client, base.advanced(by: sent), bytes.count - sent, 0)
            if count <= 0 { break }
            sent += count
        }
    }
    if command == "disconnect" { DispatchQueue.main.async { NSApp.terminate(nil) } }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !socketPath.isEmpty else { NSApp.terminate(nil); return }
        let parent = URL(fileURLWithPath: socketPath).deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        unlink(socketPath)
        let server = socket(AF_UNIX, SOCK_STREAM, 0)
        guard server >= 0 else { NSApp.terminate(nil); return }
        var address = sockaddr_un(); address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8) + [0]
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { Darwin.close(server); NSApp.terminate(nil); return }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in raw.copyBytes(from: bytes) }
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(server, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0, chmod(socketPath, 0o600) == 0, listen(server, 8) == 0 else {
            Darwin.close(server); NSApp.terminate(nil); return
        }
        listenerSocket = server
        PHPhotoLibrary.shared().register(libraryObserver)
        DispatchQueue.global(qos: .userInitiated).async {
            while true {
                let client = accept(server, nil, nil)
                if client >= 0 { DispatchQueue.global(qos: .userInitiated).async { handleClient(client) } }
                else { break }
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        PHPhotoLibrary.shared().unregisterChangeObserver(libraryObserver)
        if listenerSocket >= 0 { Darwin.close(listenerSocket); listenerSocket = -1 }
        if !socketPath.isEmpty { unlink(socketPath) }
        clearQueries()
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
private let delegate = AppDelegate()
application.delegate = delegate
application.run()
