# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-correlation and confidence scoring.

Offset convention throughout this module, stated exactly because a sign error
here would be invisible in the code and obvious in the edit suite:

    ``offset`` is the value satisfying ``a[t + offset] == b[t]``.

Equivalently, if both recordings capture one event at internal time ``t_a`` and
``t_b`` respectively, then ``offset == t_a - t_b``, and the two are aligned on a
timeline by placing b at ``position(a) + offset``.  So a recording that started
*rolling earlier* (more lead-in before the event) gets a negative offset.

Matching happens in two stages.  A normalised envelope correlation searches the
whole overlap space at 10 ms resolution and yields a confidence score; then, for
pairs that pass, a GCC-PHAT pass on raw audio refines the answer to well under a
sample.  The coarse stage cannot be skipped (raw-audio search over an hour of
audio is enormous), and the fine stage cannot be skipped either (10 ms is a
third of a frame at 29.97 fps, which is audible as an echo).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import signal

_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    """Outcome of matching two recordings."""

    offset_seconds: float
    """Place b at ``position(a) + offset_seconds`` to align the two."""

    confidence: float
    """Peak normalised correlation, in [-1, 1].  ~1 means identical shape."""

    peak_ratio: float
    """Peak height divided by the best competing peak elsewhere.

    This is what separates a real match from a periodic or noisy one: a true
    alignment has exactly one sharp peak, whereas a spurious match tends to
    produce several of similar height.
    """

    overlap_seconds: float
    """Length of the overlapping region at the winning offset."""


def _parabolic_peak(values: np.ndarray, index: int) -> float:
    """Sub-sample peak position by fitting a parabola through three points."""
    if index <= 0 or index >= values.size - 1:
        return float(index)
    left, centre, right = values[index - 1], values[index], values[index + 1]
    denominator = left - 2.0 * centre + right
    if abs(denominator) < _EPS:
        return float(index)
    return float(index) + 0.5 * (left - right) / denominator


def normalised_cross_correlation(
    a: np.ndarray,
    b: np.ndarray,
    min_overlap: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Correlation coefficient of a and b at every lag.

    Unlike a plain FFT correlation, each lag is normalised by the energy of the
    *actually overlapping* region.  Without that, long clips beat short ones on
    raw correlation magnitude regardless of how well they line up.

    Returns ``(lags, coefficients, overlaps)``; lags where the overlap is
    shorter than ``min_overlap`` are forced to zero.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    n, m = a.size, b.size
    if n == 0 or m == 0:
        empty = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int64), empty, empty

    numerator = signal.correlate(a, b, mode="full", method="fft")
    lags = np.arange(-(m - 1), n, dtype=np.int64)

    # Overlap bounds, expressed in b's index space, for every lag at once.
    lo_b = np.maximum(0, -lags)
    hi_b = np.minimum(m, n - lags)
    overlap = np.maximum(0, hi_b - lo_b)
    lo_a = lo_b + lags
    hi_a = hi_b + lags

    cumulative_a = np.concatenate(([0.0], np.cumsum(a * a)))
    cumulative_b = np.concatenate(([0.0], np.cumsum(b * b)))

    valid = overlap >= max(1, min_overlap)
    coefficients = np.zeros(lags.size, dtype=np.float64)
    if valid.any():
        energy_a = cumulative_a[np.clip(hi_a[valid], 0, n)] - cumulative_a[np.clip(lo_a[valid], 0, n)]
        energy_b = cumulative_b[np.clip(hi_b[valid], 0, m)] - cumulative_b[np.clip(lo_b[valid], 0, m)]
        denominator = np.sqrt(np.maximum(energy_a, 0.0) * np.maximum(energy_b, 0.0))
        safe = denominator > _EPS
        result = np.zeros(int(valid.sum()), dtype=np.float64)
        result[safe] = numerator[valid][safe] / denominator[safe]
        coefficients[valid] = result

    return lags, coefficients, overlap.astype(np.float64)


def match_envelopes(
    envelope_a: np.ndarray,
    envelope_b: np.ndarray,
    *,
    envelope_rate: int,
    min_overlap_seconds: float = 3.0,
    guard_seconds: float = 0.5,
) -> Optional[CorrelationResult]:
    """Find the offset between two onset envelopes.

    Returns ``None`` when the recordings are too short to overlap meaningfully.
    """
    min_overlap = int(round(min_overlap_seconds * envelope_rate))
    lags, coefficients, overlaps = normalised_cross_correlation(
        envelope_a, envelope_b, min_overlap
    )
    if lags.size == 0 or not np.any(coefficients):
        return None

    # Peak selection is done on a length-penalised score, not on the raw
    # coefficient.  A brief overlap can correlate *perfectly* by coincidence --
    # one clip's tail against another's head -- and would otherwise outrank a
    # long, slightly noisier, and obviously correct alignment.  The standard
    # error of a correlation estimate falls as 1/sqrt(N), so weighting by
    # sqrt(N) is what "how much evidence is behind this number" means.
    largest_overlap = float(overlaps.max()) if overlaps.size else 0.0
    if largest_overlap <= 0:
        return None
    score = coefficients * np.sqrt(overlaps / largest_overlap)

    peak_index = int(np.argmax(score))
    peak_value = float(coefficients[peak_index])

    # The runner-up is measured outside a guard band, so the shoulders of the
    # true peak are not mistaken for a competing hypothesis.
    guard = max(1, int(round(guard_seconds * envelope_rate)))
    masked = score.copy()
    masked[max(0, peak_index - guard): peak_index + guard + 1] = -np.inf
    finite = masked[np.isfinite(masked)]
    runner_up = float(np.max(finite)) if finite.size else 0.0
    peak_score = float(score[peak_index])
    peak_ratio = peak_score / max(runner_up, 1e-6) if peak_score > 0 else 0.0

    fractional = _parabolic_peak(coefficients, peak_index)
    offset_frames = fractional + float(lags[0])

    m, n = envelope_b.size, envelope_a.size
    lag = int(lags[peak_index])
    overlap = max(0, min(m, n - lag) - max(0, -lag))

    return CorrelationResult(
        offset_seconds=offset_frames / envelope_rate,
        confidence=peak_value,
        peak_ratio=peak_ratio,
        overlap_seconds=overlap / envelope_rate,
    )


def gcc_phat(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    max_lag_seconds: float = 0.05,
) -> tuple[float, float]:
    """Phase-transform cross-correlation of two roughly-aligned signals.

    PHAT whitening discards magnitude and keeps only phase, which makes the
    result depend on transient timing rather than on spectral content -- exactly
    the invariance we need across different microphones.

    Returns ``(residual_offset_seconds, peak_height)``.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    length = min(a.size, b.size)
    if length < 64:
        return 0.0, 0.0
    a, b = a[:length], b[:length]

    a = a - a.mean()
    b = b - b.mean()
    if np.max(np.abs(a)) < _EPS or np.max(np.abs(b)) < _EPS:
        return 0.0, 0.0

    n = int(1 << (2 * length - 1).bit_length())
    spectrum = np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n))
    spectrum /= np.abs(spectrum) + _EPS
    correlation = np.fft.irfft(spectrum, n)

    max_lag = min(int(round(max_lag_seconds * sample_rate)), n // 2 - 1)
    if max_lag < 1:
        return 0.0, 0.0

    # Rotate so index 0 of the window is lag -max_lag.
    window = np.concatenate((correlation[-max_lag:], correlation[: max_lag + 1]))
    peak_index = int(np.argmax(window))
    peak_height = float(window[peak_index])
    fractional = _parabolic_peak(window, peak_index)

    return (fractional - max_lag) / sample_rate, peak_height
