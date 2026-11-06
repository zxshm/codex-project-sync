// 为独立项目同步程序提供自己的 macOS 应用身份。
import AppKit
import Foundation

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
class LauncherDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex-project-sync/ui-session.json")
        if let data = try? Data(contentsOf: path),
           let session = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let address = session["url"] as? String, let token = session["token"] as? String,
           let url = URL(string: address + "#" + token) {
            NSWorkspace.shared.open(url)
        }
        return true
    }
}
let delegate = LauncherDelegate()
app.delegate = delegate
let child = Process()
let resources = Bundle.main.resourceURL!
child.executableURL = resources.appendingPathComponent("backend/CodexProjectSync")
child.arguments = []
child.standardOutput = FileHandle.nullDevice
child.standardError = FileHandle.nullDevice
child.terminationHandler = { _ in
    DispatchQueue.main.async { app.terminate(nil) }
}
do {
    try child.run()
} catch {
    let alert = NSAlert()
    alert.messageText = "项目同步启动失败"
    alert.informativeText = error.localizedDescription
    alert.runModal()
    exit(1)
}
app.run()
