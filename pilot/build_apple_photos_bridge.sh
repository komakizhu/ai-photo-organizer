#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
build_root=${APPLE_PHOTOS_BRIDGE_BUILD_ROOT:-$HOME/Library/Caches/LocalPhotoStudio/ApplePhotosBridge}
app="$build_root/ApplePhotosBridge.app"
mkdir -p "$app/Contents/MacOS"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.photostudio.apple-photos-bridge</string>
  <key>CFBundleName</key><string>Apple Photos Bridge</string>
  <key>CFBundleExecutable</key><string>ApplePhotosBridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSPhotoLibraryUsageDescription</key><string>照片整理工具仅在你点击连接 Apple 图库后读取照片缩略图、拍摄日期和截图标记。</string>
</dict></plist>
PLIST
xcrun swiftc -O -sdk "$(xcrun --sdk macosx --show-sdk-path)" \
  -framework AppKit -framework Photos -framework Foundation \
  "$script_dir/apple_photos_bridge.swift" -o "$app/Contents/MacOS/ApplePhotosBridge"
codesign --force --deep --sign - --identifier local.photostudio.apple-photos-bridge "$app"
printf 'Built and ad-hoc signed: %s\n' "$app"
