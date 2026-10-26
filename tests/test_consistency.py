# SPDX-License-Identifier: GPL-3.0-or-later
"""A device cannot record two things at once."""

from __future__ import annotations

from syncmyvids.model import Clip, SyncUnit
from syncmyvids.sync.consistency import enforce_device_order
from syncmyvids.sync.graph import Edge, UnitFeature
from syncmyvids.sync.solve import Component, Solution
from syncmyvids.timebase import Rate

RATE = Rate(25, False)


def _feature(index: int, name: str, track: int, duration: float) -> UnitFeature:
    clip = Clip(clip_id=name, name=name, media_type="video", track_index=track,
                start=0, end=int(duration * 25), in_point=0,
                out_point=int(duration * 25), rate=RATE, file=None)
    return UnitFeature(index, SyncUnit(key=name, clips=[clip]), None, 0.0, duration)


def _edge(a: int, b: int, weight_conf: float) -> Edge:
    return Edge(a=a, b=b, delta_seconds=0.0, confidence=weight_conf,
                peak_ratio=1.0, overlap_seconds=100.0)


class TestDeviceOrder:
    def test_overlapping_same_device_clips_are_demoted(self) -> None:
        """Two takes from one camera cannot occupy the same moment."""
        features = [_feature(0, "a", 1, 100.0), _feature(1, "b", 1, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 50.0})],
            conflicts=[], singletons=[],
        )
        edges = [_edge(0, 1, 0.9)]
        result, reports = enforce_device_order(
            features, solution, edges, {0: 0, 1: 1})
        assert len(reports) == 1
        assert reports[0]["clip"] == "V1:b"
        # Demoting one of a pair leaves a single node, which is no longer a
        # component either, so both fall out.
        assert 1 in result.singletons
        assert result.components == []

    def test_inverted_order_is_demoted(self) -> None:
        """Shot first must appear first."""
        features = [_feature(0, "a", 1, 10.0), _feature(1, "b", 1, 10.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 500.0, 1: 0.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports and reports[0]["clip"] == "V1:b"
        assert 1 in result.singletons

    def test_unmatched_footage_between_takes_still_needs_room(self) -> None:
        """The clips in between have to fit, even though they matched nothing.

        Two synced takes 60 s apart cannot be right when 600 s of that camera's
        own footage belongs between them.
        """
        features = [
            _feature(0, "first", 1, 30.0),
            _feature(1, "orphan", 1, 600.0),   # unmatched, sits between them
            _feature(2, "second", 1, 30.0),
        ]
        solution = Solution(
            components=[Component(nodes=[0, 2], positions={0: 0.0, 2: 60.0})],
            conflicts=[], singletons=[1],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 2, 0.9)], {0: 0, 1: 1, 2: 2})
        assert reports, "cramped spacing was not detected"
        assert 2 in result.singletons

    def test_consistent_layout_is_left_alone(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 100.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_different_devices_are_never_compared(self) -> None:
        """Two cameras may of course record the same moment."""
        features = [_feature(0, "a", 1, 100.0), _feature(1, "b", 2, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 0.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports == []
        assert len(result.components[0].nodes) == 2

    def test_the_better_evidenced_placement_survives(self) -> None:
        features = [_feature(0, "weak", 1, 100.0), _feature(1, "strong", 1, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 50.0})],
            conflicts=[], singletons=[],
        )
        # unit 1 is held up by a much stronger match, so unit 0 should give way
        edges = [_edge(0, 1, 0.2), _edge(1, 1, 0.95)]
        _, reports = enforce_device_order(features, solution, edges, {0: 0, 1: 1})
        assert [r["clip"] for r in reports] == ["V1:weak"]


class TestDeviceClock:
    """Recording timestamps are a far sharper test than ordering alone.

    A wrong match can be in the right order, overlap nothing, and still sit
    minutes from where the camera says the clip was shot.  Within a component
    the unknown device-to-timeline offset cancels in the difference between two
    clips, so no offset has to be estimated.
    """

    def _solution(self, first: float, second: float) -> Solution:
        return Solution(
            components=[Component(nodes=[0, 1], positions={0: first, 1: second})],
            conflicts=[], singletons=[],
        )

    def test_spacing_must_match_what_the_clock_reports(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        # Shot 600 s apart, but the sync placed them 100 s apart.
        stamps = {0: 1000.0, 1: 1600.0}
        _, reports = enforce_device_order(
            features, self._solution(0.0, 100.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports, "clock disagreement was not detected"
        assert reports[0]["reason"] == "position disagrees with this device's clock"

    def test_agreeing_spacing_is_accepted(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        stamps = {0: 1000.0, 1: 1600.0}
        result, reports = enforce_device_order(
            features, self._solution(0.0, 600.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_second_granular_timestamps_are_not_treated_as_error(self) -> None:
        """Camera clocks tick in whole seconds, and drift a little over hours."""
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        stamps = {0: 1000.0, 1: 1600.0}
        _, reports = enforce_device_order(
            features, self._solution(0.0, 602.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []

    def test_clips_without_timestamps_fall_back_to_ordering(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        result, reports = enforce_device_order(
            features, self._solution(0.0, 100.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps={})
        # 100 s apart with 30 s clips is fine on ordering grounds alone.
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_different_devices_keep_their_own_clocks(self) -> None:
        """Two cameras' clocks are never set alike; only same-device pairs compare."""
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 2, 30.0)]
        stamps = {0: 1000.0, 1: 99999.0}      # wildly different clocks
        _, reports = enforce_device_order(
            features, self._solution(0.0, 10.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []


class TestBelievingAnAlignment:
    """The acceptance rule, stated where it can be argued with.

    A wedding party's music is close to periodic, so the correlation surface
    is full of lags that score alike. The gate used to sit at 1.0, which
    accepts a peak that merely *ties* its best rival -- and a tie says nothing
    about which lag is right. Three such pairs reached the solver on a real
    project, one placing a clip 9313 s from where it belonged.
    """

    def accept(self, conf: float, peak: float, overlap: float = 100.0) -> bool:
        from syncmyvids.engine import SyncOptions
        from syncmyvids.sync.graph import is_acceptable

        options = SyncOptions()
        return is_acceptable(conf, peak, overlap,
                             min_confidence=options.min_confidence,
                             min_peak_ratio=options.min_peak_ratio)

    def test_a_tie_is_not_a_match(self) -> None:
        assert not self.accept(0.90, 1.00), "a peak level with its rival"
        assert not self.accept(0.49, 1.007), "the 9313s pair"

    def test_a_clear_winner_over_a_strong_rival_is_kept(self) -> None:
        # The weakest match that reached the solver on a healthy four-camera
        # shoot won by 50%. Nothing about this gate may threaten that.
        assert self.accept(0.50, 1.495)

    def test_the_gate_does_not_demand_a_tall_peak(self) -> None:
        # Reverberant material gives correct matches a modest margin: a 1.4
        # threshold once rejected 9 of 13 of them. The rule is "not a coin
        # flip", not "towering".
        from syncmyvids.engine import SyncOptions

        assert 1.0 < SyncOptions().min_peak_ratio <= 1.2, (
            "a high peak-ratio requirement throws away correct matches on "
            "reverberant audio")
        assert self.accept(0.60, 1.22)

    def test_confidence_still_has_to_be_met(self) -> None:
        assert not self.accept(0.10, 3.0, overlap=10.0)

    def test_an_isolated_peak_over_a_long_overlap_still_rescues(self) -> None:
        # The existing rescue must survive: it is what saves a camera whose
        # only clip would otherwise sit eleven minutes adrift.
        from syncmyvids.sync.graph import RESCUE_OVERLAP_SECONDS, RESCUE_PEAK_RATIO

        assert self.accept(0.19, RESCUE_PEAK_RATIO, RESCUE_OVERLAP_SECONDS)
