import AppKit
import Foundation
import Photos

// Type-check only. No entry point or top-level library access is defined.
enum PhotoKitCompileProbe {
    static func authorizationStatus() -> PHAuthorizationStatus {
        PHPhotoLibrary.authorizationStatus(for: .readWrite)
    }

    static func requestReadWriteAuthorization(
        completion: @escaping (PHAuthorizationStatus) -> Void
    ) {
        PHPhotoLibrary.requestAuthorization(for: .readWrite, handler: completion)
    }

    static func fetchImageAssets() -> PHFetchResult<PHAsset> {
        PHAsset.fetchAssets(with: .image, options: PHFetchOptions())
    }

    static func isScreenshot(_ asset: PHAsset) -> Bool {
        asset.mediaSubtypes.contains(.photoScreenshot)
    }

    static func originalFilename(for asset: PHAsset) -> String? {
        PHAssetResource.assetResources(for: asset).first?.originalFilename
    }

    static func requestLocalThumbnail(
        for asset: PHAsset,
        manager: PHImageManager,
        completion: @escaping (NSImage?) -> Void
    ) {
        let options = PHImageRequestOptions()
        options.isNetworkAccessAllowed = false
        options.deliveryMode = .opportunistic
        manager.requestImage(
            for: asset,
            targetSize: CGSize(width: 320, height: 320),
            contentMode: .aspectFill,
            options: options
        ) { image, _ in
            completion(image)
        }
    }

    static func deletionRequestSignature(_ assets: [PHAsset]) {
        PHAssetChangeRequest.deleteAssets(assets as NSArray)
    }
}
