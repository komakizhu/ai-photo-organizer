// Read-only macOS Vision scanner for an explicitly selected ordinary folder.
// JSONL output and optional path list must stay outside the photo source.
import Foundation
import ImageIO
import Vision

struct Candidate {
    let url: URL
    let bytes: Int64
    let modified: Double
}

let manager = FileManager.default
let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count >= 2 else {
    fputs("Usage: local_vision_scan IMAGE_FOLDER OUTPUT.jsonl [--paths-file LIST.json] [--workers N] [--resume]\n", stderr)
    exit(2)
}

let root = URL(fileURLWithPath: arguments[0], isDirectory: true).resolvingSymlinksInPath()
let output = URL(fileURLWithPath: arguments[1]).standardizedFileURL
var pathsFile: URL?
var workers = 4
var resume = false
var cursor = 2
while cursor < arguments.count {
    switch arguments[cursor] {
    case "--resume":
        resume = true
        cursor += 1
    case "--paths-file":
        guard cursor + 1 < arguments.count else { exit(2) }
        pathsFile = URL(fileURLWithPath: arguments[cursor + 1]).standardizedFileURL
        cursor += 2
    case "--workers":
        guard cursor + 1 < arguments.count, let parsed = Int(arguments[cursor + 1]),
              (1...4).contains(parsed) else {
            fputs("--workers must be between 1 and 4\n", stderr)
            exit(2)
        }
        workers = parsed
        cursor += 2
    default:
        fputs("Unknown scanner option: \(arguments[cursor])\n", stderr)
        exit(2)
    }
}

let rootPrefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
let resolvedOutputParent = output.deletingLastPathComponent().resolvingSymlinksInPath()
let resolvedOutput = output.resolvingSymlinksInPath()
var isDirectory: ObjCBool = false
guard manager.fileExists(atPath: root.path, isDirectory: &isDirectory), isDirectory.boolValue,
      root.pathExtension.lowercased() != "photoslibrary",
      !resolvedOutputParent.path.hasPrefix(rootPrefix),
      !resolvedOutput.path.hasPrefix(rootPrefix),
      output.path != root.path,
      resume || !manager.fileExists(atPath: output.path) else {
    fputs("Select an ordinary image folder and a new output path outside it.\n", stderr)
    exit(2)
}
if let pathsFile, pathsFile.resolvingSymlinksInPath().path.hasPrefix(rootPrefix) {
    fputs("Path list must be outside the scanned folder.\n", stderr)
    exit(2)
}

let extensions: Set<String> = ["jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif", "bmp"]
let resourceKeys: Set<URLResourceKey> = [.isRegularFileKey, .isSymbolicLinkKey,
                                         .fileSizeKey, .contentModificationDateKey]
var urls: [URL] = []
if let pathsFile {
    guard let data = try? Data(contentsOf: pathsFile),
          let paths = try? JSONSerialization.jsonObject(with: data) as? [String] else {
        fputs("Could not read the JSON path list.\n", stderr)
        exit(2)
    }
    for relative in paths {
        let relativeURL = URL(fileURLWithPath: relative)
        guard !relative.hasPrefix("/"), !relativeURL.pathComponents.contains("..") else {
            fputs("Path list contains an unsafe relative path.\n", stderr)
            exit(2)
        }
        let url = root.appendingPathComponent(relative).standardizedFileURL
        guard url.path.hasPrefix(rootPrefix),
              url.resolvingSymlinksInPath().standardizedFileURL.path == url.path,
              extensions.contains(url.pathExtension.lowercased()),
              let values = try? url.resourceValues(forKeys: resourceKeys),
              values.isRegularFile == true, values.isSymbolicLink != true else { continue }
        urls.append(url)
    }
} else if let enumerator = manager.enumerator(at: root, includingPropertiesForKeys: Array(resourceKeys)) {
    for case let url as URL in enumerator {
        if (try? url.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
            enumerator.skipDescendants()
            continue
        }
        if url.pathExtension.lowercased() == "photoslibrary" {
            enumerator.skipDescendants()
            continue
        }
        guard !url.lastPathComponent.hasPrefix("._"),
              url.resolvingSymlinksInPath().standardizedFileURL.path == url.standardizedFileURL.path,
              extensions.contains(url.pathExtension.lowercased()),
              let values = try? url.resourceValues(forKeys: resourceKeys),
              values.isRegularFile == true, values.isSymbolicLink != true else { continue }
        urls.append(url.standardizedFileURL)
    }
} else {
    fputs("Cannot enumerate input folder.\n", stderr)
    exit(2)
}

urls = Array(Set(urls.map(\.path))).map { URL(fileURLWithPath: $0) }.sorted { $0.path < $1.path }
let candidates: [Candidate] = urls.compactMap { url in
    guard let values = try? url.resourceValues(forKeys: resourceKeys),
          values.isRegularFile == true, values.isSymbolicLink != true else { return nil }
    return Candidate(url: url,
                     bytes: Int64(values.fileSize ?? 0),
                     modified: values.contentModificationDate?.timeIntervalSince1970 ?? 0)
}
if candidates.isEmpty {
    fputs("No supported image files found under the selected folder.\n", stderr)
    exit(0)
}
let signatureByPath = Dictionary(uniqueKeysWithValues: candidates.map {
    ($0.url.path, ($0.bytes, $0.modified))
})

