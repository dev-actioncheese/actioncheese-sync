// SPDX-License-Identifier: MIT
import Foundation

/// Clock-drift estimation.
///
/// Independent recorders do not share a clock.  A camera sampling at a nominal
/// 48 kHz might really run at 48000.9 Hz, inaudible at the head of a take and
/// an obvious echo forty minutes in.  A single best-fit offset cannot express
/// that: it splits the error, leaving both ends slightly wrong.
///
/// The offset is measured in several windows across the clip and a line fitted
/// through them; the slope is the drift.  Two details matter:
///
/// * Windows are matched on **onset envelopes**, not raw waveforms.  Raw
///   correlation between two different microphones is multi-peaked, so a
///   per-window raw match jumps between peaks and the slope becomes noise.
/// * The probe envelope rate is **four times** the matching rate.  Drift is a
///   millisecond effect measured over minutes; at 100 Hz the quantisation
///   alone costs about a quarter of the answer.
///
/// Drift is only ever reported.  Picture is never resampled.
public enum Drift {

    public static let windowSeconds: Double = 60
    public static let probeCount = 5
    public static let minDurationSeconds: Double = 300
    public static let probeEnvelopeRate: Double = 400
    public static let minProbeConfidence = 0.30
    public static let maxFitResidualSeconds = 0.010

    /// The lever arm matters as much as the fit: a tight line over two minutes
    /// says nothing about parts per million.
    public static let minBaselineSeconds: Double = 300
    /// How many standard errors the slope must clear to count as real.
    public static let significance = 3.0

    public struct Estimate: Sendable {
        public let driftPPM: Double
        /// Playback speed that cancels it; 100 means no change.
        public let speedPercent: Double
        public let measured: Bool
        public let probeCount: Int
        public let reason: String?
    }

    static func unmeasured(_ reason: String) -> Estimate {
        Estimate(driftPPM: 0, speedPercent: 100, measured: false, probeCount: 0, reason: reason)
    }

    /// Weighted least-squares line fit; returns slope, residual RMS and the
    /// standard error of the slope.
    static func fitLine(
        times: [Double], offsets: [Double], weights: [Double]
    ) -> (slope: Double, residualRMS: Double, standardError: Double) {
        let n = times.count
        let sumW = weights.reduce(0, +)
        let meanX = zip(times, weights).map(*).reduce(0, +) / sumW
        let meanY = zip(offsets, weights).map(*).reduce(0, +) / sumW
        var sxx = 0.0, sxy = 0.0
        for i in 0..<n {
            sxx += weights[i] * (times[i] - meanX) * (times[i] - meanX)
            sxy += weights[i] * (times[i] - meanX) * (offsets[i] - meanY)
        }
        let slope = sxx > 0 ? sxy / sxx : 0
        let intercept = meanY - slope * meanX
        let residuals = (0..<n).map { offsets[$0] - (slope * times[$0] + intercept) }
        let rms = (residuals.map { $0 * $0 }.reduce(0, +) / Double(n)).squareRoot()

        let spread = times.map { ($0 - times.reduce(0, +) / Double(n)) }
            .map { $0 * $0 }.reduce(0, +)
        let degrees = max(1, n - 2)
        let sigma = (residuals.map { $0 * $0 }.reduce(0, +) / Double(degrees)).squareRoot()
        let standardError = spread > 0 ? sigma / spread.squareRoot() : .infinity
        return (slope, rms, standardError)
    }

    /// Measure one clip's drift against a peer whose position is already known.
    ///
    /// `delta` is the current best offset: the peer is read from
    /// `targetTime + delta`.
    public static func measure(
        targetURL: URL, targetOffset: Double, targetDuration: Double,
        peerURL: URL, peerOffset: Double, peerDuration: Double,
        delta: Double
    ) async -> Estimate {
        guard targetDuration >= minDurationSeconds else {
            return unmeasured("clip shorter than \(Int(minDurationSeconds))s")
        }

        // Overlapping span in the target's own time.  Clamping at zero
        // matters: a negative read would silently return the wrong audio.
        let overlapStart = max(0, -delta)
        let overlapEnd = min(targetDuration, peerDuration - delta)
        guard overlapEnd - overlapStart >= 3 * windowSeconds else {
            return unmeasured("overlap too short to spread drift probes across")
        }

        let span = overlapEnd - windowSeconds - overlapStart
        var times: [Double] = [], offsets: [Double] = [], weights: [Double] = []
        for index in 0..<probeCount {
            let t = overlapStart + span * Double(index) / Double(probeCount - 1)
            guard
                let peerSamples = try? await AudioDecoder.decodeMono(
                    url: peerURL, offset: peerOffset + t + delta, duration: windowSeconds),
                let targetSamples = try? await AudioDecoder.decodeMono(
                    url: targetURL, offset: targetOffset + t, duration: windowSeconds),
                peerSamples.count > Int(AudioDecoder.sampleRate * 5),
                targetSamples.count > Int(AudioDecoder.sampleRate * 5)
            else { continue }

            let peerEnvelope = OnsetEnvelope.compute(
                samples: peerSamples, envelopeRate: probeEnvelopeRate)
            let targetEnvelope = OnsetEnvelope.compute(
                samples: targetSamples, envelopeRate: probeEnvelopeRate)
            guard let match = Correlation.matchEnvelopes(
                peerEnvelope, targetEnvelope,
                envelopeRate: probeEnvelopeRate,
                minOverlapSeconds: min(5, windowSeconds / 4),
                guardSeconds: 0.3
            ), match.confidence >= minProbeConfidence else { continue }

            times.append(t + windowSeconds / 2)
            offsets.append(match.offsetSeconds)
            weights.append(match.confidence * match.confidence)
        }

        guard times.count >= 3 else {
            return unmeasured("too few probes correlated confidently")
        }
        let baseline = times[times.count - 1] - times[0]
        guard baseline >= minBaselineSeconds else {
            return unmeasured("probe baseline only \(Int(baseline))s; need \(Int(minBaselineSeconds))s")
        }

        let (slope, rms, standardError) = fitLine(times: times, offsets: offsets, weights: weights)
        guard rms <= maxFitResidualSeconds else {
            return unmeasured(String(format: "drift fit inconsistent (%.1f ms residual)", rms * 1000))
        }
        guard abs(slope) < 0.5 else { return unmeasured("implausible drift slope") }
        guard abs(slope) >= significance * standardError else {
            return unmeasured("drift not distinguishable from noise")
        }

        // slope is d(offset)/d(target time).  A recorder running fast makes the
        // required offset shrink, so the clock ratio is 1/(1+slope).
        let ratio = 1 / (1 + slope)
        return Estimate(driftPPM: (ratio - 1) * 1e6, speedPercent: ratio * 100,
                        measured: true, probeCount: times.count, reason: nil)
    }
}
