// SPDX-License-Identifier: MIT
import Accelerate
import Foundation

/// Real-to-complex FFT over power-of-two lengths, matching NumPy's `rfft`
/// scaling so results can be compared against the Python engine directly.
///
/// vDSP's packed real transform returns everything at twice NumPy's amplitude,
/// with DC and Nyquist sharing the first complex slot; both are undone here so
/// the rest of the port can be written as a straight translation.
final class FFT {
    private let log2n: vDSP_Length
    private let count: Int
    private let setup: FFTSetup

    private static var cache: [Int: FFT] = [:]
    private static let lock = NSLock()

    static func next(_ minimum: Int) -> FFT {
        var size = 1
        while size < max(2, minimum) { size <<= 1 }
        lock.lock()
        defer { lock.unlock() }
        if let existing = cache[size] { return existing }
        let made = FFT(size: size)
        cache[size] = made
        return made
    }

    var size: Int { count }

    private init(size: Int) {
        count = size
        log2n = vDSP_Length(log2(Double(size)).rounded())
        setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2))!
    }

    deinit { vDSP_destroy_fftsetup(setup) }

    /// Forward transform of a real signal, zero-padded to the FFT length.
    /// Returns `count/2 + 1` bins as separate real and imaginary parts.
    func forward(_ signal: [Float]) -> (real: [Float], imaginary: [Float]) {
        var padded = signal
        if padded.count < count {
            padded.append(contentsOf: repeatElement(0, count: count - padded.count))
        } else if padded.count > count {
            padded = Array(padded[0..<count])
        }

        let half = count / 2
        var real = [Float](repeating: 0, count: half)
        var imaginary = [Float](repeating: 0, count: half)

        real.withUnsafeMutableBufferPointer { realBuffer in
            imaginary.withUnsafeMutableBufferPointer { imagBuffer in
                var split = DSPSplitComplex(realp: realBuffer.baseAddress!,
                                            imagp: imagBuffer.baseAddress!)
                padded.withUnsafeBufferPointer { input in
                    input.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: half) {
                        vDSP_ctoz($0, 2, &split, 1, vDSP_Length(half))
                    }
                }
                vDSP_fft_zrip(setup, &split, 1, log2n, FFTDirection(FFT_FORWARD))
            }
        }

        // Unpack: slot 0 carries DC in real and Nyquist in imaginary, and the
        // whole transform comes back at twice NumPy's amplitude.
        var outReal = [Float](repeating: 0, count: half + 1)
        var outImag = [Float](repeating: 0, count: half + 1)
        outReal[0] = real[0] / 2
        outImag[0] = 0
        outReal[half] = imaginary[0] / 2
        outImag[half] = 0
        for k in 1..<half {
            outReal[k] = real[k] / 2
            outImag[k] = imaginary[k] / 2
        }
        return (outReal, outImag)
    }

    /// Inverse of `forward`, matching NumPy's `irfft` normalisation.
    func inverse(real: [Float], imaginary: [Float]) -> [Float] {
        let half = count / 2
        var packedReal = [Float](repeating: 0, count: half)
        var packedImag = [Float](repeating: 0, count: half)
        packedReal[0] = real[0] * 2
        packedImag[0] = (real.count > half ? real[half] : 0) * 2
        for k in 1..<half {
            packedReal[k] = real[k] * 2
            packedImag[k] = imaginary[k] * 2
        }

        var output = [Float](repeating: 0, count: count)
        packedReal.withUnsafeMutableBufferPointer { realBuffer in
            packedImag.withUnsafeMutableBufferPointer { imagBuffer in
                var split = DSPSplitComplex(realp: realBuffer.baseAddress!,
                                            imagp: imagBuffer.baseAddress!)
                vDSP_fft_zrip(setup, &split, 1, log2n, FFTDirection(FFT_INVERSE))
                output.withUnsafeMutableBufferPointer { out in
                    out.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: half) {
                        vDSP_ztoc(&split, 1, $0, 2, vDSP_Length(half))
                    }
                }
            }
        }
        // zrip's round trip scales by 2n; NumPy's irfft divides by n.
        var scale = Float(1.0 / Double(2 * count))
        vDSP_vsmul(output, 1, &scale, &output, 1, vDSP_Length(count))
        return output
    }
}
