// SPDX-License-Identifier: MIT
import Accelerate
import Foundation

/// Offset convention, stated exactly because a sign error here is invisible in
/// the code and obvious in the edit suite:
///
///     `offset` satisfies `a[t + offset] == b[t]`
///
/// So if both recordings catch one event at internal times `tA` and `tB`, then
/// `offset == tA - tB`, and the two are aligned by placing b at
/// `position(a) + offset`.
public enum Correlation {

    public struct Match: Sendable {
        /// Place b at `position(a) + offsetSeconds` to align the two.
        public let offsetSeconds: Double
        /// Peak normalised correlation, in -1...1.
        public let confidence: Double
        /// Peak height over the best competing peak elsewhere.
        public let peakRatio: Double
        /// Length of the overlapping region at the winning offset.
        public let overlapSeconds: Double
    }

    static let epsilon = 1e-12

    /// Sub-sample peak position by fitting a parabola through three points.
    static func parabolicPeak(_ values: [Float], _ index: Int) -> Double {
        guard index > 0, index < values.count - 1 else { return Double(index) }
        let left = Double(values[index - 1])
        let centre = Double(values[index])
        let right = Double(values[index + 1])
        let denominator = left - 2 * centre + right
        if abs(denominator) < epsilon { return Double(index) }
        return Double(index) + 0.5 * (left - right) / denominator
    }

    /// Correlation coefficient of a and b at every lag.
    ///
    /// Each lag is normalised by the energy of the *actually overlapping*
    /// region; without that, long clips beat short ones on raw magnitude
    /// regardless of how well they line up.
    ///
    /// Returns coefficients and overlap counts indexed from lag `-(b.count-1)`.
    static func normalised(
        _ a: [Float], _ b: [Float], minOverlap: Int
    ) -> (coefficients: [Float], overlaps: [Int]) {
        let n = a.count, m = b.count
        guard n > 0, m > 0 else { return ([], []) }

        let length = n + m - 1
        let fft = FFT.next(max(length, 2))
        let (ar, ai) = fft.forward(a)
        let (br, bi) = fft.forward(b)
        var realPart = [Float](repeating: 0, count: ar.count)
        var imagPart = [Float](repeating: 0, count: ar.count)
        for k in 0..<ar.count {
            // a * conj(b)
            realPart[k] = ar[k] * br[k] + ai[k] * bi[k]
            imagPart[k] = ai[k] * br[k] - ar[k] * bi[k]
        }
        let circular = fft.inverse(real: realPart, imaginary: imagPart)

        // Prefix sums of squares, for the per-lag overlap energies.
        var cumulativeA = [Double](repeating: 0, count: n + 1)
        for i in 0..<n { cumulativeA[i + 1] = cumulativeA[i] + Double(a[i]) * Double(a[i]) }
        var cumulativeB = [Double](repeating: 0, count: m + 1)
        for i in 0..<m { cumulativeB[i + 1] = cumulativeB[i] + Double(b[i]) * Double(b[i]) }

        var coefficients = [Float](repeating: 0, count: length)
        var overlaps = [Int](repeating: 0, count: length)
        let floor = max(1, minOverlap)

        for index in 0..<length {
            let lag = index - (m - 1)
            let lowB = max(0, -lag)
            let highB = min(m, n - lag)
            let overlap = max(0, highB - lowB)
            overlaps[index] = overlap
            guard overlap >= floor else { continue }

            let lowA = lowB + lag, highA = highB + lag
            let energyA = cumulativeA[min(max(highA, 0), n)] - cumulativeA[min(max(lowA, 0), n)]
            let energyB = cumulativeB[min(max(highB, 0), m)] - cumulativeB[min(max(lowB, 0), m)]
            let denominator = (max(energyA, 0) * max(energyB, 0)).squareRoot()
            guard denominator > epsilon else { continue }

            let numerator = Double(circular[lag >= 0 ? lag : fft.size + lag])
            coefficients[index] = Float(numerator / denominator)
        }
        return (coefficients, overlaps)
    }

