// SPDX-License-Identifier: MIT
import Accelerate
import Foundation

/// Per-band spectral-flux onset envelope.
///
/// Correlating raw waveforms across devices does not work: a Sony body's
/// on-board mic and an iPhone capture the same room with wildly different
/// frequency response, gain and noise floor, so their samples correlate only
/// weakly even when perfectly aligned.  What they share is the *timing* of
/// transients, so each recording is reduced to onset strength over time, with
/// every band standardised on its own so EQ differences cancel.
public enum OnsetEnvelope {

    /// Envelope frame rate.  10 ms resolution is finer than a video frame at
    /// any normal rate; the GCC-PHAT pass then takes it finer still.
    public static let rate: Double = 100

    static let fftSize = 512
    static let bandCount = 24
    static let fMin: Double = 50
    static let fMax: Double = 3800
    static let epsilon: Float = 1e-9

    /// Band aggregation matrix, `bandCount` rows of `fftSize/2 + 1` weights.
    static func bandMatrix(sampleRate: Double) -> [[Float]] {
        let bins = fftSize / 2 + 1
        let frequencies = (0..<bins).map { Double($0) * sampleRate / Double(fftSize) }
        let top = min(fMax, sampleRate / 2 - 1)
        let edges = (0...bandCount).map { index -> Double in
            let t = Double(index) / Double(bandCount)
            return pow(10, log10(fMin) + t * (log10(top) - log10(fMin)))
        }

        var matrix = [[Float]](repeating: [Float](repeating: 0, count: bins), count: bandCount)
        for band in 0..<bandCount {
            let low = edges[band], high = edges[band + 1]
            var members = (0..<bins).filter { frequencies[$0] >= low && frequencies[$0] < high }
            if members.isEmpty {
                // Narrow low bands can fall between FFT bins; use the nearest
                // one so no band is silently empty.
                let centre = (low + high) / 2
                let nearest = (0..<bins).min {
                    abs(frequencies[$0] - centre) < abs(frequencies[$1] - centre) ? true : false
                } ?? 0
                members = [nearest]
            }
            let weight = Float(1.0 / Double(members.count))
            for bin in members { matrix[band][bin] = weight }
        }
        return matrix
    }

    /// Reduce audio to a zero-mean, unit-norm onset envelope.
    public static func compute(
        samples: [Float],
        sampleRate: Double = AudioDecoder.sampleRate,
        envelopeRate: Double = rate
    ) -> [Float] {
        let hop = max(1, Int((sampleRate / envelopeRate).rounded()))
        if samples.count < hop * 2 { return [] }

        var peak: Float = 0
        vDSP_maxmgv(samples, 1, &peak, vDSP_Length(samples.count))
        if peak <= epsilon {
            // Digital silence carries no sync information.
            return [Float](repeating: 0, count: max(0, samples.count / hop))
        }
        var normalised = [Float](repeating: 0, count: samples.count)
        var inverse = 1 / peak
        vDSP_vsmul(samples, 1, &inverse, &normalised, 1, vDSP_Length(samples.count))

        // Frames, zero-padded when the signal is shorter than one window.
        if normalised.count < fftSize {
            normalised.append(contentsOf: repeatElement(0, count: fftSize - normalised.count))
        }
        let frameCount = 1 + (normalised.count - fftSize) / hop
        if frameCount < 2 { return [] }

        // np.hanning is symmetric: 0.5 - 0.5*cos(2*pi*n/(M-1)).
        let window = (0..<fftSize).map {
            Float(0.5 - 0.5 * cos(2 * Double.pi * Double($0) / Double(fftSize - 1)))
        }
        let fft = FFT.next(fftSize)
        let matrix = bandMatrix(sampleRate: sampleRate)
        let bins = fftSize / 2 + 1

        // bands[band][frame]
        var bands = [[Float]](repeating: [Float](repeating: 0, count: frameCount),
                              count: bandCount)
        var frame = [Float](repeating: 0, count: fftSize)
        for index in 0..<frameCount {
            let start = index * hop
            vDSP_vmul(Array(normalised[start..<(start + fftSize)]), 1, window, 1,
                      &frame, 1, vDSP_Length(fftSize))
            let (real, imaginary) = fft.forward(frame)
            var magnitude = [Float](repeating: 0, count: bins)
            for bin in 0..<bins {
                magnitude[bin] = (real[bin] * real[bin] + imaginary[bin] * imaginary[bin]).squareRoot()
            }
            for band in 0..<bandCount {
                var total: Float = 0
                vDSP_dotpr(matrix[band], 1, magnitude, 1, &total, vDSP_Length(bins))
                bands[band][index] = total
            }
        }

        // log1p, then standardise each band on its own: this is what makes a
        // Sony's mic comparable to an iPhone's.
        for band in 0..<bandCount {
            for index in 0..<frameCount {
                bands[band][index] = log1pf(bands[band][index] * 1000)
            }
            var mean: Float = 0
            vDSP_meanv(bands[band], 1, &mean, vDSP_Length(frameCount))
            var centred = [Float](repeating: 0, count: frameCount)
            var negated = -mean
            vDSP_vsadd(bands[band], 1, &negated, &centred, 1, vDSP_Length(frameCount))
            var meanSquare: Float = 0
            vDSP_measqv(centred, 1, &meanSquare, vDSP_Length(frameCount))
            var scale = 1 / (meanSquare.squareRoot() + epsilon)
            vDSP_vsmul(centred, 1, &scale, &bands[band], 1, vDSP_Length(frameCount))
        }

        // Half-wave-rectified first difference, averaged over bands.
        var envelope = [Float](repeating: 0, count: frameCount - 1)
        for index in 0..<(frameCount - 1) {
            var total: Float = 0
            for band in 0..<bandCount {
                total += max(0, bands[band][index + 1] - bands[band][index])
            }
            envelope[index] = total / Float(bandCount)
        }

        var mean: Float = 0
        vDSP_meanv(envelope, 1, &mean, vDSP_Length(envelope.count))
        var negated = -mean
        vDSP_vsadd(envelope, 1, &negated, &envelope, 1, vDSP_Length(envelope.count))
        var energy: Float = 0
        vDSP_svesq(envelope, 1, &energy, vDSP_Length(envelope.count))
        let norm = energy.squareRoot()
        if norm > epsilon {
            var scale = 1 / norm
            vDSP_vsmul(envelope, 1, &scale, &envelope, 1, vDSP_Length(envelope.count))
        }
        return envelope
    }

    /// Peak envelope for drawing a waveform, in 0...1.
    public static func peaks(samples: [Float], buckets: Int = 600) -> [Float] {
        guard !samples.isEmpty, buckets > 0 else { return [] }
        let count = min(buckets, samples.count)
        var out = [Float](repeating: 0, count: count)
        for index in 0..<count {
            let low = samples.count * index / count
            let high = max(low + 1, samples.count * (index + 1) / count)
            var peak: Float = 0
            samples.withUnsafeBufferPointer {
                vDSP_maxmgv($0.baseAddress! + low, 1, &peak, vDSP_Length(high - low))
            }
            out[index] = peak
        }
        if let ceiling = out.max(), ceiling > epsilon {
            var scale = 1 / ceiling
            vDSP_vsmul(out, 1, &scale, &out, 1, vDSP_Length(count))
        }
        return out
    }
}
