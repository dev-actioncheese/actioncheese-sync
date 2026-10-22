# SPDX-License-Identifier: GPL-3.0-or-later
"""Orphans shot before their device's first synced clip need room made for them.

Two devices.  Camera A recorded a long take that matches nothing, then a short
one that does; camera B is the reference.  The sync pulls camera A's second take
near the head of the timeline, leaving nowhere for the 200 s shot before it.
"""

from __future__ import annotations

from pathlib import Path

from syncmyvids.formats import fcp7
from syncmyvids.model import build_sync_units
from syncmyvids.sync.graph import UnitFeature
from syncmyvids.sync.layout import apply_layout
from syncmyvids.sync.solve import Component, Solution

TIMELINE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s" explodedTracks="true"><uuid>u-1</uuid><name>Cam</name>
    <duration>6000</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video>
        <track>
          <clipitem id="eV"><name>early</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>5000</end><in>0</in><out>5000</out>
            <file id="fe"><name>early.MP4</name>
              <pathurl>file://localhost/nowhere/early.MP4</pathurl></file>
            <link><linkclipref>eV</linkclipref></link><link><linkclipref>eA</linkclipref></link>
          </clipitem>
          <clipitem id="lV"><name>late</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>5000</start><end>6000</end><in>0</in><out>1000</out>
            <file id="fl"><name>late.MP4</name>
              <pathurl>file://localhost/nowhere/late.MP4</pathurl></file>
            <link><linkclipref>lV</linkclipref></link><link><linkclipref>lA</linkclipref></link>
          </clipitem>
          <enabled>TRUE</enabled>
        </track>
        <track>
          <clipitem id="rV"><name>ref</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>4000</end><in>0</in><out>4000</out>
            <file id="fr"><name>ref.MP4</name>
              <pathurl>file://localhost/nowhere/ref.MP4</pathurl></file>
            <link><linkclipref>rV</linkclipref></link><link><linkclipref>rA</linkclipref></link>
          </clipitem>
          <enabled>TRUE</enabled>
        </track>
      </video>
      <audio>
        <track>
          <clipitem id="eA"><name>early</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>5000</end><in>0</in><out>5000</out>
            <file id="fe"/>
            <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
            <link><linkclipref>eV</linkclipref></link><link><linkclipref>eA</linkclipref></link>
          </clipitem>
          <clipitem id="lA"><name>late</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>5000</start><end>6000</end><in>0</in><out>1000</out>
            <file id="fl"/>
            <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
            <link><linkclipref>lV</linkclipref></link><link><linkclipref>lA</linkclipref></link>
          </clipitem>
          <enabled>TRUE</enabled><outputchannelindex>1</outputchannelindex>
        </track>
        <track>
          <clipitem id="rA"><name>ref</name>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>4000</end><in>0</in><out>4000</out>
            <file id="fr"/>
            <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
            <link><linkclipref>rV</linkclipref></link><link><linkclipref>rA</linkclipref></link>
          </clipitem>
          <enabled>TRUE</enabled><outputchannelindex>1</outputchannelindex>
        </track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


def _prepare(tmp_path: Path):
    path = tmp_path / "cam.xml"
    path.write_text(TIMELINE, encoding="utf-8")
    sequence = fcp7.read(path).sequence
    by_name = {u.primary_clip.name: u for u in build_sync_units(sequence)}
    order = [by_name["early"], by_name["late"], by_name["ref"]]
    features = [
        UnitFeature(0, order[0], None, 0.0, 200.0),   # orphan, shot first
        UnitFeature(1, order[1], None, 0.0, 40.0),    # synced
        UnitFeature(2, order[2], None, 0.0, 160.0),   # reference, synced
    ]
    # "late" is solved to sit 40 s after the reference -- near the head, with
    # nowhere near enough room in front of it for the 200 s shot earlier.
    solution = Solution(
        components=[Component(nodes=[1, 2], positions={2: 0.0, 1: 40.0})],
        conflicts=[], singletons=[0],
    )
    return sequence, features, solution


def _run(tmp_path: Path):
    sequence, features, solution = _prepare(tmp_path)
    result = apply_layout(sequence, features, solution,
                          reference_key=("video", 2),
                          unsynced_gap_seconds=0.0, prune_empty_tracks=False)
    return sequence, result


class TestHeadroom:
    def test_orphan_audio_does_not_spill_onto_a_new_pair(self, tmp_path: Path) -> None:
        """This is what makes an export sprout extra audio tracks.

        Stacked forward without room, the orphan runs straight through its own
        device's synced take; finding the track occupied, the layout gives it a
        track of its own -- and with exploded stereo that means a whole new pair.
        """
        sequence, result = _run(tmp_path)
        assert len(sequence.audio_tracks) == 2, "orphan audio spilled onto a new track"
        assert not [k for k in result.new_tracks if k.startswith("A")]

    def test_room_is_made_by_moving_the_synced_material_later(self, tmp_path: Path) -> None:
        sequence, _ = _run(tmp_path)
        clips = {c.name: c for c in sequence.audio_tracks[0].clips}
        assert clips["early"].start == 0
        # The synced take had to move right to leave 200 s in front of it.
        assert clips["late"].start >= clips["early"].end

    def test_nothing_overlaps_on_any_track(self, tmp_path: Path) -> None:
        sequence, _ = _run(tmp_path)
        for track in sequence.tracks:
            ordered = sorted(track.clips, key=lambda c: c.start)
            for first, second in zip(ordered, ordered[1:], strict=False):
                assert first.end <= second.start, f"{first} overlaps {second}"

    def test_sync_relationship_is_preserved(self, tmp_path: Path) -> None:
        """Making room shifts everything equally, so the solve still holds."""
        sequence, _ = _run(tmp_path)
        by = {c.name: c for c in sequence.video_tracks[0].clips}
        ref = sequence.video_tracks[1].clips[0]
        late = by.get("late") or sequence.video_tracks[0].clips[0]
        assert (late.start - ref.start) == 40 * 25   # 40 s at 25 fps

    def test_timeline_starts_at_zero_or_later(self, tmp_path: Path) -> None:
        sequence, _ = _run(tmp_path)
        assert min(c.start for c in sequence.clips()) >= 0
