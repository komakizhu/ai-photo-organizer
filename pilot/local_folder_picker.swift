import AppKit
import Foundation

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let panel = NSOpenPanel()
panel.title = "选择照片图库"
panel.message = "选择要整理的普通照片文件夹；扫描范围仅包含该文件夹及其子文件夹。"
panel.prompt = "选择图库"
panel.canChooseFiles = false
panel.canChooseDirectories = true
panel.allowsMultipleSelection = false
panel.treatsFilePackagesAsDirectories = false

func writeJSON(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value),
          let output = String(data: data, encoding: .utf8) else {
        fputs("Could not encode folder picker result.\n", stderr)
        exit(2)
    }
    print(output)
}

// Invoked only by the user clicking “选择图库”.
app.activate(ignoringOtherApps: true)
panel.makeKeyAndOrderFront(nil)

if panel.runModal() == .OK, let url = panel.url {
    writeJSON(["path": url.path])
} else {
    writeJSON(["cancelled": true])
}
