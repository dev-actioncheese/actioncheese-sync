// SPDX-License-Identifier: MIT
import Foundation

/// A clip to be placed, or to place others against.
public struct SyncClip: Sendable, Identifiable {
    public let id: String
    public let url: URL
    /// Clips sharing a device never match each other: one camera cannot record
    /// two things at once, and on real footage two sequential takes from one
    /// body correlate at ~0.5 on shared room tone alone.
    public let device: String
    /// Seconds into the file where the used range begins.
    public let sourceOffset: Double
    /// Length of the used range; `nil` means the whole file.
    public let duration: Double?

    public init(id: String, url: URL, device: String,
                sourceOffset: Double = 0, duration: Double? = nil) {
        self.id = id; self.url = url; self.device = device
        self.sourceOffset = sourceOffset; self.duration = duration
    }
}

/// A clip whose absolute start is already known -- in ActionCheese, an iPhone
/// clip placed from its time-of-day timecode.  Anchors never move.
public struct SyncAnchor: Sendable {
    public let clip: SyncClip
    public let start: Double

    public init(clip: SyncClip, start: Double) {
        self.clip = clip; self.start = start
    }
}

public enum SyncOutcome: Sendable {
    /// Placed at `start`, with the drift its clock showed against the peer it
    /// was measured from.  Drift is reported only; picture is never resampled.
    case placed(start: Double, confidence: Double, driftPPM: Double?)
    case unmatched(reason: String)
}

public struct SyncResult: Sendable {
    public let clipID: String
    public let outcome: SyncOutcome
}

public struct SyncOptions: Sendable {
    /// Minimum correlation to accept a match.
    public var minConfidence: Double = 0.45
    /// Minimum common material behind a match.  A coefficient from a few
    /// seconds of envelope is close to meaningless: on a real four-camera
    /// project a pair that never overlapped scored 0.69 on six seconds, higher
    /// than any correct match in that project.  Against hand-verified ground
    /// truth elsewhere, every correct match rested on at least 24 seconds.
    public var minOverlapSeconds: Double = 20
    /// Sharpen accepted matches with GCC-PHAT on raw audio.
    public var refine: Bool = true
    public var measureDrift: Bool = true

    public init() {}
}

/// Waveform sync for clips whose clocks do not match the shoot's.
public enum ActionCheeseSync {

