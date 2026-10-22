# SPDX-License-Identifier: GPL-3.0-or-later
"""Placing unmatched clips from the camera's own clock.

Cameras stamp each file with when it started recording.  Clocks between devices
are never set alike, but each device's own clock is consistent, so one matched
clip fixes that device's offset to the timeline and every other clip from it can
be placed from its timestamp -- which is where it was actually shot, rather than
merely after whatever preceded it.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from syncmyvids.formats import fcp7
from syncmyvids.model import build_sync_units
from syncmyvids.sync.graph import UnitFeature
from syncmyvids.sync.layout import apply_layout
from syncmyvids.sync.solve import Component, Solution

TIMELINE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s" explodedTracks="true"><uuid>u</uuid><name>Cam</name>
    <duration>20000</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video>
        <track>
          <clipitem id="aV"><name>a</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>250</end><in>0</in><out>250</out>
            <file id="fa"><name>a.MP4</name>
              <pathurl>file://localhost/nowhere/a.MP4</pathurl></file>
          </clipitem>
          <clipitem id="bV"><name>b</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>250</start><end>500</end><in>0</in><out>250</out>
            <file id="fb"><name>b.MP4</name>
              <pathurl>file://localhost/nowhere/b.MP4</pathurl></file>
          </clipitem>
          <enabled>TRUE</enabled>
        </track>
        <track>
          <clipitem id="rV"><name>ref</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>250</end><in>0</in><out>250</out>
            <file id="fr"><name>ref.MP4</name>
              <pathurl>file://localhost/nowhere/ref.MP4</pathurl></file>
          </clipitem>
          <enabled>TRUE</enabled>
        </track>
      </video>
      <audio>
        <track><enabled>TRUE</enabled><outputchannelindex>1</outputchannelindex></track>
        <track><enabled>TRUE</enabled><outputchannelindex>2</outputchannelindex></track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


def _prepare(tmp_path: Path, stamps):
    path = tmp_path / "cam.xml"
    path.write_text(TIMELINE, encoding="utf-8")
    sequence = fcp7.read(path).sequence
    by = {u.primary_clip.name: u for u in build_sync_units(sequence)}
    units = [by["a"], by["b"], by["ref"]]
    features = [
        UnitFeature(0, units[0], None, 0.0, 10.0),   # synced
        UnitFeature(1, units[1], None, 0.0, 10.0),   # unmatched, same device
        UnitFeature(2, units[2], None, 0.0, 10.0),   # reference
    ]
    solution = Solution(
        components=[Component(nodes=[0, 2], positions={2: 0.0, 0: 0.0})],
        conflicts=[], singletons=[1],
    )
    return sequence, apply_layout(
        sequence, features, solution, reference_key=("video", 2),
        unsynced_gap_seconds=0.0, prune_empty_tracks=False, stamps=stamps)


class TestClockPlacement:
    def test_orphan_lands_at_its_recorded_time(self, tmp_path: Path) -> None:
        """"a" synced at 0 s and was shot at t=1000; "b" was shot 600 s later."""
        sequence, result = _prepare(tmp_path, {0: 1000.0, 1: 1600.0, 2: 1000.0})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        # offset for the device is 0 - 1000 = -1000, so "b" lands at 600 s.
        assert placed["b"] == 600 * 25

    def test_without_timestamps_it_falls_back_to_shooting_order(self, tmp_path: Path) -> None:
        sequence, result = _prepare(tmp_path, {})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        # No clock, so "b" simply follows the synced clip of its own device.
        assert placed["b"] == 250

    def test_a_device_with_no_synced_clip_gets_no_clock(self, tmp_path: Path) -> None:
        """Its offset is unknowable, so ordering is the only guide left."""
        sequence, result = _prepare(tmp_path, {1: 5000.0})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        assert placed["b"] == 250

    def test_orphans_never_land_before_zero(self, tmp_path: Path) -> None:
        sequence, result = _prepare(tmp_path, {0: 1000.0, 1: 100.0, 2: 1000.0})
        assert min(c.start for c in sequence.clips()) >= 0


class TestStereoOutput:
    def test_master_becomes_a_single_stereo_group(self, tmp_path: Path) -> None:
        """Premiere exports these as a mono master: two one-channel groups."""
        sequence, _ = _prepare(tmp_path, {})
        audio = sequence.element.find("media/audio")
        groups = audio.findall("outputs/group")
        assert len(groups) == 1
        assert groups[0].findtext("numchannels") == "2"
        assert [c.findtext("index") for c in groups[0].findall("channel")] == ["1", "2"]
        assert audio.findtext("numOutputChannels") == "2"

    def test_track_channel_routing_is_untouched(self, tmp_path: Path) -> None:
        """Channel 1 must still feed left and channel 2 right."""
        sequence, _ = _prepare(tmp_path, {})
        audio = sequence.element.find("media/audio")
        routing = [t.findtext("outputchannelindex") for t in audio.findall("track")]
        assert routing == ["1", "2"]

    def test_converting_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "m.xml"
        path.write_text(TIMELINE, encoding="utf-8")
        sequence = fcp7.read(path).sequence

        assert fcp7.set_stereo_output(sequence) is True, "mono master was not converted"
        after = etree.tostring(sequence.element.find("media/audio/outputs"))
        # Already stereo: nothing to do, and nothing changed.
        assert fcp7.set_stereo_output(sequence) is False
        assert etree.tostring(sequence.element.find("media/audio/outputs")) == after

    def test_the_sequence_can_be_left_mono_on_request(self, tmp_path: Path) -> None:
        path = tmp_path / "m.xml"
        path.write_text(TIMELINE, encoding="utf-8")
        sequence = fcp7.read(path).sequence
        by = {u.primary_clip.name: u for u in build_sync_units(sequence)}
        features = [UnitFeature(0, by["a"], None, 0.0, 10.0)]
        apply_layout(sequence, features,
                     Solution(components=[], conflicts=[], singletons=[0]),
                     prune_empty_tracks=False, stereo_output=False)
        # The fixture declares no <outputs> at all, so leaving it alone means
        # not inventing one.
        assert sequence.element.find("media/audio/outputs") is None


#: Two devices, two clips each. Used to build a solution where one clip from
#: each device synced to the other's, and the remaining two synced only to each
#: other -- an island.
ISLAND_TIMELINE = TIMELINE.replace(
    """          <enabled>TRUE</enabled>
        </track>
      </video>""",
    """          <clipitem id="cV"><name>c</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>250</start><end>500</end><in>0</in><out>250</out>
            <file id="fc"><name>c.MP4</name>
              <pathurl>file://localhost/nowhere/c.MP4</pathurl></file>
          </clipitem>
          <enabled>TRUE</enabled>
        </track>
      </video>""")


def _island(tmp_path: Path, stamps):
    """"a" and "ref" synced to each other; "b" and "c" only to each other."""
    path = tmp_path / "island.xml"
    path.write_text(ISLAND_TIMELINE, encoding="utf-8")
    sequence = fcp7.read(path).sequence
    by = {u.primary_clip.name: u for u in build_sync_units(sequence)}
    units = [by["a"], by["b"], by["ref"], by["c"]]
    features = [UnitFeature(i, u, None, 0.0, 10.0) for i, u in enumerate(units)]
    solution = Solution(
        components=[
            Component(nodes=[0, 2], positions={0: 0.0, 2: 0.0}),
            Component(nodes=[1, 3], positions={1: 0.0, 3: 0.0}),
        ],
        conflicts=[], singletons=[],
    )
    return sequence, apply_layout(
        sequence, features, solution, reference_key=("video", 2),
        unsynced_gap_seconds=0.0, prune_empty_tracks=False, stamps=stamps)


class TestIslandPlacement:
    """A group that synced to itself but to nothing else still has a home."""

    def test_an_island_goes_where_its_device_clock_says(self, tmp_path: Path) -> None:
        # "a" and "ref" sit at 0 and were shot at t=1000, so both devices are
        # offset by -1000. The island was shot 600 s later.
        _, result = _island(tmp_path, {0: 1000.0, 2: 1000.0, 1: 1600.0, 3: 1600.0})
        placed = {p.label.split(":", 1)[1]: p for p in result.placements}
        assert placed["b"].status == "island"
        assert placed["b"].start_frames == 600 * 25
        assert placed["c"].start_frames == 600 * 25

    def test_the_island_keeps_its_own_shape(self, tmp_path: Path) -> None:
        """Its internal geometry was measured; only its position was in doubt."""
        _, result = _island(tmp_path, {0: 1000.0, 2: 1000.0, 1: 1600.0, 3: 1604.0})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        assert placed["b"] == placed["c"], "the solve put them together"

    def test_without_a_clock_it_is_appended_as_before(self, tmp_path: Path) -> None:
        _, result = _island(tmp_path, {})
        placed = {p.label.split(":", 1)[1]: p for p in result.placements}
        assert placed["b"].status == "island"
        # After the synced material rather than at an arbitrary guess.
        assert placed["b"].start_frames >= 250


class TestRoomAtTheStart:
    def test_a_clip_shot_before_zero_gets_room_made_for_it(self, tmp_path: Path) -> None:
        """Clamping it to zero would silently put it in the wrong place.

        "a" synced at 0 and was shot at t=1000; "b" was shot 100 s *earlier*,
        so it belongs at -100 s. The whole timeline moves later instead.
        """
        sequence, result = _prepare(tmp_path, {0: 1000.0, 1: 900.0, 2: 1000.0})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        assert placed["b"] >= 0
        # 100 s earlier than its device's synced clip, exactly as the clock says.
        assert placed["a"] - placed["b"] == 100 * 25

    def test_everything_moves_together_so_sync_survives(self, tmp_path: Path) -> None:
        _, result = _prepare(tmp_path, {0: 1000.0, 1: 900.0, 2: 1000.0})
        placed = {p.label.split(":", 1)[1]: p.start_frames for p in result.placements}
        # "a" and "ref" were synced to each other and must still be.
        assert placed["a"] == placed["ref"]
