# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature extraction and all-pairs matching.

Produces the weighted graph the solver consumes: one node per sync unit, one
edge per accepted pairwise offset.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..audio import correlate
from ..audio.features import ENVELOPE_RATE, FeatureCache
from ..model import SyncUnit

log = logging.getLogger(__name__)

#: Never refine over more than this much audio -- GCC-PHAT converges long before.
_REFINE_WINDOW_SECONDS = 20.0

#: Envelope correlation resolves to 10 ms; allow refinement a few times that.
_REFINE_MAX_LAG_SECONDS = 0.05


@dataclass(slots=True)
class UnitFeature:
    """A sync unit's audio, reduced to something correlatable."""

    index: int
    unit: SyncUnit
    path: Optional[Path]
    source_offset: float
    """Seconds into the media file where this clip's used range begins."""
    duration: Optional[float]
    envelope: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    error: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.envelope.size > 0


#: What a relationship we were *told* is worth, next to one we measured.
#:
#: Least-squares weights should go as 1/sigma^2, so the question is how
#: accurate each kind is. Measured against the ActionCheese app's own shoot
#: index on two real shoots, its clip starts are good to about 0.1 s, while
#: audio correlation between the same clips agrees with itself to about
#: 0.01 s -- and where the two disagreed, the audio was self-consistent across
#: independent pairs and the index was not. A stated time is therefore a
#: *prior*: worth about as much as one just-acceptable audio match (0.45
#: confidence over the 20 s minimum overlap gives 4), enough to place a clip
#: nothing else can reach, not enough to overrule material evidence.
PRIOR_WEIGHT = 5.0

#: How much of the shorter clip two short clips must share before their
#: correlation is taken seriously. Two takes of the same moment overlap almost
#: completely -- on the example shoot, 16.6 s of a 16.8 s take, 99% -- while
#: the coincidental pairings on the same material overlapped 21% and scored
#: below the confidence floor anyway. Demanding most of the shorter clip is
#: the same test as "20 seconds of common audio", expressed in a way that a
#: 17 s take can actually satisfy.
OVERLAP_FRACTION = 0.8

#: Below this, no overlap fraction rescues a pair: a handful of seconds of
#: envelope produces a confident-looking coefficient from nothing but room
#: tone, and there is no threshold that separates those from real matches.
OVERLAP_FLOOR_SECONDS = 6.0

#: A match can be rescued below the confidence floor when its peak is this
#: much higher than the best competing alignment, over at least
#: :data:`RESCUE_OVERLAP_SECONDS` of common material.
#:
#: The coefficient says how *alike* two recordings are; the peak ratio says how
#: much better this alignment is than every other alignment of the same two.
#: They are different questions, and for a camera with poor on-board audio in a
#: large room the first answer is low even when the second is overwhelming.
#:
#: Measured against a hand-synced four-camera shoot: of the pairs sharing five
#: minutes or more, every *wrong* one had a peak ratio of at most 1.01 -- no
#: better than the runner-up -- while every correct one was at least 4.9. The
#: clip this rescues scored 0.194, far below the 0.45 floor, yet its peak stood
#: 17 times above the next and placed the clip within one frame of where it was
#: put by hand. Left out, that camera's only clip sat 11 minutes adrift.
#:
#: The threshold sits in the middle of that gap, with roughly equal margin
#: either side. This is a *rescue*, not a gate: the peak ratio is near useless
#: as a requirement on reverberant material, where a 1.4 threshold once
#: rejected 9 of 13 correct matches.
RESCUE_PEAK_RATIO = 3.0
RESCUE_OVERLAP_SECONDS = 300.0

#: Far above anything audio can reach, for ``--trust-synced``: the operator has
#: said the stated times are authoritative, so they win every disagreement.
#: Still finite, so the least-squares system stays well behaved; the exact
#: geometry is restored afterwards by
#: :func:`syncmyvids.sync.solve.snap_rigid`.
RIGID_WEIGHT = 1.0e7


def _source_name(feature: "UnitFeature") -> Optional[str]:
    """The media file a unit came from, for joining across timelines."""
    media = feature.unit.primary_clip.file
    return media.path.name if media is not None and media.path is not None else None


def is_distinctive(peak_ratio: float, overlap_seconds: float) -> bool:
    """Is this alignment so much better than every other that it stands alone?

    The rescue test, kept separate from the matching loop so it can be stated
    and checked on its own. See :data:`RESCUE_PEAK_RATIO`.
    """
    return (peak_ratio >= RESCUE_PEAK_RATIO
            and overlap_seconds >= RESCUE_OVERLAP_SECONDS)