var validPreviousRows: [String: Data] = [:]
if resume, let oldData = try? Data(contentsOf: output), !oldData.isEmpty {
    for line in String(decoding: oldData, as: UTF8.self).split(separator: "\n") {
        guard let data = String(line).data(using: .utf8),
              let row = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let path = row["path"] as? String,
              let bytes = (row["file_size_bytes"] as? NSNumber)?.int64Value,
              let modified = (row["file_modified_unix"] as? NSNumber)?.doubleValue,
              let current = signatureByPath[path], current.0 == bytes, current.1 == modified else { continue }
        validPreviousRows[path] = data + Data([10])
    }
    let repaired = validPreviousRows.keys.sorted().reduce(into: Data()) { result, path in
        if let row = validPreviousRows[path] { result.append(row) }
    }
    do { try repaired.write(to: output, options: .atomic) }
    catch { fputs("Could not repair the interrupted output: \(error)\n", stderr); exit(3) }
}

do {
    try manager.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
    if !manager.fileExists(atPath: output.path) {
        guard manager.createFile(atPath: output.path, contents: nil) else {
            fputs("Could not create output file.\n", stderr)
            exit(3)
        }
        try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output.path)
    }
} catch {
    fputs("Could not prepare output file: \(error)\n", stderr)
    exit(3)
}
guard let outputHandle = try? FileHandle(forWritingTo: output) else {
    fputs("Could not open output file.\n", stderr)
    exit(3)
}
_ = try? outputHandle.seekToEnd()

let completedBefore = Set(validPreviousRows.keys)
let pending = candidates.filter { !completedBefore.contains($0.url.path) }
let outputLock = NSLock()
let stateLock = NSLock()
var completed = completedBefore.count
var failed = 0
let queue = OperationQueue()
queue.name = "local-photo-studio.vision"
queue.maxConcurrentOperationCount = workers

func process(_ candidate: Candidate) -> [String: Any] {
    let url = candidate.url
    let relativePath = String(url.path.dropFirst(rootPrefix.count))
    var row: [String: Any] = [
        "path": url.path, "relative_path": relativePath,
        "filename": url.lastPathComponent,
        "extension": url.pathExtension.lowercased(),
        "file_size_bytes": candidate.bytes,
        "file_modified_unix": candidate.modified,
        "status": "metadata_error"
    ]
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else {
        return row
    }
    let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0
    let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue ?? 0
    row["image_width"] = width
    row["image_height"] = height
    let tiff = properties[kCGImagePropertyTIFFDictionary] as? [CFString: Any] ?? [:]
    let exif = properties[kCGImagePropertyExifDictionary] as? [CFString: Any] ?? [:]
    if let value = tiff[kCGImagePropertyTIFFMake] { row["camera_make"] = String(describing: value) }
    if let value = tiff[kCGImagePropertyTIFFModel] { row["camera_model"] = String(describing: value) }
    if let value = exif[kCGImagePropertyExifDateTimeOriginal] { row["capture_time_exif"] = String(describing: value) }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceThumbnailMaxPixelSize: 768,
        kCGImageSourceCreateThumbnailWithTransform: true
    ]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
        row["status"] = "decode_error"
        return row
    }
    let text = VNRecognizeTextRequest()
    text.recognitionLevel = .fast
    text.usesLanguageCorrection = false
    let faces = VNDetectFaceRectanglesRequest()
    let people = VNDetectHumanRectanglesRequest()
    let categories = VNClassifyImageRequest()
    do {
        try VNImageRequestHandler(cgImage: image).perform([text, faces, people, categories])
        let lines = (text.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        row["status"] = "scanned"
        row["face_count"] = faces.results?.count ?? 0
        row["human_count"] = people.results?.count ?? 0
        row["ocr_line_count"] = lines.count
        row["ocr_text_samples"] = lines.prefix(8).map { String($0.prefix(180)) }
        row["vision_labels"] = (categories.results ?? []).prefix(15).map {
            ["label": $0.identifier, "confidence": Double($0.confidence)] as [String: Any]
        }
    } catch {
        row["status"] = "vision_error"
        row["error"] = String(describing: error)
    }
    let current = try? manager.attributesOfItem(atPath: url.path)
    let currentBytes = (current?[.size] as? NSNumber)?.int64Value
    let currentModified = (current?[.modificationDate] as? Date)?.timeIntervalSince1970
    if currentBytes != candidate.bytes || currentModified != candidate.modified {
        row["status"] = "file_changed"
        row["error"] = "图片在 Vision 分析期间发生变化"
    }
    return row
}

for candidate in pending {
    queue.addOperation {
        let row = process(candidate)
        guard let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else {
            stateLock.lock()
            failed += 1
            completed += 1
            let progress = completed
            let failureCount = failed
            stateLock.unlock()
            fputs("PROGRESS\t\(progress)\t\(candidates.count)\t\(failureCount)\n", stderr)
            return
        }
        outputLock.lock()
        outputHandle.write(data)
        outputHandle.write(Data([10]))
        try? outputHandle.synchronize()
        outputLock.unlock()
        stateLock.lock()
        completed += 1
        if row["status"] as? String != "scanned" { failed += 1 }
        let progress = completed
        let failureCount = failed
        stateLock.unlock()
        fputs("PROGRESS\t\(progress)\t\(candidates.count)\t\(failureCount)\n", stderr)
    }
}
queue.waitUntilAllOperationsAreFinished()
try? outputHandle.close()
fputs("DONE\t\(candidates.count)\t\(completed)\t\(failed)\n", stderr)
print("Scanned \(candidates.count) images")
