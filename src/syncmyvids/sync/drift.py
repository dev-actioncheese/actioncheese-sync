# SPDX-License-Identifier: GPL-3.0-or-later
"""Clock-drift estimation.

Independent recorders do not share a clock.  A field recorder sampling at a
nominal 48 kHz might really run at 48000.9 Hz, which is inaudible at the head of
a take and a very audible echo forty minutes in.  A single best-fit offset
cannot express that -- it splits the error, leaving both ends slightly wrong.

Method: measure the offset in several fixed-length windows spread across the
clip and fit a line through them.  The slope is the drift rate.

Two details matter for this to work at all:

* **Windows are matched on onset envelopes, not raw waveforms.**  Raw
  cross-correlation between two different microphones is multi-peaked -- on the
  test footage the true peak scores 0.41 while a competing peak scores 0.31 --
  so a per-window raw match jumps between peaks and the resulting slope is
  noise.  Envelopes score ~0.85 with a single clear peak.
* **The envelope is computed at a higher rate here than for matching.**  Drift
  is a millisecond-scale effect measured over minutes; at the 100 Hz rate used
  for coarse matching the quantisation alone costs about a quarter of the
  answer.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..audio import correlate
from ..audio.features import FeatureCache, onset_envelope
from .graph import Edge, UnitFeature

log = logging.getLogger(__name__)

#: Length of each probe.  Long enough for a confident envelope peak, short
#: enough that drift within one probe stays far below its resolution.
DEFAULT_WINDOW_SECONDS = 60.0

#: Number of probes spread across the overlap.  Three would suffice for a line;
#: five gives the fit enough redundancy to expose a bad probe through its
#: residual.
DEFAULT_PROBE_COUNT = 5

#: Below this, head and tail probes are too close for the slope to mean anything.
DEFAULT_MIN_DURATION_SECONDS = 300.0

#: Envelope resolution used for probes -- four times the matching rate.
PROBE_ENVELOPE_RATE = 400

#: A probe correlating worse than this is dropped from the fit.
MIN_PROBE_CONFIDENCE = 0.30

#: If the fitted line misses its own probes by more than this, the measurement
#: is not trustworthy and no correction is written.
MAX_FIT_RMS_SECONDS = 0.010

#: Minimum spread between the first and last probe.  Drift is a slope, so its
#: precision depends on the lever arm, not on how neatly the line fits.  A real
#: project measured -33, +13, -7 and +27 ppm for four takes off *one* recorder
#: -- physically impossible for a single clock -- because each probe set spanned
#: only the two-to-six minutes a camera clip happened to overlap.
MIN_BASELINE_SECONDS = 300.0

#: How many standard errors the slope must exceed to count as real drift.
DRIFT_SIGNIFICANCE = 3.0


@dataclass(slots=True)
class DriftEstimate:
    """How fast one clip's clock ran relative to its reference peer."""

    unit_index: int
    peer_index: Optional[int]
    drift_ppm: float
    """Positive means this recorder ran fast (captured more time than real)."""
    speed_percent: float
    """Playback speed that cancels the drift; 100.0 means no change."""
    span_seconds: float
    measured: bool
    probe_count: int = 0
    fit_rms_seconds: float = 0.0
    reason: Optional[str] = None

    @property
    def total_error_seconds(self) -> float:
        """How far the ends would diverge if the drift were left uncorrected."""
        return abs(self.drift_ppm) * 1e-6 * self.span_seconds


