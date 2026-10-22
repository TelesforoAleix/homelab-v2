import AppKit
import Foundation

@MainActor
final class ProfileStore: ObservableObject {
    @Published var status: ProfileStatus?
    @Published var statusError: String?
    @Published var waiting: WaitingReport?
    @Published var waitingError: String?
    @Published var nightWindow: NightWindow?
    @Published var usage: UsageReport?
    @Published var usageError: String?
    @Published var busy: String?
    @Published var refreshingUsage = false
    @Published var loadingWaiting = false

    // Editable night-window fields; the command validates and stores them.
    @Published var draftStart = "22:00"
    @Published var draftEnd = "07:00"
    @Published var draftDays: Set<Int> = [0, 1, 2, 3, 4]

    init() {
        Task { await reload() }
    }

    func reload() async {
        async let state: Void = reloadStatus()
        async let night: Void = reloadNightWindow()
        async let blocked: Void = reloadWaiting()
        _ = await (state, night, blocked)
    }

    func reloadStatus() async {
        let result = await ProfileCommand.run(["status"])
        if result.succeeded, let decoded = result.decode(ProfileStatus.self) {
            status = decoded
            statusError = nil
        } else {
            status = nil
            statusError = result.message
        }
    }

    func reloadWaiting() async {
        loadingWaiting = true
        defer { loadingWaiting = false }
        let result = await ProfileCommand.run(["waiting"])
        if result.succeeded, let decoded = result.decode(WaitingReport.self) {
            waiting = decoded
            waitingError = nil
        } else {
            waiting = nil
            // The command names which node read failed.
            waitingError = result.message
        }
    }

    func reloadNightWindow() async {
        let result = await ProfileCommand.run(["schedule", "show"])
        guard let decoded = result.decode(NightWindow.self) else { return }
        nightWindow = decoded
        draftStart = decoded.start
        draftEnd = decoded.end
        draftDays = Set(decoded.days)
    }

    func refreshUsage() async {
        refreshingUsage = true
        defer { refreshingUsage = false }
        let result = await ProfileCommand.run(["usage"])
        if result.succeeded, let decoded = result.decode(UsageReport.self) {
            usage = decoded
            usageError = nil
        } else {
            usageError = result.message
        }
    }

    // MARK: Actions. Each runs the command once and shows its answer.

    func queueAction(_ action: String, name: String? = nil) async {
        guard busy == nil else { return }
        busy = "Updating queue…"
        defer { busy = nil }
        let arguments = ["queue", action] + (name.map { [$0] } ?? [])
        let result = await ProfileCommand.run(arguments)
        if !result.succeeded { Dialogs.tell("Queue command refused", result.message) }
        await reloadStatus()
    }

    func switchToWorking() async {
        await perform("Switching to Working…", ["switch", "working"])
    }

    func setService(_ name: String, start: Bool) async {
        await perform(start ? "Starting \(name)…" : "Stopping \(name)…", [start ? "start" : "stop", name])
    }

    func switchToInference() async {
        busy = "Checking what Inference would stop…"
        let planned = await ProfileCommand.run(["plan-inference"])
        busy = nil
        guard planned.succeeded, let plan = planned.decode(InferencePlan.self) else {
            Dialogs.tell("Could not prepare the switch", planned.message)
            return
        }
        if let refusal = plan.refusal {
            Dialogs.tell("Inference refused", refusal)
            return
        }
        guard Dialogs.ask("Switch to Inference?", Self.describe(plan), confirm: "Switch to Inference") else {
            return
        }
        busy = "Switching to Inference…"
        var result = await ProfileCommand.run(["switch", "inference"])
        if !result.succeeded, let refusal = result.decode(GuardRefusal.self) {
            // The guard refused: repeat its warning and let the owner decide.
            busy = nil
            let warning = ([refusal.refused + ":"] + refusal.work.map(Self.describe)
                + ["", "Switch anyway only if you accept stopping that work."]).joined(separator: "\n")
            guard Dialogs.ask("Running work would be killed", warning, confirm: "Switch anyway") else {
                await reloadStatus()
                return
            }
            busy = "Switching to Inference…"
            result = await ProfileCommand.run(["switch", "inference", "--confirm-kill"])
        }
        await finish(result, failure: "Inference refused")
    }

    func saveNightWindow(enabled: Bool) async {
        let week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        let arguments: [String]
        if enabled {
            let days = draftDays.sorted().map { week[$0] }.joined(separator: ",")
            arguments = ["schedule", "set", "--start", draftStart, "--end", draftEnd, "--days", days]
        } else {
            arguments = ["schedule", "off"]
        }
        let result = await ProfileCommand.run(arguments)
        if !result.succeeded {
            Dialogs.tell("Night window not saved", "Use HH:MM times that differ, and choose at least one day.")
        }
        await reloadNightWindow()
    }

    private func perform(_ label: String, _ arguments: [String]) async {
        busy = label
        await finish(await ProfileCommand.run(arguments), failure: "The command refused")
    }

    private func finish(_ result: CommandResult, failure: String) async {
        busy = nil
        if let decoded = result.decode(ProfileStatus.self), result.succeeded {
            status = decoded
        } else {
            Dialogs.tell(failure, result.message)
            await reloadStatus()
        }
        await reloadNightWindow()
    }

    private static func describe(_ plan: InferencePlan) -> String {
        let apps = plan.appsToQuit.map(Names.app)
        let services = plan.servicesToStop.map(Names.service)
        var lines = [
            "Quits: " + (apps.isEmpty ? "nothing running" : apps.joined(separator: ", ")),
            "Stops: " + (services.isEmpty ? "nothing running" : services.joined(separator: ", ")),
            "",
        ]
        if plan.work.isEmpty {
            lines.append("The guard found no running work.")
        } else {
            lines.append("The guard found running work that the switch would kill:")
            lines += plan.work.map(describe)
        }
        return lines.joined(separator: "\n")
    }

    private static func describe(_ work: GuardedWork) -> String {
        let command = work.command.count > 120 ? String(work.command.prefix(120)) + "…" : work.command
        return "• \(work.pid): \(command)"
    }
}

enum Names {
    static func app(_ bundle: String) -> String {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle)
            .map { FileManager.default.displayName(atPath: $0.path) } ?? bundle
    }

    static func service(_ name: String) -> String {
        switch name {
        case "vision": "Vision"
        case "embed-large-bulk": "Bulk embeddings"
        case "cleanup": "Dictation cleanup"
        case "voiceink": "Dictation (VoiceInk)"
        default: name
        }
    }
}

@MainActor
enum Dialogs {
    static func ask(_ title: String, _ message: String, confirm: String) -> Bool {
        let alert = make(title, message)
        alert.addButton(withTitle: confirm)
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    static func tell(_ title: String, _ message: String) {
        let alert = make(title, message)
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private static func make(_ title: String, _ message: String) -> NSAlert {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        return alert
    }
}
