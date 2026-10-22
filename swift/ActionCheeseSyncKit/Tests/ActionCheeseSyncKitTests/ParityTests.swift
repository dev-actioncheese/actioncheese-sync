// SPDX-License-Identifier: MIT
import XCTest
import Foundation
@testable import ActionCheeseSyncKit

/// The Swift engine is a translation of the Python one, not a reimplementation,
/// so it has to agree with it numerically.  `tests/fixtures/make_golden.py`
/// records what the Python engine produces; these replay it.
final class ParityTests: XCTestCase {

    struct Golden: Decodable {
        struct DSPProbe: Decodable { let samples: [Float]; let envelope: [Float] }
        struct PhatProbe: Decodable {
            let samples_a: [Float]
            let samples_b: [Float]
            let expected_offset_seconds: Double
            let python_offset_seconds: Double
        }
        struct Pair: Decodable {
            let scene: String
            let a: String
            let b: String
            let offset_seconds: Double
            let confidence: Double
        }
        let sample_rate: Double
        let envelope_rate: Double
        let dsp_probe: DSPProbe
        let gcc_phat_probe: PhatProbe
        let pairs: [Pair]
        let callout_truth: [String: Double]
    }

    static var golden: Golden = {
        let url = Bundle.module.url(forResource: "golden", withExtension: "json",
                                    subdirectory: "Golden")
            ?? Bundle.module.url(forResource: "golden", withExtension: "json")!
        return try! JSONDecoder().decode(Golden.self, from: Data(contentsOf: url))
    }()

    /// The fixture media lives in the Python tree; parity is checked against
    /// the very same files both engines decode.
    static func media(_ name: String) -> URL {
        URL(fileURLWithPath: #filePath)                 // .../Tests/<target>/ParityTests.swift
            .deletingLastPathComponent()                // <target>
            .deletingLastPathComponent()                // Tests
            .deletingLastPathComponent()                // ActionCheeseSyncKit
            .deletingLastPathComponent()                // swift
            .deletingLastPathComponent()                // repo root
            .appendingPathComponent("tests/fixtures/media/\(name)")
    }

    // ------------------------------------------------------------------ DSP

    func testOnsetEnvelopeMatchesPython() throws {
        let probe = Self.golden.dsp_probe
        let mine = OnsetEnvelope.compute(samples: probe.samples,
                                         sampleRate: Self.golden.sample_rate)
        XCTAssertEqual(mine.count, probe.envelope.count, "frame count differs")

        var worst: Float = 0
        for (a, b) in zip(mine, probe.envelope) { worst = max(worst, abs(a - b)) }
        // Both are unit-norm vectors of ~400 values, so a per-sample difference
        // of 1e-3 is far below anything that could move a correlation peak.
        XCTAssertLessThan(worst, 1e-3, "envelope drifts from the Python engine")
    }

    func testGCCPhatMatchesPythonAndTheTruth() throws {
        let probe = Self.golden.gcc_phat_probe
        let (offset, peak) = Correlation.gccPhat(probe.samples_a, probe.samples_b,
                                                 sampleRate: Self.golden.sample_rate)
        XCTAssertGreaterThan(peak, 0)
        XCTAssertEqual(offset, probe.expected_offset_seconds, accuracy: 1e-4,
                       "GCC-PHAT missed a known integer-sample shift")
        XCTAssertEqual(offset, probe.python_offset_seconds, accuracy: 1e-4,
                       "GCC-PHAT disagrees with the Python engine")
    }

    // -------------------------------------------------------------- matching

    func testPairOffsetsMatchPythonWithinOneMillisecond() async throws {
        var envelopes: [String: [Float]] = [:]
        for name in Set(Self.golden.pairs.flatMap { [$0.a, $0.b] }) {
            let samples = try await AudioDecoder.decodeMono(url: Self.media(name))
            XCTAssertFalse(samples.isEmpty, "\(name) decoded to nothing")
            envelopes[name] = OnsetEnvelope.compute(samples: samples)
        }

        for pair in Self.golden.pairs {
            let match = Correlation.matchEnvelopes(
                envelopes[pair.a]!, envelopes[pair.b]!,
                envelopeRate: Self.golden.envelope_rate, minOverlapSeconds: 20)
            let found = try XCTUnwrap(match, "\(pair.a) vs \(pair.b) produced no match")
            XCTAssertEqual(found.offsetSeconds, pair.offset_seconds, accuracy: 0.001,
                           "\(pair.scene): \(pair.a) vs \(pair.b) offset differs from Python")
            XCTAssertEqual(found.confidence, pair.confidence, accuracy: 0.02,
                           "\(pair.scene): \(pair.a) vs \(pair.b) confidence differs")
        }
    }

    /// Somebody says "action": the same short word on every device, and nothing
    /// else in common.  This is how a crew without timecode actually syncs.
    func testActionCalloutAlignsEveryDevice() async throws {
        let truth = Self.golden.callout_truth
        var envelopes: [String: [Float]] = [:]
        for name in truth.keys {
            let samples = try await AudioDecoder.decodeMono(url: Self.media(name))
            envelopes[name] = OnsetEnvelope.compute(samples: samples)
        }

        let names = truth.keys.sorted()
        for (index, first) in names.enumerated() {
            for second in names[(index + 1)...] {
                let match = try XCTUnwrap(Correlation.matchEnvelopes(
                    envelopes[first]!, envelopes[second]!,
                    envelopeRate: Self.golden.envelope_rate, minOverlapSeconds: 20))
                // offset satisfies a[t + offset] == b[t].  A device that
                // started rolling earlier carries the word later in its own
                // file, so the offset is simply the difference in start times.
                let expected = truth[second]! - truth[first]!
                XCTAssertEqual(match.offsetSeconds, expected, accuracy: 0.001,
                               "callout \(first) vs \(second) misaligned")
            }
        }
    }

    /// One transient in a minute of unrelated noise correlates correctly but
    /// weakly, so a callout workflow needs a lower floor than a scene with
    /// continuous shared sound.  Recorded here so the threshold is a decision,
    /// not an accident.
    func testCalloutConfidenceSitsBelowTheDefaultFloor() throws {
        let callout = Self.golden.pairs.filter { $0.scene == "callout" }
        XCTAssertFalse(callout.isEmpty)
        for pair in callout {
            XCTAssertLessThan(pair.confidence, SyncOptions().minConfidence)
            XCTAssertGreaterThan(pair.confidence, 0.15)
        }
    }
}
