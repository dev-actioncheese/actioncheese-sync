# SPDX-License-Identifier: GPL-3.0-or-later
"""Onset-envelope features.

Correlating raw waveforms across devices does not work: a camera's on-board mic
and a lavalier feeding a field recorder capture the same event with wildly
different frequency response, gain and noise floor, so their samples are only
weakly correlated even when perfectly aligned.

What *is* common between them is the timing of transients.  We therefore reduce
each recording to a per-band spectral-flux onset envelope at 100 Hz, normalising
each band independently so that EQ differences cancel out.  Two recordings of
the same room then correlate strongly, and the envelope is ~800x smaller than
the audio, which is what makes all-pairs matching affordable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import numpy as np

from . import ffmpeg

#: Envelope frame rate in Hz.  10 ms resolution is far finer than a video frame
#: at any normal rate, and the GCC-PHAT refinement pass takes it finer still.
ENVELOPE_RATE = 100

_N_FFT = 512
_N_BANDS = 24
_FMIN = 50.0
_FMAX = 3800.0
_EPS = 1e-9


def _frame(samples: np.ndarray, frame_length: int, hop: int) -> np.ndarray:
    """Slice a signal into overlapping frames (n_frames, frame_length)."""
    if samples.size < frame_length:
        padded = np.zeros(frame_length, dtype=np.float32)
        padded[: samples.size] = samples
        samples = padded
    n_frames = 1 + (samples.size - frame_length) // hop
    strided = np.lib.stride_tricks.as_strided(
        samples,
        shape=(n_frames, frame_length),
        strides=(samples.strides[0] * hop, samples.strides[0]),
        writeable=False,
    )
    return strided


def _band_matrix(sample_rate: int, n_fft: int, n_bands: int) -> np.ndarray:
    """Triangular-ish log-spaced band aggregation matrix (n_bands, n_bins)."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    fmax = min(_FMAX, sample_rate / 2.0 - 1.0)
    edges = np.logspace(np.log10(_FMIN), np.log10(fmax), n_bands + 1)

    matrix = np.zeros((n_bands, freqs.size), dtype=np.float32)
    for band in range(n_bands):
        lo, hi = edges[band], edges[band + 1]
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():
            # Narrow low bands can fall between FFT bins; fall back to the
            # single nearest bin so no band is silently empty.
            mask = np.zeros_like(mask)
            mask[np.argmin(np.abs(freqs - (lo + hi) / 2))] = True
        matrix[band, mask] = 1.0 / mask.sum()
    return matrix


