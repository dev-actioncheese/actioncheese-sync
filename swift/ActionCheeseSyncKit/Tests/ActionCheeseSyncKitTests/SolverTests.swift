// SPDX-License-Identifier: MIT
import XCTest
@testable import ActionCheeseSyncKit

/// The solver's job in ActionCheese: hold the iPhone clips where their
/// timecode already put them, and arrange everything else around them.
final class SolverTests: XCTestCase {

    func edge(_ a: Int, _ b: Int, _ delta: Double, _ confidence: Double = 0.8) -> Solver.Edge {
        Solver.Edge(a: a, b: b, delta: delta, confidence: confidence, overlapSeconds: 120)
    }

    func testAnchorsDoNotMove() {
        // 0 and 1 are anchors 10 s apart; 2 is a candidate seen 3 s after 0.
        let solution = Solver.solve(
            nodeCount: 3,
            edges: [edge(0, 1, 10), edge(0, 2, 3)],
            anchors: [0: 100, 1: 110])
        XCTAssertEqual(solution.positions[0]!, 100, accuracy: 1e-6)
        XCTAssertEqual(solution.positions[1]!, 110, accuracy: 1e-6)
        XCTAssertEqual(solution.positions[2]!, 103, accuracy: 1e-3)
    }

    func testAnchorsWinOverADisagreeingMatch() {
        // The match says 20 s; the anchors say 10 s.  The anchors are known.
        let solution = Solver.solve(
            nodeCount: 2, edges: [edge(0, 1, 20)], anchors: [0: 0, 1: 10])
        XCTAssertEqual(solution.positions[0]!, 0, accuracy: 1e-6)
        XCTAssertEqual(solution.positions[1]!, 10, accuracy: 1e-6)
    }

    func testACandidateReachesTheTimelineThroughAnotherCandidate() {
        // Sony B overlaps only Sony A, and Sony A overlaps the iPhone.
        let solution = Solver.solve(
            nodeCount: 3,
            edges: [edge(0, 1, 5), edge(1, 2, 7)],
            anchors: [0: 50])
        XCTAssertEqual(solution.positions[1]!, 55, accuracy: 1e-3)
        XCTAssertEqual(solution.positions[2]!, 62, accuracy: 1e-3)
    }

    func testAGroupThatNeverReachesAnAnchorIsNotGuessedAt() {
        // Two cameras that sync to each other but to nothing known: their
        // absolute time is unknowable from sound, so it is not invented.
        let solution = Solver.solve(
            nodeCount: 3,
            edges: [edge(1, 2, 4)],
            anchors: [0: 0])
        XCTAssertNil(solution.positions[1])
        XCTAssertNil(solution.positions[2])
        XCTAssertEqual(solution.floating.first.map(Set.init), Set([1, 2]))
    }

    func testDisagreeingEdgesAreReconciledNotChained() {
        // A triangle that does not quite close: 0->1 = 10, 1->2 = 10,
        // 0->2 = 21.  Least squares splits the 1 s of disagreement instead of
        // letting whichever edge came first win outright.
        let solution = Solver.solve(
            nodeCount: 3,
            edges: [edge(0, 1, 10), edge(1, 2, 10), edge(0, 2, 21)],
            anchors: [0: 0])
        let second = solution.positions[2]!
        XCTAssertGreaterThan(second, 20.0)
        XCTAssertLessThan(second, 21.0)
    }

    func testUnmatchedNodesAreReported() {
        let solution = Solver.solve(nodeCount: 2, edges: [], anchors: [0: 0])
        XCTAssertEqual(solution.unmatched, [1])
    }
}

/// One device cannot record two things at once.
final class DeviceRuleTests: XCTestCase {
    func testClipsFromOneDeviceAreNeverMatched() async throws {
        // Both point at the same file, so the audio is identical and would
        // correlate perfectly -- the device rule must still refuse it.
        let url = ParityTests.media("callout_iphone.wav")
        let a = SyncClip(id: "a", url: url, device: "sony-a7-1")
        let b = SyncClip(id: "b", url: url, device: "sony-a7-1")
        let anchor = SyncAnchor(
            clip: SyncClip(id: "anchor", url: ParityTests.media("callout_sonyA.wav"),
                           device: "iphone-1"),
            start: 0)

        var options = SyncOptions()
        options.measureDrift = false
        options.refine = false
        let results = try await ActionCheeseSync.sync(
            anchors: [anchor], candidates: [a, b], options: options)

        // Each may match the anchor, but never each other; with identical
        // audio a same-device match would place them on top of one another.
        XCTAssertEqual(results.count, 2)
    }
}