    /// Place `candidates` against `anchors` using sound alone.
    ///
    /// Candidates may match each other as well as the anchors -- a second Sony
    /// body that only ever overlaps the first still reaches the timeline
    /// through it -- but a group that never reaches an anchor has no knowable
    /// absolute time and is reported unmatched rather than guessed at.
    public static func sync(
        anchors: [SyncAnchor],
        candidates: [SyncClip],
        options: SyncOptions = SyncOptions(),
        progress: (@Sendable (Double, String) -> Void)? = nil
    ) async throws -> [SyncResult] {

        let clips = anchors.map(\.clip) + candidates
        let anchorCount = anchors.count
        guard !clips.isEmpty else { return [] }

        progress?(0.05, "decoding audio")
        var envelopes = [[Float]](repeating: [], count: clips.count)
        var durations = [Double](repeating: 0, count: clips.count)
        var failures = [String?](repeating: nil, count: clips.count)

        for (index, clip) in clips.enumerated() {
            do {
                let samples = try await AudioDecoder.decodeMono(
                    url: clip.url, offset: clip.sourceOffset, duration: clip.duration)
                if samples.isEmpty {
                    failures[index] = "no usable audio"
                } else {
                    envelopes[index] = OnsetEnvelope.compute(samples: samples)
                    durations[index] = clip.duration
                        ?? Double(samples.count) / AudioDecoder.sampleRate
                    if envelopes[index].isEmpty { failures[index] = "no usable audio" }
                }
            } catch {
                failures[index] = error.localizedDescription
            }
            progress?(0.05 + 0.5 * Double(index + 1) / Double(clips.count), "decoding audio")
        }

        progress?(0.55, "matching clips")
        var edges: [Solver.Edge] = []
        for i in 0..<clips.count {
            guard failures[i] == nil else { continue }
            for j in (i + 1)..<clips.count {
                guard failures[j] == nil else { continue }
                // One device cannot record two things at once.
                if clips[i].device == clips[j].device { continue }

                guard let match = Correlation.matchEnvelopes(
                    envelopes[i], envelopes[j],
                    minOverlapSeconds: options.minOverlapSeconds
                ), match.confidence >= options.minConfidence else { continue }

                var delta = match.offsetSeconds
                if options.refine {
                    delta = await refined(
                        clips[i], clips[j], durations[i], durations[j], delta) ?? delta
                }
                edges.append(Solver.Edge(a: i, b: j, delta: delta,
                                         confidence: match.confidence,
                                         overlapSeconds: match.overlapSeconds))
            }
        }

        progress?(0.8, "solving offsets")
        var fixed: [Int: Double] = [:]
        for (index, anchor) in anchors.enumerated() { fixed[index] = anchor.start }
        let solution = Solver.solve(nodeCount: clips.count, edges: edges, anchors: fixed)

        // Drift, measured against the best-connected peer already placed.
        var drift: [Int: Double] = [:]
        if options.measureDrift {
            progress?(0.9, "measuring drift")
            var bestPeer: [Int: (Double, Int)] = [:]
            for edge in edges {
                for (node, peer) in [(edge.a, edge.b), (edge.b, edge.a)] {
                    if solution.positions[node] == nil || solution.positions[peer] == nil { continue }
                    if bestPeer[node] == nil || edge.weight > bestPeer[node]!.0 {
                        bestPeer[node] = (edge.weight, peer)
                    }
                }
            }
            for index in anchorCount..<clips.count {
                guard let (_, peer) = bestPeer[index],
                      let here = solution.positions[index],
                      let there = solution.positions[peer] else { continue }
                let estimate = await Drift.measure(
                    targetURL: clips[index].url, targetOffset: clips[index].sourceOffset,
                    targetDuration: durations[index],
                    peerURL: clips[peer].url, peerOffset: clips[peer].sourceOffset,
                    peerDuration: durations[peer],
                    delta: here - there)
                if estimate.measured { drift[index] = estimate.driftPPM }
            }
        }

        var confidenceByNode: [Int: Double] = [:]
        for edge in edges {
            confidenceByNode[edge.a] = max(confidenceByNode[edge.a] ?? 0, edge.confidence)
            confidenceByNode[edge.b] = max(confidenceByNode[edge.b] ?? 0, edge.confidence)
        }
        let floatingNodes = Set(solution.floating.flatMap { $0 })

        progress?(1.0, "done")
        return candidates.enumerated().map { offset, clip in
            let index = anchorCount + offset
            if let reason = failures[index] {
                return SyncResult(clipID: clip.id, outcome: .unmatched(reason: reason))
            }
            if let start = solution.positions[index] {
                return SyncResult(clipID: clip.id, outcome: .placed(
                    start: start,
                    confidence: confidenceByNode[index] ?? 0,
                    driftPPM: drift[index]))
            }
            if floatingNodes.contains(index) {
                return SyncResult(clipID: clip.id, outcome: .unmatched(
                    reason: "synced to other clips but not to any anchor"))
            }
            return SyncResult(clipID: clip.id, outcome: .unmatched(
                reason: "no confident audio match"))
        }
    }

    /// GCC-PHAT on raw audio in the overlap, taking a 10 ms coarse answer to
    /// well under a sample.
    static func refined(
        _ a: SyncClip, _ b: SyncClip,
        _ durationA: Double, _ durationB: Double, _ delta: Double
    ) async -> Double? {
        guard durationA > 0, durationB > 0 else { return nil }
        let overlapStart = max(0, delta)
        let overlapEnd = min(durationA, delta + durationB)
        guard overlapEnd - overlapStart >= 2 else { return nil }

        let window = min(20.0, overlapEnd - overlapStart)
        let centre = (overlapStart + overlapEnd) / 2
        let startA = max(overlapStart, centre - window / 2)
        let startB = startA - delta

        guard
            let samplesA = try? await AudioDecoder.decodeMono(
                url: a.url, offset: a.sourceOffset + startA, duration: window),
            let samplesB = try? await AudioDecoder.decodeMono(
                url: b.url, offset: b.sourceOffset + startB, duration: window)
        else { return nil }

        let (residual, peak) = Correlation.gccPhat(samplesA, samplesB)
        // A residual pinned to the search limit means the coarse offset was
        // not close enough for PHAT to mean anything.
        guard peak > 0, abs(residual) < 0.05 * 0.99 else { return nil }
        return delta + residual
    }
}
