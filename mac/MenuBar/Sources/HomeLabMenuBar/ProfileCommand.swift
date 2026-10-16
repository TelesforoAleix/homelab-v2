import Foundation

/// One run of `homelab-profile`. The app holds no profile rules: every state, plan, guard and
/// switch comes from the command, which the installer records in the bundle's Info.plist.
struct CommandResult: Sendable {
    let status: Int32
    let output: Data
    let errors: String

    var succeeded: Bool { status == 0 }

    /// The command's own one-line reason, shown as-is (it never contains content).
    var message: String {
        errors.isEmpty ? "The profile command failed (exit \(status))." : errors
    }

    func decode<T: Decodable>(_ type: T.Type) -> T? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(type, from: output)
    }
}

enum ProfileCommand {
    static func run(_ arguments: [String], input: Data? = nil) async -> CommandResult {
        await Task.detached(priority: .userInitiated) { runBlocking(arguments, input: input) }.value
    }

    private static func runBlocking(_ arguments: [String], input: Data?) -> CommandResult {
        let info = Bundle.main.infoDictionary ?? [:]
        guard let python = info["HomeLabPython"] as? String,
              let script = info["HomeLabProfileCommand"] as? String
        else {
            return CommandResult(
                status: -1, output: Data(),
                errors: "HomeLab is not installed; run mac/install-menu-bar.sh from the repository.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [script] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let output = Pipe()
        let errors = Pipe()
        let stdin = Pipe()
        if input == nil { process.standardInput = FileHandle.nullDevice }
        else { process.standardInput = stdin }
        process.standardOutput = output
        process.standardError = errors
        do {
            try process.run()
        } catch {
            return CommandResult(status: -1, output: Data(), errors: "The profile command could not start.")
        }
        if let input {
            try? stdin.fileHandleForWriting.write(contentsOf: input)
            try? stdin.fileHandleForWriting.close()
        }
        // The command writes little to stderr, so reading stdout first cannot fill that pipe.
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let errorData = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(decoding: errorData, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return CommandResult(status: process.terminationStatus, output: data, errors: text)
    }
}
