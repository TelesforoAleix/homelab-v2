import SwiftUI

struct ServicePolicy: Codable, Equatable {
    var state: String
    var allowed: [String]?
    var `default`: String?
}

struct BudgetReport: Decodable {
    let requiredMb: Int?
    let knownMb: Int
    let unknownModels: [String]
    let warning: String?
}

struct AvailableModel: Decodable {
    let memoryMb: Int?
}

struct ProfilesPreview: Decodable {
    let memoryBudgets: [String: BudgetReport]
    let modelRegistry: [String: [String: AvailableModel]]
}

@MainActor
final class ProfilesEditor: ObservableObject {
    @Published var policies: [String: [String: ServicePolicy]] = [:]
    @Published var budgets: [String: String] = [:]
    @Published var preview: ProfilesPreview?
    @Published var error: String?
    @Published var message: String?
    @Published var busy = false
    private var document: [String: Any] = [:]

    var payload: Data? {
        var result = document
        guard var profiles = result["profiles"] as? [String: [String: Any]], !policies.isEmpty else { return nil }
        let encoder = JSONEncoder()
        for name in policies.keys {
            guard let data = try? encoder.encode(policies[name]),
                  let services = try? JSONSerialization.jsonObject(with: data) else { return nil }
            profiles[name]?["services"] = services
            // Keep invalid input intact so command validation supplies the error.
            if let limit = Int(budgets[name] ?? "") {
                profiles[name]?["memory_budget_mb"] = limit
            } else {
                profiles[name]?["memory_budget_mb"] = budgets[name] ?? ""
            }
        }
        result["profiles"] = profiles
        return try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    }

    func load() async {
        busy = true
        defer { busy = false }
        let result = await ProfileCommand.run(["profiles-show"])
        guard result.succeeded,
              let raw = try? JSONSerialization.jsonObject(with: result.output) as? [String: Any],
              let profiles = raw["profiles"] as? [String: [String: Any]],
              raw["version"] as? Int == 2 else {
            error = result.succeeded ? "Run homelab-profile migrate before editing profiles." : result.message
            return
        }
        document = raw
        var decoded: [String: [String: ServicePolicy]] = [:]
        for (name, profile) in profiles {
            guard let services = profile["services"],
                  let data = try? JSONSerialization.data(withJSONObject: services),
                  let policy = try? JSONDecoder().decode([String: ServicePolicy].self, from: data) else {
                error = "Could not read profile services."
                return
            }
            decoded[name] = policy
            budgets[name] = String(profile["memory_budget_mb"] as? Int ?? 0)
        }
        policies = decoded
        message = nil
        await refreshPreview()
    }

    func refreshPreview() async {
        guard let data = payload else { return }
        let result = await ProfileCommand.run(["profiles-preview"], input: data)
        guard !Task.isCancelled, payload == data else { return }
        if result.succeeded, let report = result.decode(ProfilesPreview.self) {
            preview = report
            error = nil
        } else {
            error = result.message
        }
    }

    func save() async -> Bool {
        guard let data = payload else { return false }
        busy = true
        defer { busy = false }
        let result = await ProfileCommand.run(["profiles-set"], input: data)
        if result.succeeded {
            await load()
            message = "Profiles saved. Services apply now when the current state matches a profile."
            return true
        }
        error = result.message
        message = nil
        return false
    }
}

