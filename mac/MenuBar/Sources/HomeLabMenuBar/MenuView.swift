import SwiftUI

struct MenuView: View {
    @Environment(\.openWindow) private var openWindow
    @EnvironmentObject private var store: ProfileStore
    private let services = ["vision", "embed-large-bulk", "cleanup", "voiceink"]
    private let weekdays = ["M", "T", "W", "T", "F", "S", "S"]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            switching
            serviceList
            Divider()
            queueSection
            Divider()
            waitingList
            Divider()
            usageList
            Divider()
            nightWindow
            Divider()
            HStack {
                Button("Profiles…") {
                    openWindow(id: "profiles")
                    NSApp.activate()
                }
                Button("Reload") { Task { await store.reload() } }
                Spacer()
                Button("Quit HomeLab") { NSApplication.shared.terminate(nil) }
            }
            .buttonStyle(.borderless)
        }
        .font(.callout)
        .padding(14)
        .frame(width: 340)
        .disabled(store.busy != nil)
        .task {
            await store.reload()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                if store.busy == nil { await store.reloadStatus() }
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.title(store.status)).font(.headline)
                if let busy = store.busy {
                    Text(busy).foregroundStyle(.secondary)
                } else if let error = store.statusError {
                    Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                } else if let jobs = store.status?.runningJobs, !jobs.isEmpty {
                    Text("Jobs running: " + jobs.joined(separator: ", ")).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if store.busy != nil { ProgressView().controlSize(.small) }
        }
    }

    private var switching: some View {
        HStack {
            Button("Working") { Task { await store.switchToWorking() } }
                .disabled(store.status?.profile == "working")
            Button("Inference…") { Task { await store.switchToInference() } }
                .disabled(store.status?.profile == "inference" || store.status?.returnPending == true)
        }
    }

    private var serviceList: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(services, id: \.self) { name in
                let state = store.status?.services[name]
                HStack {
                    Image(systemName: state?.on == true ? "circle.fill" : "circle")
                        .foregroundStyle(state?.on == true ? Color.green : Color.secondary)
                        .imageScale(.small)
                    Text(Names.service(name))
                    Spacer()
                    Text(state?.label ?? "unknown").foregroundStyle(.secondary)
                    Button(state?.on == true ? "Stop" : "Start") {
                        Task { await store.setService(name, start: state?.on != true) }
                    }
                    .buttonStyle(.borderless)
                    .disabled(state == nil)
                }
            }
            Text("Starting or stopping one service makes the state custom.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var queueSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Job queue").font(.subheadline.weight(.semibold))
                Spacer()
                Button(store.status?.queue?.play == true ? "Stop" : "Play") {
                    Task { await store.queueAction(store.status?.queue?.play == true ? "stop" : "play") }
                }
                .buttonStyle(.borderless)
                .disabled(store.status?.queue == nil)
            }
            if let queue = store.status?.queue {
                if queue.pausedOnBattery {
                    Text("Paused on battery · waiting for sustained AC power")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let job = queue.running {
                    Text("Running: " + job.name).lineLimit(1).help(job.name)
                    Text("\(Self.elapsed(job.elapsedSeconds)) · attempt \(job.attempt)")
                        .font(.caption).foregroundStyle(.secondary)
                    if let progress = job.progress, !progress.isEmpty {
                        Text(progress).font(.caption).lineLimit(2).help(progress)
                    }
                    Text("Footprint \(Self.memory(job.physicalFootprintMb)) · available \(Self.memory(queue.freeMemoryMb))")
                        .font(.caption).foregroundStyle(.secondary)
                        .help("Process-group physical footprint includes charged Metal allocations; excludes clean mapped model files. Available memory includes immediately reclaimable memory (macOS memory_pressure).")
                } else {
                    Text("No job running · available \(Self.memory(queue.freeMemoryMb))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                let jobs = queue.waiting + queue.failed + queue.finished
                if jobs.isEmpty {
                    Text("No waiting, failed or finished jobs").font(.caption).foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(jobs) { job in queueRow(job) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: min(220, CGFloat(jobs.count) * 54))
                }
            } else {
                Text("Queue unavailable").foregroundStyle(.secondary)
            }
        }
    }

    private func queueRow(_ job: QueueJob) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(job.name).lineLimit(1).help(job.name)
                Spacer()
                Text(job.state == "failed" ? "failed (exit \(job.exitCode.map(String.init) ?? "unknown"))" : job.state)
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                if job.state == "waiting" {
                    Button("To front") { Task { await store.queueAction("top", name: job.name) } }
                }
                if job.state == "failed" {
                    Button("Requeue") { Task { await store.queueAction("requeue", name: job.name) } }
                }
                Button("Remove") { Task { await store.queueAction("remove", name: job.name) } }
            }.buttonStyle(.borderless)
        }
    }

    private var waitingList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Waiting (refused in the last 24 hours)").font(.subheadline.weight(.semibold))
            if let error = store.waitingError {
                Text(error).foregroundStyle(.secondary)
            } else if let waiting = store.waiting {
                if waiting.purposes.isEmpty {
                    Text("Nothing waiting").foregroundStyle(.secondary)
                }
                ForEach(waiting.purposes) { row in
                    HStack {
                        Text(row.purpose)
                        Spacer()
                        Text("\(row.count) · " + Self.estimate(row.estimatedSeconds))
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                Text(store.loadingWaiting ? "Reading the node…" : "Not read yet").foregroundStyle(.secondary)
            }
        }
    }

    private var usageList: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Usage").font(.subheadline.weight(.semibold))
                Spacer()
                Button(store.refreshingUsage ? "Reading…" : "Refresh") {
                    Task { await store.refreshUsage() }
                }
                .buttonStyle(.borderless)
                .disabled(store.refreshingUsage)
            }
            if let usage = store.usage {
                usageRows("Claude", usage.claude)
                usageRows("Codex", usage.codex)
            } else {
                Text(store.usageError ?? "Press Refresh to read local usage.").foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func usageRows(_ name: String, _ source: UsageSource?) -> some View {
        if let source {
            VStack(alignment: .leading, spacing: 1) {
                HStack {
                    Text(name)
                    Spacer()
                    Text("as of \(Self.time(source.asOf)) (\(Self.age(source.ageSeconds)))")
                        .foregroundStyle(.secondary)
                }
                usageLine("5 hours", source.fiveHour)
                usageLine("7 days", source.sevenDay)
            }
        } else {
            HStack {
                Text(name)
                Spacer()
                Text("unknown").foregroundStyle(.secondary)
            }
        }
    }

    private func usageLine(_ label: String, _ window: UsageWindow) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(String(format: "%.0f%% used", window.usedPercentage))
            Text("resets " + (window.resetsAt.map(Self.time) ?? "unknown")).foregroundStyle(.secondary)
        }
        .font(.caption)
    }

    private var nightWindow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: Binding(
                get: { store.nightWindow?.enabled ?? false },
                set: { enabled in Task { await store.saveNightWindow(enabled: enabled) } }
            )) {
                Text("Night window").font(.subheadline.weight(.semibold))
            }
            .toggleStyle(.switch)
            .disabled(store.nightWindow == nil)
            HStack {
                TextField("22:00", text: $store.draftStart).frame(width: 56)
                Text("to")
                TextField("07:00", text: $store.draftEnd).frame(width: 56)
                Spacer()
                ForEach(0..<7, id: \.self) { day in
                    Toggle(weekdays[day], isOn: Binding(
                        get: { store.draftDays.contains(day) },
                        set: { on in
                            if on { store.draftDays.insert(day) } else { store.draftDays.remove(day) }
                        }
                    ))
                    .toggleStyle(.button)
                    .controlSize(.mini)
                }
            }
            HStack {
                Text(nightSummary).font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                if store.nightWindow?.enabled == true {
                    Button("Save") { Task { await store.saveNightWindow(enabled: true) } }
                        .buttonStyle(.borderless)
                }
            }
        }
    }

    private var nightSummary: String {
        guard let window = store.nightWindow else { return "Night window unavailable." }
        var text = window.enabled
            ? "On: enters Inference at the start only on AC, from Working, with no running work; "
                + "returns to Working at the end unless you switched by hand."
            : "Off."
        if let last = window.last, let night = last.night, let result = last.result {
            text += "\nNight of \(night): \(result)."
        }
        return text
    }

    // MARK: Formatting

    static func title(_ status: ProfileStatus?) -> String {
        guard let status else { return "HomeLab: unknown" }
        switch status.profile {
        case "working": return "Working"
        case "inference" where status.paused:
            let reason = status.pauseReason == "on_battery" ? "on battery" : (status.pauseReason ?? "paused")
            return "Inference, paused (\(reason))"
        case "inference": return "Inference"
        default: return "Custom"
        }
    }

    static func memory(_ mib: Double?) -> String {
        guard let mib else { return "unknown" }
        return String(format: "%.1f GiB", mib / 1024)
    }

    static func elapsed(_ seconds: Double?) -> String {
        let total = Int(max(0, seconds ?? 0))
        return String(format: "%d:%02d:%02d", total / 3600, total / 60 % 60, total % 60)
    }

    static func estimate(_ seconds: Double?) -> String {
        guard let seconds else { return "estimate unavailable" }
        if seconds < 90 { return "about \(Int(seconds.rounded())) s" }
        if seconds < 5400 { return "about \(Int((seconds / 60).rounded())) min" }
        return String(format: "about %.1f h", seconds / 3600)
    }

    static func age(_ seconds: Int) -> String {
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(seconds / 60) min ago" }
        if seconds < 172_800 { return "\(seconds / 3600) h ago" }
        return "\(seconds / 86400) days ago"
    }

    static func time(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: iso) else { return iso }
        let style = Calendar.current.isDateInToday(date)
            ? Date.FormatStyle(date: .omitted, time: .shortened)
            : Date.FormatStyle(date: .abbreviated, time: .shortened)
        return date.formatted(style)
    }
}
