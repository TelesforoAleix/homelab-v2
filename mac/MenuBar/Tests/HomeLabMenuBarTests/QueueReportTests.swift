import Foundation
import Testing
@testable import HomeLabMenuBar

private func decode(_ queue: String?) throws -> ProfileStatus {
    let base = #"""
    {"profile":"working","services":{},"paused":false,"return_pending":false,"running_jobs":[]
    """#
    let json = base + (queue.map { ",\"queue\":" + $0 } ?? "") + "}"
    let result = CommandResult(status: 0, output: Data(json.utf8), errors: "")
    return try #require(result.decode(ProfileStatus.self))
}

@Test func runningQueueAndOrderedRecords() throws {
    let status = try decode(#"""
    {"play":true,"paused_on_battery":false,"free_memory_mb":17694.72,
     "running":{"name":"active","state":"running","attempt":2,"elapsed_seconds":65,
                "progress":"1240/5000","physical_footprint_mb":21504},
     "waiting":[{"name":"second","state":"waiting","attempt":0},
                {"name":"third","state":"waiting","attempt":0}],
     "failed":[{"name":"failed","state":"failed","attempt":2,"exit_code":7}],
     "finished":[{"name":"done","state":"finished","attempt":1,"exit_code":0}]}
    """#)
    let queue = try #require(status.queue)
    #expect(queue.play)
    #expect(queue.running?.physicalFootprintMb == 21504)
    #expect(queue.freeMemoryMb == 17694.72)
    #expect(queue.running?.elapsedSeconds == 65)
    #expect(queue.running?.attempt == 2)
    #expect(queue.running?.progress == "1240/5000")
    #expect(queue.waiting.map(\.name) == ["second", "third"])
    #expect(queue.failed.first?.exitCode == 7)
    #expect(queue.finished.first?.name == "done")
}

@Test func pausedQueueWithUnavailableMeasurements() throws {
    let status = try decode(#"""
    {"play":true,"paused_on_battery":true,"free_memory_mb":null,
     "running":null,"waiting":[],"failed":[],"finished":[]}
    """#)
    #expect(status.queue?.pausedOnBattery == true)
    #expect(status.queue?.freeMemoryMb == nil)
    #expect(status.queue?.running == nil)
    #expect(status.queue?.waiting.isEmpty == true)
}

@Test func olderStatusWithoutQueueStillDecodes() throws {
    #expect(try decode(nil).queue == nil)
}
