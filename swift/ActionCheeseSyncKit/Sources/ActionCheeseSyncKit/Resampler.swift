// SPDX-License-Identifier: MIT
import Accelerate
import Foundation

/// Windowed-sinc resampling, done in the engine rather than in the decoder.
///
/// AVAssetReader will happily convert straight to 8 kHz, but its converter
/// aliases at that ratio: measured against ffmpeg on the same files, the
/// decoded waveforms barely correlated (0.05) and transients shifted by up to
/// 18 ms.  Sync lives in transient timing, so the resampler cannot be left to
/// chance -- and doing it here means every container is treated identically.
/// With this in place the two engines' decodes agree to zero samples at a
/// correlation of 0.999.
enum Resampler {

    /// Half-width of the kernel in *output* samples.  Sixteen per side is well
    /// past the point where more taps move a correlation peak.
    static let halfWidth = 16
    /// Sub-sample positions the kernel is precomputed for.  The error from
    /// snapping to the nearest phase is under a thousandth of an input sample.
    static let phaseCount = 512

    /// Precomputed kernel: `phases[p]` are the weights to apply at input
    /// offsets `-radius ... +radius` for fractional position `p / phaseCount`.
    private struct Kernel {
        let radius: Int
        let phases: [[Float]]
    }

    private static func makeKernel(ratio: Double) -> Kernel {
        let cutoff = min(0.5, 0.5 * ratio) * 0.92
        let span = Double(halfWidth) / min(1.0, ratio)
        let radius = Int(span.rounded(.up))

        var phases: [[Float]] = []
        phases.reserveCapacity(phaseCount)
        for phase in 0..<phaseCount {
            let fraction = Double(phase) / Double(phaseCount)
            var weights = [Float](repeating: 0, count: 2 * radius + 1)
            var total = 0.0
            for tap in -radius...radius {
                let distance = Double(tap) - fraction
                if abs(distance) > span { continue }
                let scaled = distance * 2 * cutoff
                let sinc = abs(scaled) < 1e-9
                    ? 1.0
                    : sin(Double.pi * scaled) / (Double.pi * scaled)
                let angle = Double.pi * (distance / span)
                let window = 0.42 + 0.5 * cos(angle) + 0.08 * cos(2 * angle)
                let weight = sinc * window
                weights[tap + radius] = Float(weight)
                total += weight
            }
            if total != 0 {
                var scale = Float(1.0 / total)
                vDSP_vsmul(weights, 1, &scale, &weights, 1, vDSP_Length(weights.count))
            }
            phases.append(weights)
        }
        return Kernel(radius: radius, phases: phases)
    }

    static func toRate(_ samples: [Float], from input: Double, to output: Double) -> [Float] {
        guard !samples.isEmpty, input > 0, output > 0 else { return samples }
        if abs(input - output) < 0.5 { return samples }

        let ratio = output / input
        let count = Int((Double(samples.count) * ratio).rounded(.down))
        guard count > 0 else { return [] }

        let kernel = makeKernel(ratio: ratio)
        let radius = kernel.radius
        let width = 2 * radius + 1

        // Pad so every output position has a full kernel under it; this is
        // also what keeps the first and last samples from being attenuated.
        var padded = [Float](repeating: 0, count: samples.count + 2 * radius)
        padded.replaceSubrange(radius..<(radius + samples.count), with: samples)

        var out = [Float](repeating: 0, count: count)
        padded.withUnsafeBufferPointer { source in
            for index in 0..<count {
                let centre = Double(index) / ratio
                let base = Int(centre.rounded(.down))
                var phase = Int(((centre - Double(base)) * Double(phaseCount)).rounded())
                var start = base
                if phase >= phaseCount { phase -= phaseCount; start += 1 }

                var value: Float = 0
                kernel.phases[phase].withUnsafeBufferPointer { weights in
                    vDSP_dotpr(source.baseAddress! + start, 1,
                               weights.baseAddress!, 1, &value, vDSP_Length(width))
                }
                out[index] = value
            }
        }
        return out
    }
}
