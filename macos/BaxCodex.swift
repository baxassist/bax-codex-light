import AppKit
import SwiftUI

@main
struct BaxCodexApp: App {
    var body: some Scene {
        WindowGroup("Бакс для Codex") { InstallerView() }
            .windowResizability(.contentSize)
    }
}

struct InstallerView: View {
    @State private var working = false
    @State private var installed = false
    @State private var message = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("Бакс для Codex", systemImage: "bubble.left.and.bubble.right.fill")
                .font(.largeTitle.bold())
            Text("Подключите Бакс на телефоне к разговору Codex на этом Mac.")
                .font(.title3)
            Text(installed
                 ? "Перезапустите Codex и откройте чат в своём проекте. В Баксе получите код подключения и вставьте просьбу подключить этот чат. После этого можно ставить задачи с телефона."
                 : "Сначала установите и откройте Codex. Бакс добавит свой плагин; папку проекта выбирать здесь не нужно.")
                .fixedSize(horizontal: false, vertical: true)
            if working { ProgressView("Устанавливаю плагин…") }
            if !message.isEmpty {
                Text(message).foregroundStyle(installed ? Color.secondary : Color.red)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button(installed ? "Готово" : "Установить в Codex") {
                    if installed { NSApplication.shared.terminate(nil) } else { install() }
                }
                .buttonStyle(.borderedProminent).disabled(working)
                if !installed {
                    Link("Скачать Codex", destination: URL(string: "https://chatgpt.com/codex")!)
                }
            }
        }
        .padding(32).frame(width: 530)
    }

    private func install() {
        working = true
        message = ""
        Task {
            let result = await Task.detached { () -> String? in
                do {
                    let manager = FileManager.default
                    let folder = manager.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
                    try manager.createDirectory(at: folder, withIntermediateDirectories: true)
                    let destination = folder.appendingPathComponent("Bax Codex.app")
                    let source = Bundle.main.bundleURL.resolvingSymlinksInPath()
                    if source != destination.resolvingSymlinksInPath() {
                        // Сначала полная копия. Ошибка копирования сохраняет прежнее приложение.
                        let staged = folder.appendingPathComponent(".Bax Codex-\(UUID().uuidString).app")
                        try manager.copyItem(at: source, to: staged)
                        defer { try? manager.removeItem(at: staged) }
                        if manager.fileExists(atPath: destination.path) {
                            _ = try manager.replaceItemAt(destination, withItemAt: staged)
                        } else { try manager.moveItem(at: staged, to: destination) }
                    }
                    let process = Process()
                    process.executableURL = destination.appendingPathComponent("Contents/Resources/agent/bax-codex-light")
                    process.arguments = ["install-macos", "--app-path", destination.path]
                    let output = Pipe()
                    process.standardOutput = output
                    process.standardError = output
                    try process.run()
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if process.terminationStatus != 0 {
                        return String(data: data, encoding: .utf8) ?? "Не удалось установить плагин."
                    }
                    return nil
                } catch { return error.localizedDescription }
            }.value
            working = false
            installed = result == nil
            message = result ?? "Плагин установлен. Перезапустите Codex, чтобы он стал доступен."
        }
    }
}