def _probe(
    cache: FeatureCache,
    peer: UnitFeature,
    target: UnitFeature,
    delta: float,
    target_time: float,
    window: float,
) -> Optional[tuple[float, float]]:
    """Offset and confidence measured in one window at ``target_time``.

    ``delta`` is the current global offset, so the peer is read from
    ``target_time + delta``; the returned offset is the *residual* still needed
    at this point in the clip.
    """
    try:
        peer_samples = cache.samples(
            peer.path, start=peer.source_offset + target_time + delta, duration=window  # type: ignore[arg-type]
        )
        target_samples = cache.samples(
            target.path, start=target.source_offset + target_time, duration=window  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("drift probe decode failed: %s", exc)
        return None

    minimum = cache.sample_rate * 5
    if peer_samples.size < minimum or target_samples.size < minimum:
        return None

    result = correlate.match_envelopes(
        onset_envelope(peer_samples, cache.sample_rate, PROBE_ENVELOPE_RATE),
        onset_envelope(target_samples, cache.sample_rate, PROBE_ENVELOPE_RATE),
        envelope_rate=PROBE_ENVELOPE_RATE,
        min_overlap_seconds=min(5.0, window / 4),
        guard_seconds=0.3,
    )
    if result is None or result.confidence < MIN_PROBE_CONFIDENCE:
        return None
    return result.offset_seconds, result.confidence


def _fit_line(
    times: np.ndarray, offsets: np.ndarray, weights: np.ndarray
) -> tuple[float, float, float]:
    """Weighted least-squares line fit.

    Returns ``(slope, residual RMS, standard error of the slope)``.  The
    standard error is what tells a real drift from a plausible-looking line
    through noisy points: it grows as the probes bunch together, which is
    exactly when a tight-looking fit stops meaning anything.
    """
    design = np.vstack([times, np.ones_like(times)]).T
    weighted = design * weights[:, None]
    solution = np.linalg.lstsq(weighted.T @ design, weighted.T @ offsets, rcond=None)[0]
    residuals = offsets - design @ solution
    rms = float(np.sqrt(np.mean(residuals ** 2)))

    spread = float(np.sum((times - times.mean()) ** 2))
    degrees = max(1, times.size - 2)
    residual_sigma = float(np.sqrt(np.sum(residuals ** 2) / degrees))
    stderr = residual_sigma / np.sqrt(spread) if spread > 0 else float("inf")
    return float(solution[0]), rms, stderr


def estimate_drift(
    features: list[UnitFeature],
    positions: dict[int, float],
    edges: list[Edge],
    cache: FeatureCache,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    probe_count: int = DEFAULT_PROBE_COUNT,
    min_duration_seconds: float = DEFAULT_MIN_DURATION_SECONDS,
    reference_key: Optional[tuple[str, int]] = None,
    jobs: int = 4,
) -> dict[int, DriftEstimate]:
    """Estimate drift for every synced unit long enough to measure.

    Drift is relative, so it is only meaningful against a stated baseline.  When
    ``reference_key`` names a track, peers on that track are preferred, making
    every number in the report "drift against the reference device" rather than
    "drift against whichever clip happened to match best".
    """

    def on_reference(node: int) -> bool:
        if reference_key is None:
            return False
        clip = features[node].unit.primary_clip
        return (clip.media_type, clip.track_index) == reference_key

    # Best peer overall, and best peer that sits on the reference track.
    best_peer: dict[int, tuple[float, int]] = {}
    best_reference_peer: dict[int, tuple[float, int]] = {}
    for edge in edges:
        if edge.a not in positions or edge.b not in positions:
            continue
        for node, peer in ((edge.a, edge.b), (edge.b, edge.a)):
            current = best_peer.get(node)
            if current is None or edge.weight > current[0]:
                best_peer[node] = (edge.weight, peer)
            if on_reference(peer):
                current = best_reference_peer.get(node)
                if current is None or edge.weight > current[0]:
                    best_reference_peer[node] = (edge.weight, peer)

    return {
        index: _estimate_one(
            index,
            features,
            positions,
            cache,
            peer_entry=best_reference_peer.get(index) or best_peer.get(index),
            is_reference=on_reference(index),
            window_seconds=window_seconds,
            probe_count=probe_count,
            min_duration_seconds=min_duration_seconds,
            jobs=jobs,
        )
        for index in sorted(positions)
    }


def _estimate_one(
    index: int,
    features: list[UnitFeature],
    positions: dict[int, float],
    cache: FeatureCache,
    *,
    peer_entry: Optional[tuple[float, int]],
    is_reference: bool,
    window_seconds: float,
    probe_count: int,
    min_duration_seconds: float,
    jobs: int,
) -> DriftEstimate:
    """Measure one unit's drift against its chosen peer."""
    feature = features[index]
    duration = feature.duration or 0.0
    peer_index = peer_entry[1] if peer_entry else None

    def unmeasured(reason: str) -> DriftEstimate:
        return DriftEstimate(
            unit_index=index,
            peer_index=peer_index,
            drift_ppm=0.0,
            speed_percent=100.0,
            span_seconds=duration,
            measured=False,
            reason=reason,
        )

    if is_reference:
        return unmeasured("reference device -- drift is measured against this")
    if duration < min_duration_seconds:
        return unmeasured(f"clip shorter than {min_duration_seconds:.0f}s")
    if peer_entry is None or peer_index is None:
        return unmeasured("no matched peer to measure against")

    peer = features[peer_index]
    peer_duration = peer.duration or 0.0
    if peer.path is None or feature.path is None or peer_duration <= 0:
        return unmeasured("peer has no usable media")

    delta = positions[index] - positions[peer_index]
    # Overlapping span in the target's internal time.  Clamping at zero matters:
    # ffmpeg silently clamps a negative seek to the start of the file, which
    # would return the wrong audio rather than an error.
    overlap_start = max(0.0, -delta)
    overlap_end = min(duration, peer_duration - delta)
    if overlap_end - overlap_start < 3.0 * window_seconds:
        return unmeasured("overlap too short to spread drift probes across")

    probe_times = np.linspace(
        overlap_start, overlap_end - window_seconds, max(3, probe_count)
    )

    def run(target_time: float) -> Optional[tuple[float, float]]:
        return _probe(cache, peer, feature, delta, target_time, window_seconds)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(run, (float(t) for t in probe_times)))

    samples = [
        (float(t) + window_seconds / 2.0, result[0], result[1])
        for t, result in zip(probe_times, results, strict=True)
        if result is not None
    ]
    if len(samples) < 3:
        return unmeasured("too few probes correlated confidently")

    times = np.array([s[0] for s in samples])
    offsets = np.array([s[1] for s in samples])
    weights = np.array([s[2] for s in samples]) ** 2

    baseline = float(times[-1] - times[0])
    if baseline < MIN_BASELINE_SECONDS:
        return unmeasured(
            f"probe baseline only {baseline:.0f}s; need {MIN_BASELINE_SECONDS:.0f}s "
            "of separation to resolve drift"
        )

    slope, rms, stderr = _fit_line(times, offsets, weights)
    if rms > MAX_FIT_RMS_SECONDS:
        return unmeasured(f"drift fit inconsistent ({rms * 1000:.1f} ms residual)")
    if abs(slope) >= 0.5:
        return unmeasured("implausible drift slope")
    if abs(slope) < DRIFT_SIGNIFICANCE * stderr:
        return unmeasured(
            f"drift not distinguishable from noise "
            f"({abs(slope) * 1e6:.1f} ppm vs {stderr * 1e6:.1f} ppm uncertainty)"
        )

    # slope is d(offset)/d(target time).  A recorder running fast makes the
    # required offset shrink, so the clock ratio is 1/(1+slope).
    clock_ratio = 1.0 / (1.0 + slope)

    return DriftEstimate(
        unit_index=index,
        peer_index=peer_index,
        drift_ppm=(clock_ratio - 1.0) * 1e6,
        speed_percent=clock_ratio * 100.0,
        span_seconds=duration,
        measured=True,
        probe_count=len(samples),
        fit_rms_seconds=rms,
    )