@dataclass(frozen=True, slots=True)
class Edge:
    """An accepted pairwise alignment between two units."""

    a: int
    b: int
    delta_seconds: float
    """Required ``start(b) - start(a)`` on the timeline, in seconds."""
    confidence: float
    peak_ratio: float
    overlap_seconds: float
    refined: bool = False
    distinctive: bool = False
    """Accepted on the strength of an isolated peak over a long overlap rather
    than on its coefficient. See :data:`RESCUE_PEAK_RATIO`."""
    kind: str = "audio"
    """``"audio"`` for something we measured; ``"prior"`` for a relationship we
    were told, such as two clips the ActionCheese app recorded against one
    shared clock; ``"rigid"`` for a told relationship the operator has declared
    authoritative."""

    @property
    def rigid(self) -> bool:
        return self.kind == "rigid"

    @property
    def stated(self) -> bool:
        """Told to us rather than measured, whether or not it is authoritative."""
        return self.kind in ("prior", "rigid")

    @property
    def weight(self) -> float:
        """Least-squares weight.

        For a measured match: confidence squared, scaled by overlap, so a match
        backed by ten minutes of common material has more say than one backed
        by four seconds. For a stated one, a fixed weight reflecting how
        accurate the source of the statement is -- see PRIOR_WEIGHT.
        """
        if self.kind == "rigid":
            return RIGID_WEIGHT
        if self.kind == "prior":
            return PRIOR_WEIGHT
        return max(self.confidence, 0.0) ** 2 * min(self.overlap_seconds, 300.0)


def extract_features(
    units: Iterable[SyncUnit],
    cache: FeatureCache,
    *,
    jobs: int = 4,
    progress=None,
) -> list[UnitFeature]:
    """Decode and reduce every unit's audio, in parallel."""
    features: list[UnitFeature] = []
    for index, unit in enumerate(units):
        clip = unit.primary_clip
        media = clip.file
        offset, duration = clip.source_span_seconds()
        if duration <= 0:
            # Some exports leave in/out unset; fall back to the whole file.
            offset, duration = 0.0, None

        if media is None or media.path is None:
            features.append(UnitFeature(index, unit, None, offset, duration,
                                        error="clip has no media file reference"))
        elif not media.path.exists():
            features.append(UnitFeature(index, unit, media.path, offset, duration,
                                        error=f"media not found: {media.path}"))
        else:
            features.append(UnitFeature(index, unit, media.path, offset, duration))

    pending = [f for f in features if f.error is None]

    def load(feature: UnitFeature) -> None:
        try:
            feature.envelope = cache.envelope(
                feature.path,  # type: ignore[arg-type]
                start=feature.source_offset,
                duration=feature.duration,
            )
            if feature.envelope.size == 0:
                feature.error = "no usable audio"
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
            feature.error = f"{type(exc).__name__}: {exc}"

    if pending:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for _ in pool.map(load, pending):
                done += 1
                if progress is not None:
                    progress(done / len(pending))

    return features


def _refine(
    cache: FeatureCache,
    a: UnitFeature,
    b: UnitFeature,
    delta: float,
) -> tuple[float, bool]:
    """Sharpen a coarse offset with GCC-PHAT on raw audio.

    ``delta`` is expressed in the units' own internal time: b's internal time
    ``t`` corresponds to a's internal time ``t + delta``.
    """
    duration_a = a.duration if a.duration is not None else 0.0
    duration_b = b.duration if b.duration is not None else 0.0
    if duration_a <= 0 or duration_b <= 0:
        return delta, False

    # Overlapping span, in a's internal time.
    overlap_start = max(0.0, delta)
    overlap_end = min(duration_a, delta + duration_b)
    if overlap_end - overlap_start < 2.0:
        return delta, False

    window = min(_REFINE_WINDOW_SECONDS, overlap_end - overlap_start)
    centre = (overlap_start + overlap_end) / 2.0
    start_a = max(overlap_start, centre - window / 2.0)
    start_b = start_a - delta

    try:
        samples_a = cache.samples(a.path, start=a.source_offset + start_a, duration=window)  # type: ignore[arg-type]
        samples_b = cache.samples(b.path, start=b.source_offset + start_b, duration=window)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        log.debug("refinement decode failed for %s/%s: %s", a.unit.label, b.unit.label, exc)
        return delta, False

    residual, peak = correlate.gcc_phat(
        samples_a, samples_b,
        sample_rate=cache.sample_rate,
        max_lag_seconds=_REFINE_MAX_LAG_SECONDS,
    )
    if peak <= 0.0 or abs(residual) >= _REFINE_MAX_LAG_SECONDS * 0.99:
        # A residual pinned to the search limit means the coarse offset was not
        # close enough for PHAT to be meaningful; keep the coarse answer.
        return delta, False
    return delta + residual, True


