import Foundation
import ImageIO
import Vision

// Read-only sample probe: detect faces, then rank cropped-face image similarity.
// Image feature prints are not identity embeddings; every match needs review.
struct Face {
    let file: String
    let index: Int
    let confidence: Float
    let box: CGRect
    let feature: VNFeaturePrintObservation
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: face_candidates <image-folder>\n", stderr)
    exit(2)
}

let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let imageExtensions: Set<String> = ["jpg", "jpeg", "png", "heic"]
let urls = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
    .filter { !$0.lastPathComponent.hasPrefix("._") && imageExtensions.contains($0.pathExtension.lowercased()) }
    .sorted { $0.lastPathComponent < $1.lastPathComponent }

var faces: [Face] = []
var noFace: [String] = []
var errors: [[String: String]] = []

for url in urls {
    let name = url.lastPathComponent
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        errors.append(["file": name, "error": "cannot open image"])
        continue
    }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 1600,
    ]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
        errors.append(["file": name, "error": "cannot decode image"])
        continue
    }

    do {
        let request = VNDetectFaceRectanglesRequest()
        try VNImageRequestHandler(cgImage: image).perform([request])
        let observations = request.results ?? []
        if observations.isEmpty { noFace.append(name) }

        for (index, observation) in observations.enumerated() {
            let box = observation.boundingBox
            let padded = box.insetBy(dx: -box.width * 0.12, dy: -box.height * 0.12)
            let crop = CGRect(
                x: padded.minX * CGFloat(image.width),
                y: (1 - padded.maxY) * CGFloat(image.height),
                width: padded.width * CGFloat(image.width),
                height: padded.height * CGFloat(image.height)
            ).integral.intersection(CGRect(x: 0, y: 0, width: image.width, height: image.height))
            guard !crop.isEmpty, let faceImage = image.cropping(to: crop) else { continue }

            let featureRequest = VNGenerateImageFeaturePrintRequest()
            try VNImageRequestHandler(cgImage: faceImage).perform([featureRequest])
            guard let feature = featureRequest.results?.first else { continue }
            faces.append(Face(file: name, index: index + 1, confidence: observation.confidence,
                              box: box, feature: feature))
        }
    } catch {
        errors.append(["file": name, "error": String(describing: error)])
    }
}

let faceRows: [[String: Any]] = faces.map { face in
    ["file": face.file, "face": face.index, "confidence": face.confidence,
     "box": [face.box.minX, face.box.minY, face.box.width, face.box.height]]
}
var pairs: [[String: Any]] = []
for i in faces.indices {
    for j in faces.indices where j > i && faces[i].file != faces[j].file {
        var distance: Float = 0
        do {
            try faces[i].feature.computeDistance(&distance, to: faces[j].feature)
            pairs.append(["a": faces[i].file, "aFace": faces[i].index,
                          "b": faces[j].file, "bFace": faces[j].index,
                          "distance": distance])
        } catch {
            continue
        }
    }
}
pairs.sort { ($0["distance"] as! Float) < ($1["distance"] as! Float) }

let report: [String: Any] = [
    "images": urls.count,
    "faceCount": faces.count,
    "faces": faceRows,
    "noFace": noFace,
    "errors": errors,
    "closestPairs": Array(pairs.prefix(80)),
]
let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
