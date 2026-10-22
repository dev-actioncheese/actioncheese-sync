# SPDX-License-Identifier: GPL-3.0-or-later
"""Correlation behaviour on signals whose true offset is known exactly."""

from __future__ import annotations

import numpy as np
import pytest

from syncmyvids.audio.correlate import (
    gcc_phat,
    match_envelopes,
    normalised_cross_correlation,
)
from syncmyvids.audio.features import ENVELOPE_RATE, onset_envelope

SAMPLE_RATE = 8000


def transient_signal(seconds: float, seed: int = 11) -> np.ndarray:
    """Broadband, transient-rich audio -- the kind sync actually keys off."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    audio = rng.normal(0.0, 0.01, n).astype(np.float32)
    for onset in rng.uniform(0.0, seconds - 0.5, int(seconds * 4)):
        index = int(onset * SAMPLE_RATE)
        length = int(0.08 * SAMPLE_RATE)
        envelope = np.exp(-np.linspace(0.0, 8.0, length))
        tone = np.sin(2 * np.pi * rng.uniform(150, 3000) * np.arange(length) / SAMPLE_RATE)
        audio[index:index + length] += (envelope * tone).astype(np.float32)
    return audio


class TestSignConvention:
    """``offset`` must satisfy ``a[t + offset] == b[t]``.

    A sign error here is invisible in the code and catastrophic in the edit, so
    both stages are pinned against the same explicit definition.
    """

    @pytest.mark.parametrize("shift", [37, -91, 5, 0])
    def test_gcc_phat(self, shift: int) -> None:
        rng = np.random.default_rng(3)
        a = rng.normal(0, 1, SAMPLE_RATE * 20).astype(np.float32)
        b = np.roll(a, -shift)  # b[t] == a[t + shift]
        offset, peak = gcc_phat(a[2000:-2000], b[2000:-2000],
                                sample_rate=SAMPLE_RATE, max_lag_seconds=0.05)
        assert offset == pytest.approx(shift / SAMPLE_RATE, abs=1e-6)
        assert peak > 0.5

    @pytest.mark.parametrize("shift_seconds", [1.5, -2.25])
    def test_envelope_matches_gcc_phat_convention(self, shift_seconds: float) -> None:
        audio = transient_signal(40.0)
        shifted = np.roll(audio, -int(shift_seconds * SAMPLE_RATE))
        result = match_envelopes(
            onset_envelope(audio, SAMPLE_RATE),
            onset_envelope(shifted, SAMPLE_RATE),
            envelope_rate=ENVELOPE_RATE,
        )
        assert result is not None
        assert result.offset_seconds == pytest.approx(shift_seconds, abs=0.02)


class TestMatchEnvelopes:
    def test_finds_offset_between_trimmed_windows(self) -> None:
        # The realistic case: two recorders covering overlapping windows of the
        # same event, so neither signal is a shifted copy of the other.
        master = transient_signal(120.0)
        a = master[int(10 * SAMPLE_RATE):int(100 * SAMPLE_RATE)]
        b = master[int(30 * SAMPLE_RATE):int(115 * SAMPLE_RATE)]
        result = match_envelopes(
            onset_envelope(a, SAMPLE_RATE), onset_envelope(b, SAMPLE_RATE),
            envelope_rate=ENVELOPE_RATE,
        )
        assert result is not None
        # b starts 20 s later in the master, so b belongs 20 s after a.
        assert result.offset_seconds == pytest.approx(20.0, abs=0.05)
        assert result.confidence > 0.6
        assert result.overlap_seconds == pytest.approx(70.0, abs=1.0)

    def test_survives_different_microphones(self) -> None:
        from scipy import signal as sg

        master = transient_signal(90.0)
        rng = np.random.default_rng(5)
        low_b, low_a = sg.butter(4, 3000 / (SAMPLE_RATE / 2), "low")
        quiet = (sg.lfilter(low_b, low_a, master) * 0.3
                 + rng.normal(0, 0.02, master.size)).astype(np.float32)
        # Pad rather than roll: np.roll wraps, which creates a second, equally
        # valid alignment and tests something other than what we mean.
        pad = int(7.5 * SAMPLE_RATE)
        shifted = np.concatenate([np.zeros(pad, dtype=np.float32), quiet])

        result = match_envelopes(
            onset_envelope(master, SAMPLE_RATE), onset_envelope(shifted, SAMPLE_RATE),
            envelope_rate=ENVELOPE_RATE,
        )
        assert result is not None
        assert result.offset_seconds == pytest.approx(-7.5, abs=0.05)
        assert result.confidence > 0.5

    def test_unrelated_audio_is_rejected(self) -> None:
        rng = np.random.default_rng(99)
        a = transient_signal(60.0, seed=1)
        b = rng.normal(0, 0.1, SAMPLE_RATE * 60).astype(np.float32)
        result = match_envelopes(
            onset_envelope(a, SAMPLE_RATE), onset_envelope(b, SAMPLE_RATE),
            envelope_rate=ENVELOPE_RATE,
        )
        assert result is not None
        # Must fall below the shipped acceptance thresholds on both counts.
        assert result.confidence < 0.35 or result.peak_ratio < 1.4

    def test_returns_none_for_empty(self) -> None:
        empty = np.zeros(0, dtype=np.float32)
        assert match_envelopes(empty, empty, envelope_rate=ENVELOPE_RATE) is None


class TestSameTrackExclusion:
    """Two clips on one track are one device and can never overlap in time.

    Found on a real project: two sequential takes from the same camera
    correlated at 0.50 purely from shared room tone and music.  The solver has
    no way to know such an edge is impossible, so it averaged that false
    constraint into the true chain and placed two clips 151 s apart whose
    correct separation was 166 s.
    """

    def _feature(self, label, media_type, track_index, envelope):
        from syncmyvids.model import Clip, SyncUnit
        from syncmyvids.sync.graph import UnitFeature
        from syncmyvids.timebase import Rate

        clip = Clip(clip_id=label, name=label, media_type=media_type,
                    track_index=track_index, start=0, end=100, in_point=0,
                    out_point=100, rate=Rate(25, False), file=None)
        unit = SyncUnit(key=label, clips=[clip])
        feature = UnitFeature(index=0, unit=unit, path=None,
                              source_offset=0.0, duration=60.0)
        feature.envelope = envelope
        return feature

    def test_same_track_pairs_are_never_matched(self) -> None:
        from syncmyvids.audio.features import FeatureCache
        from syncmyvids.sync.graph import match_pairs

        # Identical audio: correlation would be perfect if it were allowed.
        envelope = onset_envelope(transient_signal(60.0), SAMPLE_RATE)
        same = [self._feature("a", "video", 1, envelope),
                self._feature("b", "video", 1, envelope)]
        for i, f in enumerate(same):
            f.index = i
        edges, attempts = match_pairs(same, FeatureCache(), refine=False)
        assert edges == []
        assert attempts == []

    def test_different_tracks_still_match(self) -> None:
        from syncmyvids.audio.features import FeatureCache
        from syncmyvids.sync.graph import match_pairs

        envelope = onset_envelope(transient_signal(60.0), SAMPLE_RATE)
        pair = [self._feature("a", "video", 1, envelope),
                self._feature("b", "video", 2, envelope)]
        for i, f in enumerate(pair):
            f.index = i
        edges, _ = match_pairs(pair, FeatureCache(), refine=False)
        assert len(edges) == 1


class TestCorroboration:
    """A clip cannot be placed where the same device is already recording."""

    def _setup(self, weak_delta: float):
        from syncmyvids.model import Clip, SyncUnit
        from syncmyvids.sync.graph import Edge, UnitFeature
        from syncmyvids.sync.solve import Component, Solution
        from syncmyvids.timebase import Rate

        def feature(index, label, media_type, track, duration):
            clip = Clip(clip_id=label, name=label, media_type=media_type,
                        track_index=track, start=0, end=100, in_point=0,
                        out_point=100, rate=Rate(25, False), file=None)
            f = UnitFeature(index=index, unit=SyncUnit(key=label, clips=[clip]),
                            path=None, source_offset=0.0, duration=duration)
            return f

        # 0 and 1 are already placed: a recorder and a camera clip on V1.
        features = [feature(0, "rec", "audio", 3, 600.0),
                    feature(1, "camA", "video", 1, 100.0),
                    feature(2, "camB", "video", 1, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 200.0})],
            conflicts=[], singletons=[2],
        )
        # Weak candidate placing camB relative to the recorder.
        edge = Edge(a=0, b=2, delta_seconds=weak_delta, confidence=0.35,
                    peak_ratio=1.0, overlap_seconds=100.0)
        return features, solution, [edge]

    def test_colliding_candidate_is_rejected(self) -> None:
        from syncmyvids.sync.corroborate import corroborate

        # camB would land at 250s, inside camA's 200-300s span on the same track.
        features, solution, edges = self._setup(weak_delta=250.0)
        accepted, decisions = corroborate(features, solution, edges, min_confidence=0.45)
        assert accepted == []
        assert "overlap" in decisions[0]["rejected_because"]

    def test_clear_candidate_is_admitted(self) -> None:
        from syncmyvids.sync.corroborate import corroborate

        # camB lands at 400s, well clear of camA.
        features, solution, edges = self._setup(weak_delta=400.0)
        accepted, decisions = corroborate(features, solution, edges, min_confidence=0.45)
        assert len(accepted) == 1
        assert decisions[0]["accepted"]

    def test_strong_edges_are_left_alone(self) -> None:
        from syncmyvids.sync.corroborate import corroborate

        features, solution, edges = self._setup(weak_delta=400.0)
        # Already above the main floor, so not this pass's business.
        accepted, _ = corroborate(features, solution, edges, min_confidence=0.30)
        assert accepted == []


class TestNormalisation:
    def test_short_overlap_is_suppressed(self) -> None:
        """A two-sample overlap can correlate perfectly and mean nothing."""
        a = np.array([0, 0, 0, 1, 2, 3, 2, 1, 0, 0], dtype=np.float64)
        b = np.array([1, 2, 3, 2, 1], dtype=np.float64)
        lags, coefficients, overlaps = normalised_cross_correlation(a, b, min_overlap=5)
        best = lags[int(np.argmax(coefficients))]
        assert best == 3
        # Lags leaving fewer than min_overlap samples in common score zero.
        assert coefficients[0] == 0.0
        assert coefficients[-1] == 0.0
        assert overlaps[int(np.argmax(coefficients))] == 5

    def test_coefficient_is_bounded(self) -> None:
        rng = np.random.default_rng(4)
        a = rng.normal(0, 1, 500)
        lags, coefficients, _ = normalised_cross_correlation(a, a, min_overlap=50)
        assert coefficients.max() == pytest.approx(1.0, abs=1e-9)
        assert lags[int(np.argmax(coefficients))] == 0

    def test_long_good_overlap_beats_short_perfect_one(self) -> None:
        """The failure mode the length penalty exists to prevent."""
        rng = np.random.default_rng(17)
        shared = transient_signal(60.0, seed=2)
        noisy = (shared + rng.normal(0, 0.05, shared.size)).astype(np.float32)
        # Append a short, *exact* copy of the head so a brief perfect alignment
        # competes with the long imperfect one.
        decoy = np.concatenate([noisy, shared[: int(4 * SAMPLE_RATE)]]).astype(np.float32)

        result = match_envelopes(
            onset_envelope(shared, SAMPLE_RATE), onset_envelope(decoy, SAMPLE_RATE),
            envelope_rate=ENVELOPE_RATE,
        )
        assert result is not None
        assert result.offset_seconds == pytest.approx(0.0, abs=0.05)


class TestRescuingADistinctiveMatch:
    """A low coefficient is not the same thing as a doubtful alignment.

    The coefficient says how alike two recordings are; the peak ratio says how
    much better this alignment is than every other alignment of the same two.
    A camera with poor on-board audio in a big room scores badly on the first
    while being unambiguous on the second.

    The numbers come from a hand-synced four-camera shoot. Among pairs sharing
    five minutes or more, every wrong one peaked at most 1.01 above its
    runner-up and every correct one at least 4.9.
    """

    def test_an_isolated_peak_over_a_long_overlap_is_kept(self) -> None:
        from syncmyvids.sync.graph import is_distinctive

        # The real case: coefficient 0.194, far below the 0.45 floor, but the
        # peak stood 17 times above the next over 42 minutes of common audio,
        # and placed the clip within one frame of the hand sync.
        assert is_distinctive(17.24, 2527.5)

    def test_a_peak_no_better_than_its_runner_up_is_not(self) -> None:
        from syncmyvids.sync.graph import is_distinctive

        # Both of the wrong long-overlap pairs on that shoot looked like this.
        assert not is_distinctive(1.01, 2527.5)

    def test_a_short_overlap_cannot_be_rescued(self) -> None:
        from syncmyvids.sync.graph import is_distinctive

        # A towering ratio over a few seconds is what coincidence looks like.
        assert not is_distinctive(20.0, 30.0)

    def test_the_threshold_sits_between_the_measured_groups(self) -> None:
        from syncmyvids.sync.graph import RESCUE_PEAK_RATIO

        assert 1.01 < RESCUE_PEAK_RATIO < 4.9, "no margin either side"

    def test_a_rescued_edge_is_marked_as_such(self) -> None:
        from syncmyvids.sync.graph import Edge

        edge = Edge(a=0, b=1, delta_seconds=1.0, confidence=0.19,
                    peak_ratio=17.0, overlap_seconds=2500.0, distinctive=True)
        assert edge.distinctive
        # Rescued, not promoted: it carries the weight its coefficient earns.
        assert edge.weight < Edge(a=0, b=1, delta_seconds=1.0, confidence=0.9,
                                  peak_ratio=17.0, overlap_seconds=2500.0).weight