def match_pairs(
    features: list[UnitFeature],
    cache: FeatureCache,
    *,
    min_confidence: float = 0.45,
    min_peak_ratio: float = 1.0,
    min_overlap_seconds: float = 20.0,
    refine: bool = True,
    progress=None,
) -> tuple[list[Edge], list[dict]]:
    """Correlate every usable pair of units.

    Returns accepted edges plus a record of every pair considered, which the
    report uses to explain *why* a clip failed to sync.

    ``min_overlap_seconds`` is the other half of acceptance, and it matters as
    much as the confidence floor.  A correlation coefficient computed from a few
    seconds of envelope is close to meaningless -- on a real four-camera project
    a pair of clips that never overlapped in time scored 0.69 on six seconds of
    common audio, higher than any genuinely correct match in that project.
    Measured against hand-verified ground truth on another project, *every*
    correct match rested on at least 24 seconds of overlap, so requiring 20
    discards the coincidences without losing a single true match.

    Acceptance rests on the correlation coefficient, with the peak-to-runner-up
    ratio recorded but effectively off by default.  That ratio looked like a
    good discriminator on synthetic transient-rich audio, where a true match
    gives one isolated peak.  Real reverberant material -- a church, music, a
    room full of people -- produces a *spiky* correlation with many local maxima
    close to the peak's height, so the ratio carries almost no signal: measured
    against timestamp ground truth on a real 18-clip project, a 1.4 threshold
    rejected 9 of 13 correct matches while admitting incorrect ones scoring
    higher on the same ratio.  Confidence alone, at a higher floor, separated
    those 13 cleanly.

    That 20 s is an absolute floor for long material, but it cannot be applied
    literally: two clips can never overlap by more than the shorter of them, so
    on a shoot of short takes -- a 17 s take covered by a camera that rolled for
    21 s -- it rejects every pair before a single one is scored, and nothing
    syncs at all. What the rule is really asking for is *most of the available
    material*, so for a short pair the requirement becomes a fraction of the
    shorter clip instead, with a hard floor below which a coefficient means
    nothing whatever it says. See :data:`OVERLAP_FRACTION`.
    """
    usable = [f for f in features if f.usable]
    edges: list[Edge] = []
    attempts: list[dict] = []

    total_pairs = max(1, len(usable) * (len(usable) - 1) // 2)
    seen = 0
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            seen += 1
            if progress is not None and seen % 16 == 0:
                progress(seen / total_pairs)
            if a.unit.source_tracks & b.unit.source_tracks:
                # Same track means same device, and one device cannot record the
                # same moment twice -- so two clips sharing a track can never
                # legitimately overlap in time and must never be matched.
                #
                # This is not a mere optimisation.  On real footage, two
                # sequential takes from one camera correlate spuriously (0.50 on
                # the test project, from shared room tone and music), and because
                # the solver has no way to know the edge is impossible it
                # averages that false constraint into the true chain: two clips
                # whose correct separation was 166 s were placed 151 s apart.
                continue

            shorter = min(len(a.envelope), len(b.envelope)) / ENVELOPE_RATE
            required = min(min_overlap_seconds, OVERLAP_FRACTION * shorter)
            if required < OVERLAP_FLOOR_SECONDS:
                required = OVERLAP_FLOOR_SECONDS

            result = correlate.match_envelopes(
                a.envelope, b.envelope,
                envelope_rate=ENVELOPE_RATE,
                min_overlap_seconds=required,
            )
            if result is None:
                continue

            distinctive = is_distinctive(result.peak_ratio, result.overlap_seconds)
            accepted = distinctive or (
                result.confidence >= min_confidence
                and result.peak_ratio >= min_peak_ratio
            )
            attempts.append({
                "a": a.unit.label,
                "b": b.unit.label,
                # The files, so an attempt can be matched against a timeline
                # someone else produced. Labels carry a track number, and
                # tracks move.
                "a_file": _source_name(a),
                "b_file": _source_name(b),
                "confidence": round(result.confidence, 4),
                "peak_ratio": round(result.peak_ratio, 3),
                "overlap_seconds": round(result.overlap_seconds, 2),
                # Where this pairing would have put b relative to a. Recorded
                # even when rejected: against a known-good timeline it is the
                # only way to tell a match that was wrong from one that was
                # right and thrown away.
                "delta_seconds": round(result.offset_seconds, 4),
                "accepted": accepted,
            })
            if not accepted:
                continue

            delta = result.offset_seconds
            refined = False
            if refine:
                delta, refined = _refine(cache, a, b, delta)

            # Each envelope starts at its clip's in-point, so envelope time 0 is
            # that clip's timeline start.  The correlation offset is therefore
            # already the required difference between the two start positions --
            # no conversion needed, which is exactly why we window the audio to
            # the clip's used range rather than correlating whole files.
            edges.append(Edge(
                a=a.index,
                b=b.index,
                delta_seconds=delta,
                confidence=result.confidence,
                peak_ratio=result.peak_ratio,
                overlap_seconds=result.overlap_seconds,
                refined=refined,
                distinctive=distinctive,
            ))

    return edges, attempts
