// SPDX-License-Identifier: MIT
import AVFoundation
import Foundation

/// Decodes a span of a media file to mono `Float` at a fixed sample rate.
///
/// AVAssetReader rather than ffmpeg: this ships inside an App Store app, and it
/// reads everything the shoot produces -- Sony XAVC S/HS MP4, iPhone MOV, and
/// plain WAV -- through the same path the rest of the system uses.
public enum AudioDecoder {

    /// Analysis rate.  Sync cues live in transients, not in high frequencies,
    /// so 8 kHz resolves offsets far finer than a video frame while keeping
    /// correlation cheap.
    public static let sampleRate: Double = 8000

    public enum DecodeError: Error, LocalizedError {
        case noAudioTrack(URL)
        case readerFailed(URL, String)

        public var errorDescription: String? {
            switch self {
            case .noAudioTrack(let url):
                return "no audio track in \(url.lastPathComponent)"
            case .readerFailed(let url, let detail):
                return "could not read \(url.lastPathComponent): \(detail)"
            }
        }
    }

    /// Decode `duration` seconds starting `offset` seconds into the file.
    ///
    /// Returns an empty array when the file has no audio, which is a normal
    /// condition -- a silent B-roll clip cannot contribute to sync -- rather
    /// than an error.
    public static func decodeMono(
        url: URL,
        offset: Double = 0,
        duration: Double? = nil,
        rate: Double = sampleRate
    ) async throws -> [Float] {
        let asset = AVURLAsset(url: url)
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else {
            return []
        }

        let reader = try AVAssetReader(asset: asset)
        if offset > 0 || duration != nil {
            let start = CMTime(seconds: max(0, offset), preferredTimescale: 48_000)
            let length = duration.map { CMTime(seconds: $0, preferredTimescale: 48_000) }
                ?? CMTime.positiveInfinity
            reader.timeRange = CMTimeRange(start: start, duration: length)
        }

        // Decode at the file's own rate and downsample here.
        //
        // Asking AVAssetReader for 8 kHz directly is tempting and wrong: its
        // converter aliases badly at that ratio.  Measured against ffmpeg on
        // the same files, its output barely correlated (0.05) and transients
        // moved by up to 18 ms -- far more than a frame, and fatal for sync.
        // Our own windowed-sinc decimation is deterministic, matches ffmpeg to
        // well under a millisecond, and behaves the same on every container.
        let nativeRate = try await track.load(.naturalTimeScale)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMIsNonInterleaved: false,
            AVLinearPCMIsBigEndianKey: false,
            AVNumberOfChannelsKey: 1,
        ]
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else {
            throw DecodeError.readerFailed(url, "cannot attach PCM output")
        }
        reader.add(output)

        guard reader.startReading() else {
            throw DecodeError.readerFailed(url, reader.error?.localizedDescription ?? "unknown")
        }

        var samples: [Float] = []
        if let duration {
            samples.reserveCapacity(Int(duration * rate))
        }

        while let buffer = output.copyNextSampleBuffer() {
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let count = CMBlockBufferGetDataLength(block)
            var bytes = [UInt8](repeating: 0, count: count)
            if CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: count,
                                          destination: &bytes) == kCMBlockBufferNoErr {
                bytes.withUnsafeBytes { raw in
                    samples.append(contentsOf: raw.bindMemory(to: Float.self))
                }
            }
            CMSampleBufferInvalidate(buffer)
        }

        if reader.status == .failed {
            throw DecodeError.readerFailed(url, reader.error?.localizedDescription ?? "unknown")
        }
        return Resampler.toRate(samples, from: Double(nativeRate), to: rate)
    }

    /// The file's recording start, when the camera stamped one.
    ///
    /// Clocks differ between devices, but each device's own clock is
    /// consistent, so this pins a whole camera's footage once any one of its
    /// clips has been matched.
    public static func creationDate(url: URL) async -> Date? {
        let asset = AVURLAsset(url: url)
        return try? await asset.load(.creationDate)?.load(.dateValue)
    }
}