def onset_envelope(
    samples: np.ndarray,
    sample_rate: int,
    envelope_rate: int = ENVELOPE_RATE,
) -> np.ndarray:
    """Reduce audio to a normalised onset-strength envelope."""
    hop = max(1, int(round(sample_rate / envelope_rate)))
    if samples.size < hop * 2:
        return np.zeros(0, dtype=np.float32)

    samples = np.ascontiguousarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak <= _EPS:
        # Digital silence carries no sync information.
        return np.zeros(max(0, samples.size // hop), dtype=np.float32)
    samples = samples / peak

    frames = _frame(samples, _N_FFT, hop)
    window = np.hanning(_N_FFT).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)

    bands = _band_matrix(sample_rate, _N_FFT, _N_BANDS) @ spectrum.T  # (n_bands, n_frames)
    log_bands = np.log1p(bands * 1000.0)

    # Per-band standardisation is the step that makes a camera mic comparable
    # to a lav: it removes each band's level and dynamic range, leaving only
    # the *shape* of activity over time.
    mean = log_bands.mean(axis=1, keepdims=True)
    std = log_bands.std(axis=1, keepdims=True)
    log_bands = (log_bands - mean) / (std + _EPS)

    flux = np.maximum(0.0, np.diff(log_bands, axis=1))
    envelope = flux.mean(axis=0)

    envelope -= envelope.mean()
    scale = float(np.sqrt(np.sum(envelope ** 2)))
    if scale > _EPS:
        envelope /= scale
    return envelope.astype(np.float32)


def peak_envelope(samples: np.ndarray, buckets: int = 600) -> np.ndarray:
    """Downsample audio to a drawable peak envelope in [0, 1].

    Peaks rather than RMS: a waveform overview should show transients at their
    real height, which is what an editor looks for when eyeballing sync.
    """
    if samples.size == 0 or buckets < 1:
        return np.zeros(0, dtype=np.float32)

    magnitude = np.abs(samples)
    count = min(buckets, magnitude.size)
    edges = np.linspace(0, magnitude.size, count + 1).astype(np.int64)
    out = np.zeros(count, dtype=np.float32)
    for index in range(count):
        lo, hi = edges[index], max(edges[index] + 1, edges[index + 1])
        out[index] = magnitude[lo:hi].max()

    ceiling = float(out.max())
    if ceiling > _EPS:
        out /= ceiling
    return out


class FeatureCache:
    """Disk cache of onset envelopes.

    Decoding is by far the slowest part of a sync run, and envelopes are tiny
    (~1.4 MB per hour of audio), so caching them means a re-run with different
    thresholds is effectively instant.
    """

    def __init__(self, cache_dir: Optional[Path] = None, sample_rate: int = ffmpeg.DEFAULT_SAMPLE_RATE):
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "syncmyvids"
        self.sample_rate = sample_rate
        self._memory: dict[str, np.ndarray] = {}

    def _key(self, path: Path, start: float, duration: Optional[float], stream_index: Optional[int]) -> str:
        try:
            stat = path.stat()
            fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            fingerprint = "missing"
        raw = "|".join([
            str(path.resolve() if path.exists() else path),
            fingerprint,
            f"{start:.6f}",
            "full" if duration is None else f"{duration:.6f}",
            str(stream_index),
            str(self.sample_rate),
            str(ENVELOPE_RATE),
            # Bump when the feature definition changes, so stale caches are ignored.
            "v1",
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def envelope(
        self,
        path: Path,
        *,
        start: float = 0.0,
        duration: Optional[float] = None,
        stream_index: Optional[int] = None,
    ) -> np.ndarray:
        key = self._key(path, start, duration, stream_index)
        if key in self._memory:
            return self._memory[key]

        cache_file = self.cache_dir / f"{key}.npy"
        if cache_file.exists():
            try:
                envelope = np.load(cache_file)
                self._memory[key] = envelope
                return envelope
            except (OSError, ValueError):
                # A truncated or corrupt cache entry should never be fatal.
                cache_file.unlink(missing_ok=True)

        samples = ffmpeg.decode_mono(
            path,
            sample_rate=self.sample_rate,
            start=start,
            duration=duration,
            stream_index=stream_index,
        )
        envelope = onset_envelope(samples, self.sample_rate)

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(cache_file, envelope)
        except OSError:
            pass  # An unwritable cache directory only costs us speed.

        self._memory[key] = envelope
        return envelope

    def peaks(
        self,
        path: Path,
        *,
        start: float = 0.0,
        duration: Optional[float] = None,
        buckets: int = 600,
    ) -> np.ndarray:
        """Cached peak envelope for drawing a waveform."""
        key = self._key(path, start, duration, None) + f"-p{buckets}"
        if key in self._memory:
            return self._memory[key]

        cache_file = self.cache_dir / f"{key}.npy"
        if cache_file.exists():
            try:
                values = np.load(cache_file)
                self._memory[key] = values
                return values
            except (OSError, ValueError):
                cache_file.unlink(missing_ok=True)

        samples = ffmpeg.decode_mono(
            path, sample_rate=self.sample_rate, start=start, duration=duration
        )
        values = peak_envelope(samples, buckets)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(cache_file, values)
        except OSError:
            pass
        self._memory[key] = values
        return values

    def samples(
        self,
        path: Path,
        *,
        start: float = 0.0,
        duration: Optional[float] = None,
        stream_index: Optional[int] = None,
    ) -> np.ndarray:
        """Decode raw audio (uncached -- used only for short refinement windows)."""
        return ffmpeg.decode_mono(
            path,
            sample_rate=self.sample_rate,
            start=start,
            duration=duration,
            stream_index=stream_index,
        )
