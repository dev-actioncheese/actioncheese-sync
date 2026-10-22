// SPDX-License-Identifier: MIT
import XCTest
import Foundation
@testable import ActionCheeseSyncKit

/// Not a test: a harness that writes Swift's decode to disk so it can be
/// compared against ffmpeg's, sample for sample.
final class DumpDecode: XCTestCase {
    func testDumpForComparison() async throws {
        guard ProcessInfo.processInfo.environment["DUMP_DECODE"] != nil else { return }
        for name in ["camB.mov", "recorder.wav", "callout_iphone.wav"] {
            let url = ParityTests.media(name)
            let samples = try await AudioDecoder.decodeMono(url: url)
            let out = URL(fileURLWithPath: "/tmp/swiftdecode-\(name).f32")
            try Data(bytes: samples, count: samples.count * 4).write(to: out)
            print("DUMP \(name) samples=\(samples.count) -> \(out.path)")
        }
    }
}