struct ProfilesView: View {
    @EnvironmentObject private var store: ProfileStore
    @StateObject private var editor = ProfilesEditor()
    @State private var selected = "working"
    private let services = ["vision", "embed-large-bulk", "cleanup", "voiceink"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Profile", selection: $selected) {
                Text("Working").tag("working")
                Text("Inference").tag("inference")
            }.pickerStyle(.segmented)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    budget
                    ForEach(services, id: \.self) { service in
                        if editor.policies[selected]?[service] != nil {
                            serviceEditor(service)
                        }
                    }
                }.padding(2)
            }
            if let error = editor.error {
                Text(error).foregroundStyle(.red).textSelection(.enabled)
            }
            if let message = editor.message { Text(message).foregroundStyle(.secondary) }
            HStack {
                Text("Current profile applies now; the other applies at its next switch.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if editor.busy { ProgressView().controlSize(.small) }
                Button("Reload") { Task { await editor.load() } }
                Button("Save profiles") {
                    Task {
                        if await editor.save() { await store.reloadStatus() }
                    }
                }.keyboardShortcut("s").disabled(editor.payload == nil || store.busy != nil)
            }
        }
        .padding(20).frame(minWidth: 620, minHeight: 500)
        .disabled(editor.busy)
        .task { await editor.load() }
        .task(id: editor.payload) {
            do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            await editor.refreshPreview()
        }
    }

    private var budget: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Memory budget (MiB)").font(.headline)
                TextField("MiB", text: Binding(
                    get: { editor.budgets[selected] ?? "" },
                    set: { editor.budgets[selected] = $0; editor.message = nil }
                )).frame(width: 100)
                if let report = editor.preview?.memoryBudgets[selected] {
                    Text(report.requiredMb.map { "Required: \($0) MiB" }
                         ?? "Required: unknown (known: \(report.knownMb) MiB)")
                        .foregroundStyle(.secondary)
                }
            }
            if let report = editor.preview?.memoryBudgets[selected] {
                if let warning = report.warning {
                    Text(warning + " — saving is allowed.").foregroundStyle(.orange)
                }
                if !report.unknownModels.isEmpty {
                    Text("Memory unknown: " + report.unknownModels.joined(separator: ", "))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func policy(_ service: String) -> Binding<ServicePolicy> {
        Binding(get: { editor.policies[selected]![service]! }, set: {
            editor.policies[selected]?[service] = $0
            editor.message = nil
        })
    }

    private func serviceEditor(_ service: String) -> some View {
        let binding = policy(service)
        let routed = binding.wrappedValue.allowed != nil
        return GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Picker("State", selection: binding.state) {
                    Text("off").tag("off")
                    if routed { Text("on request").tag("on request") }
                    Text("on").tag("on")
                }.pickerStyle(.segmented)
                if routed {
                    HStack(alignment: .top, spacing: 24) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Available · memory").font(.subheadline.weight(.semibold))
                            ForEach((editor.preview?.modelRegistry[service] ?? [:]).keys.sorted(), id: \.self) { model in
                                let memory = editor.preview?.modelRegistry[service]?[model]?.memoryMb
                                Text(model + " · " + (memory.map { "\($0) MiB" } ?? "unknown"))
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Allowed").font(.subheadline.weight(.semibold))
                            ForEach((editor.preview?.modelRegistry[service] ?? [:]).keys.sorted(), id: \.self) { model in
                                Toggle(model, isOn: Binding(
                                    get: { binding.wrappedValue.allowed?.contains(model) == true },
                                    set: { on in
                                        var value = binding.wrappedValue
                                        var allowed = value.allowed ?? []
                                        if on { allowed.append(model) } else { allowed.removeAll { $0 == model } }
                                        value.allowed = allowed
                                        if !allowed.contains(value.default ?? "") { value.default = allowed.first }
                                        binding.wrappedValue = value
                                    }
                                ))
                            }
                            Picker("Default", selection: Binding(
                                get: { binding.wrappedValue.default ?? "" },
                                set: { binding.wrappedValue.default = $0 }
                            )) {
                                if binding.wrappedValue.allowed?.isEmpty == true { Text("Choose an allowed model").tag("") }
                                ForEach(binding.wrappedValue.allowed ?? [], id: \.self) { Text($0).tag($0) }
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.font(.callout)
                }
            }.padding(8)
        } label: { Text(Names.service(service)).font(.headline) }
    }
}
