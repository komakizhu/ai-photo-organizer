// Independent, compact Haar-wavelet visual fingerprint for candidate review.
// Inspired by the public Haar image-retrieval method; no digiKam source is used.
// Usage: haar_fingerprint <image> [<image> ...] -> JSON on stdout.

import CoreGraphics
import Foundation
import ImageIO

private let side = 128
private let featureCount = 64

private struct Feature: Codable {
    let index: Int
    let sign: Int
}

private struct Result: Codable {
    let path: String
    let fingerprint: [Feature]
}

private func haar(_ values: inout [Double], offset: Int, stride: Int, length: Int) {
    guard length > 1 else { return }
    let half = length / 2
    let scale = 1.0 / sqrt(2.0)
    var output = [Double](repeating: 0, count: length)
    for index in 0..<half {
        let even = values[offset + (index * 2) * stride]
        let odd = values[offset + (index * 2 + 1) * stride]
        output[index] = (even + odd) * scale
        output[half + index] = (even - odd) * scale
    }
    for index in 0..<length {
        values[offset + index * stride] = output[index]
    }
}

private func decodePixels(_ path: String) throws -> [Double] {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateThumbnailAtIndex(
            source, 0,
            [kCGImageSourceCreateThumbnailFromImageAlways: true,
             kCGImageSourceThumbnailMaxPixelSize: side,
             kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary
          ) else {
        throw NSError(domain: "LocalPhotoStudio.Haar", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "image decode failed"])
    }

    var bytes = [UInt8](repeating: 0, count: side * side)
    let colorSpace = CGColorSpaceCreateDeviceGray()
    let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue)
    let width = min(side, image.width)
    let height = min(side, image.height)
    let rect = CGRect(x: (side - width) / 2, y: (side - height) / 2,
                      width: width, height: height)
    let madeContext = bytes.withUnsafeMutableBytes { buffer -> Bool in
        guard let address = buffer.baseAddress,
              let context = CGContext(data: address, width: side, height: side,
                                      bitsPerComponent: 8, bytesPerRow: side,
                                      space: colorSpace, bitmapInfo: info.rawValue) else {
            return false
        }
        context.interpolationQuality = .high
        context.draw(image, in: rect)
        return true
    }
    guard madeContext else {
        throw NSError(domain: "LocalPhotoStudio.Haar", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "grayscale context failed"])
    }
    return bytes.map(Double.init)
}

private func fingerprint(_ path: String) throws -> Result {
    var coefficients = try decodePixels(path)
    var active = side
    while active >= 2 {
        for row in 0..<active {
            haar(&coefficients, offset: row * side, stride: 1, length: active)
        }
        for column in 0..<active {
            haar(&coefficients, offset: column, stride: side, length: active)
        }
        active /= 2
    }

    // Ignore the global brightness/DC term. Keep coefficient positions and
    // signs for comparison; magnitudes determine which coefficients survive.
    let ranked = coefficients.enumerated().dropFirst().sorted {
        let left = abs($0.element)
        let right = abs($1.element)
        return left == right ? $0.offset < $1.offset : left > right
    }.prefix(featureCount)
    let features = ranked.map { Feature(index: $0.offset, sign: $0.element >= 0 ? 1 : -1) }
    return Result(path: path, fingerprint: features)
}

do {
    let paths = Array(CommandLine.arguments.dropFirst())
    guard !paths.isEmpty else {
        fputs("usage: haar_fingerprint <image> [<image> ...]\n", stderr)
        exit(64)
    }
    let rows = try paths.map(fingerprint)
    let output = try JSONEncoder().encode(rows)
    FileHandle.standardOutput.write(output)
    FileHandle.standardOutput.write(Data([0x0a]))
} catch {
    fputs("haar_fingerprint: \(error.localizedDescription)\n", stderr)
    exit(1)
}