    /// Find the offset between two onset envelopes.
    public static func matchEnvelopes(
        _ a: [Float], _ b: [Float],
        envelopeRate: Double = OnsetEnvelope.rate,
        minOverlapSeconds: Double = 20,
        guardSeconds: Double = 0.5
    ) -> Match? {
        let minOverlap = Int((minOverlapSeconds * envelopeRate).rounded())
        let (coefficients, overlaps) = normalised(a, b, minOverlap: minOverlap)
        guard !coefficients.isEmpty, coefficients.contains(where: { $0 != 0 }) else { return nil }

        // Peak selection uses a length-penalised score, not the raw
        // coefficient.  A brief overlap can correlate *perfectly* by
        // coincidence -- one clip's tail against another's head -- and would
        // otherwise outrank a long, slightly noisier, obviously correct
        // alignment.  A correlation's standard error falls as 1/sqrt(N), so
        // weighting by sqrt(N) is what "how much evidence" means.
        let largest = Double(overlaps.max() ?? 0)
        guard largest > 0 else { return nil }
        var score = [Float](repeating: 0, count: coefficients.count)
        for index in 0..<coefficients.count {
            score[index] = coefficients[index] * Float((Double(overlaps[index]) / largest).squareRoot())
        }

        var peak = 0
        for index in 1..<score.count where score[index] > score[peak] { peak = index }

        // The runner-up is measured outside a guard band, so the shoulders of
        // the true peak are not mistaken for a competing hypothesis.
        let guardWidth = max(1, Int((guardSeconds * envelopeRate).rounded()))
        var runnerUp: Float = 0
        for index in 0..<score.count where abs(index - peak) > guardWidth {
            runnerUp = max(runnerUp, score[index])
        }
        let peakScore = Double(score[peak])
        let ratio = peakScore > 0 ? peakScore / max(Double(runnerUp), 1e-6) : 0

        let fractional = parabolicPeak(coefficients, peak)
        let firstLag = Double(-(b.count - 1))
        return Match(
            offsetSeconds: (fractional + firstLag) / envelopeRate,
            confidence: Double(coefficients[peak]),
            peakRatio: ratio,
            overlapSeconds: Double(overlaps[peak]) / envelopeRate
        )
    }

    /// Phase-transform cross-correlation of two roughly aligned signals.
    ///
    /// PHAT whitening discards magnitude and keeps only phase, so the result
    /// depends on transient timing rather than spectral content -- exactly the
    /// invariance needed across different microphones.
    ///
    /// Returns the residual offset in seconds and the peak height.
    public static func gccPhat(
        _ a: [Float], _ b: [Float],
        sampleRate: Double = AudioDecoder.sampleRate,
        maxLagSeconds: Double = 0.05
    ) -> (offsetSeconds: Double, peak: Double) {
        let length = min(a.count, b.count)
        guard length >= 64 else { return (0, 0) }

        var left = Array(a[0..<length])
        var right = Array(b[0..<length])
        var mean: Float = 0
        vDSP_meanv(left, 1, &mean, vDSP_Length(length))
        var negated = -mean
        vDSP_vsadd(left, 1, &negated, &left, 1, vDSP_Length(length))
        vDSP_meanv(right, 1, &mean, vDSP_Length(length))
        negated = -mean
        vDSP_vsadd(right, 1, &negated, &right, 1, vDSP_Length(length))

        var peakLeft: Float = 0, peakRight: Float = 0
        vDSP_maxmgv(left, 1, &peakLeft, vDSP_Length(length))
        vDSP_maxmgv(right, 1, &peakRight, vDSP_Length(length))
        guard Double(peakLeft) > epsilon, Double(peakRight) > epsilon else { return (0, 0) }

        let fft = FFT.next(2 * length)
        let (ar, ai) = fft.forward(left)
        let (br, bi) = fft.forward(right)
        var realPart = [Float](repeating: 0, count: ar.count)
        var imagPart = [Float](repeating: 0, count: ar.count)
        for k in 0..<ar.count {
            let re = ar[k] * br[k] + ai[k] * bi[k]
            let im = ai[k] * br[k] - ar[k] * bi[k]
            let magnitude = (re * re + im * im).squareRoot() + Float(epsilon)
            realPart[k] = re / magnitude
            imagPart[k] = im / magnitude
        }
        let correlation = fft.inverse(real: realPart, imaginary: imagPart)

        let maxLag = min(Int((maxLagSeconds * sampleRate).rounded()), fft.size / 2 - 1)
        guard maxLag >= 1 else { return (0, 0) }

        // Rotate so index 0 of the window is lag -maxLag.
        var window = [Float](repeating: 0, count: 2 * maxLag + 1)
        for index in 0..<maxLag { window[index] = correlation[fft.size - maxLag + index] }
        for index in 0...maxLag { window[maxLag + index] = correlation[index] }

        var peak = 0
        for index in 1..<window.count where window[index] > window[peak] { peak = index }
        let fractional = parabolicPeak(window, peak)
        return ((fractional - Double(maxLag)) / sampleRate, Double(window[peak]))
    }
}
