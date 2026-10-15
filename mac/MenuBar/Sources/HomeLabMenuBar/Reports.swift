import Foundation

// The command's JSON, decoded with snake_case keys converted. Only the fields shown are read.

struct ProfileStatus: Decodable, Sendable {
    let profile: String
    let services: [String: ServiceState]
    let paused: Bool
    let pauseReason: String?
    let returnPending: Bool
    let runningJobs: [String]
}

struct ServiceState: Decodable, Sendable {
    /// Router services report `allowed` and a native `state`; dictation reports `running`.
    let allowed: Bool?
    let state: String?
    let running: Bool?

    var on: Bool { allowed ?? running ?? false }
    var label: String { state ?? (running == true ? "running" : "stopped") }
}

struct WaitingReport: Decodable, Sendable {
    let sinceHours: Int
    let purposes: [WaitingPurpose]
}

struct WaitingPurpose: Decodable, Sendable, Identifiable {
    let purpose: String
    let count: Int
    let estimatedSeconds: Double?

    var id: String { purpose }
}

struct NightWindow: Decodable, Sendable {
    let enabled: Bool
    let start: String
    let end: String
    let days: [Int]
    let last: LastNight?
}

struct LastNight: Decodable, Sendable {
    let night: String?
    let result: String?
    let owned: Bool?
}

struct InferencePlan: Decodable, Sendable {
    let appsToQuit: [String]
    let servicesToStop: [String]
    let work: [GuardedWork]
    let onAcPower: Bool
    let refusal: String?
}

struct GuardedWork: Decodable, Sendable {
    let pid: Int
    let command: String
}

/// What the VS Code guard prints when it refuses an unconfirmed switch.
struct GuardRefusal: Decodable, Sendable {
    let refused: String
    let work: [GuardedWork]
}

struct UsageReport: Decodable, Sendable {
    let readAt: String
    let codex: UsageSource?
    let claude: UsageSource?
}

struct UsageSource: Decodable, Sendable {
    let asOf: String
    let ageSeconds: Int
    let fiveHour: UsageWindow
    let sevenDay: UsageWindow
}

struct UsageWindow: Decodable, Sendable {
    let usedPercentage: Double
    let resetsAt: String?
}
