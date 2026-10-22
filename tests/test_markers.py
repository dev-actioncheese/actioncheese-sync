# SPDX-License-Identifier: GPL-3.0-or-later
"""Markers must end up on the moment they were put on, not the frame."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from syncmyvids import markers, sources
from syncmyvids.formats import fcp7, fcp7_build
from syncmyvids.timebase import Rate

from conftest import MEDIA_DIR, requires_ffmpeg

WITH_MARKERS = """<?xml version="1.0" encoding="UTF-8"?>
<xmeml version="5">
  <sequence id="s1">
    <name>Marked</name>
    <duration>1000</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video><track>
        <clipitem id="c1"><name>one</name><duration>250</duration>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>100</start><end>350</end><in>0</in><out>250</out>
          <file id="f1"><name>one.mov</name>
            <pathurl>file://localhost/nowhere/one.mov</pathurl></file>
        </clipitem>
        <clipitem id="c2"><name>two</name><duration>250</duration>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>600</start><end>850</end><in>0</in><out>250</out>
          <file id="f2"><name>two.mov</name>
            <pathurl>file://localhost/nowhere/two.mov</pathurl></file>
        </clipitem>
      </track></video>
      <audio><track/></audio>
    </media>
    <marker><name>inside one</name><comment>a note</comment><in>150</in><out>-1</out></marker>
    <marker><name>between</name><comment></comment><in>500</in><out>-1</out></marker>
    <marker><name>a range</name><comment></comment><in>620</in><out>680</out></marker>
  </sequence>
</xmeml>
"""


@pytest.fixture()
def marked(tmp_path: Path):
    path = tmp_path / "marked.xml"
    path.write_text(WITH_MARKERS, encoding="utf-8")
    return fcp7.read(path)


class TestAnchoring:
    def test_a_marker_is_tied_to_the_clip_playing_under_it(self, marked) -> None:
        found = markers.collect(marked.sequence)
        first = next(m for m in found if m.name == "inside one")
        assert first.clip_id == "c1"
        assert first.offset == 50, "150 is 50 frames into a clip starting at 100"

    def test_a_marker_over_nothing_follows_the_nearest_clip(self, marked) -> None:
        # Dropped between takes. It still has to keep its distance from them.
        found = markers.collect(marked.sequence)
        between = next(m for m in found if m.name == "between")
        assert between.anchored
        assert between.clip_id in ("c1", "c2")

    def test_a_hint_decides_between_clips_that_both_cover_it(self, marked) -> None:
        # Several cameras cover one moment; the marker belongs to the camera it
        # was taken on.
        found = markers.collect(marked.sequence, {"a range": ("c2",)})
        assert next(m for m in found if m.name == "a range").clip_id == "c2"

    def test_a_point_marker_and_a_range_are_told_apart(self, marked) -> None:
        found = {m.name: m for m in markers.collect(marked.sequence)}
        assert found["inside one"].span == markers.POINT_MARKER
        assert found["a range"].span == 60

    def test_no_clips_means_no_anchor(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.xml"
        path.write_text(
            '<?xml version="1.0"?><xmeml version="5"><sequence id="s"><name>n</name>'
            "<duration>10</duration><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>"
            "<media><video><track/></video></media>"
            "<marker><name>m</name><comment></comment><in>5</in><out>-1</out></marker>"
            "</sequence></xmeml>", encoding="utf-8")
        found = markers.collect(fcp7.read(path).sequence)
        assert found and not found[0].anchored


class TestRepositioning:
    def move(self, sequence, clip_id: str, delta: int) -> None:
        for clip in sequence.clips():
            if clip.clip_id == clip_id:
                fcp7.set_clip_timing(clip, start=clip.start + delta,
                                     end=clip.end + delta)

    def test_a_marker_moves_by_as_much_as_its_clip(self, marked) -> None:
        sequence = marked.sequence
        found = markers.collect(sequence)
        self.move(sequence, "c1", 400)
        assert markers.reposition(sequence, found) >= 1
        element = sequence.element.findall("marker")[0]
        assert int(element.findtext("in")) == 550, "150 + 400"

    def test_a_range_marker_keeps_its_length(self, marked) -> None:
        sequence = marked.sequence
        found = markers.collect(sequence, {"a range": ("c2",)})
        self.move(sequence, "c2", -100)
        markers.reposition(sequence, found)
        element = next(e for e in sequence.element.findall("marker")
                       if e.findtext("name") == "a range")
        assert int(element.findtext("in")) == 520
        assert int(element.findtext("out")) == 580

    def test_a_point_marker_stays_a_point(self, marked) -> None:
        sequence = marked.sequence
        found = markers.collect(sequence)
        self.move(sequence, "c1", 25)
        markers.reposition(sequence, found)
        element = sequence.element.findall("marker")[0]
        assert int(element.findtext("out")) == markers.POINT_MARKER

    def test_a_marker_is_never_pushed_before_the_start(self, marked) -> None:
        sequence = marked.sequence
        found = markers.collect(sequence)
        self.move(sequence, "c1", -100)
        markers.reposition(sequence, found)
        assert int(sequence.element.findall("marker")[0].findtext("in")) >= 0

    def test_the_comment_survives(self, marked) -> None:
        sequence = marked.sequence
        markers.reposition(sequence, markers.collect(sequence))
        assert sequence.element.findall("marker")[0].findtext("comment") == "a note"

    def test_a_marker_whose_clip_vanished_is_reported_not_dropped(self, marked) -> None:
        sequence = marked.sequence
        found = markers.collect(sequence)
        gone = next(c for c in sequence.clips() if c.clip_id == "c1")
        fcp7.remove_clip(sequence, gone)
        assert "inside one" in markers.orphaned(found, sequence)
        # Still in the document: losing somebody's note is worse than leaving
        # it slightly adrift.
        assert len(sequence.element.findall("marker")) == 3


class TestAppending:
    def test_a_marker_lands_after_the_media_element(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.xml"
        path.write_text(
            '<?xml version="1.0"?><xmeml version="5"><sequence id="s"><name>n</name>'
            "<duration>10</duration><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>"
            "<media><video><track/></video></media></sequence></xmeml>", encoding="utf-8")
        sequence = fcp7.read(path).sequence
        markers.append(sequence, "m", "", 5)
        tags = [c.tag for c in sequence.element]
        assert tags.index("marker") == tags.index("media") + 1


@requires_ffmpeg
class TestShootMarkers:
    def shoot(self, tmp_path: Path) -> Path:
        folder = tmp_path / "2026-09-27"
        folder.mkdir()
        shutil.copy(MEDIA_DIR / "camA.mov", folder / "Take 01 - phone.mov")
        (folder / sources.SHOOT_INDEX).write_text(json.dumps({
            "clips": [{"camera": "phone", "file": "Take 01 - phone.mov",
                       "start": 1000.0 - sources.APPLE_EPOCH_OFFSET, "duration": 12.0,
                       "isVideo": True, "frameRate": 30}],
            "markers": [{"number": 1, "date": 1004.0 - sources.APPLE_EPOCH_OFFSET,
                         "cameras": ["phone"]},
                        {"number": 2, "date": 1200.0 - sources.APPLE_EPOCH_OFFSET,
                         "cameras": []}],
        }), encoding="utf-8")
        return folder

    def test_the_index_s_markers_are_read(self, fixture_timeline: Path,
                                          tmp_path: Path) -> None:
        result = sources.ingest([self.shoot(tmp_path)])
        assert [m.name for m in result.markers] == ["Marker 1", "Marker 2"]
        assert result.markers[0].devices == ("phone",)

    def test_they_reach_the_built_timeline_at_the_right_frame(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        result = sources.ingest([self.shoot(tmp_path)])
        built = fcp7_build.build(result.devices, rate=Rate(30, False),
                                 markers=result.markers,
                                 out_path=tmp_path / "built.xml")
        elements = built.timeline.sequence.element.findall("marker")
        assert [e.findtext("name") for e in elements] == ["Marker 1", "Marker 2"]
        # Marker 1 is 4 s after the only clip started, and that clip is the
        # origin, so 4 s at 30 fps.
        assert int(elements[0].findtext("in")) == 120

    def test_a_marker_is_hinted_towards_the_camera_it_was_taken_on(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        result = sources.ingest([self.shoot(tmp_path)])
        built = fcp7_build.build(result.devices, rate=Rate(30, False),
                                 markers=result.markers,
                                 out_path=tmp_path / "built.xml")
        assert built.marker_hints["Marker 1"], "taken on a camera"
        # No camera was rolling, but the shoot's own clips share its clock and
        # are what it should follow.
        assert built.marker_hints["Marker 2"]


@requires_ffmpeg
class TestThroughARealSync:
    """The whole point: a marker still marks the same moment afterwards."""

    def marked_copy(self, fixture_timeline: Path, tmp_path: Path) -> Path:
        """The fixture timeline with a marker placed inside its first clip."""
        tree = etree.parse(str(fixture_timeline))
        sequence = tree.getroot().find("sequence")
        clip = sequence.find("media/video/track/clipitem")
        start = int(clip.findtext("start")) + 100
        element = etree.SubElement(sequence, "marker")
        for tag, value in (("name", "the moment"), ("comment", "keep me"),
                           ("in", start), ("out", -1)):
            etree.SubElement(element, tag).text = str(value)
        path = tmp_path / "marked.xml"
        tree.write(str(path), encoding="UTF-8", xml_declaration=True)
        return path

    def run(self, path: Path, **options):
        from syncmyvids.engine import SyncOptions, sync_timeline

        timeline = fcp7.read(path)
        sequence = timeline.sequence
        clip = next(c for c in sequence.clips() if c.clip_id)
        before = (clip.clip_id, clip.start, int(
            sequence.element.find("marker").findtext("in")))
        report = sync_timeline(timeline, SyncOptions(**options))
        return timeline, report, before

    def test_the_marker_keeps_its_place_inside_its_clip(self, fixture_timeline: Path,
                                                        tmp_path: Path) -> None:
        path = self.marked_copy(fixture_timeline, tmp_path)
        timeline, report, (clip_id, old_start, old_marker) = self.run(path)
        sequence = timeline.sequence
        clip = next(c for c in sequence.clips() if c.clip_id == clip_id)
        marker = int(sequence.element.find("marker").findtext("in"))
        assert marker - clip.start == old_marker - old_start
        assert report.markers_total == 1

    def test_closing_gaps_takes_the_marker_with_it(self, fixture_timeline: Path,
                                                   tmp_path: Path) -> None:
        # Gap closing shifts everything; a marker left behind would land on
        # whatever slid into its frame.
        path = self.marked_copy(fixture_timeline, tmp_path)
        timeline, _, (clip_id, old_start, old_marker) = self.run(path, close_gaps=True)
        sequence = timeline.sequence
        clip = next(c for c in sequence.clips() if c.clip_id == clip_id)
        marker = int(sequence.element.find("marker").findtext("in"))
        assert marker - clip.start == old_marker - old_start

    def test_the_marker_survives_the_write(self, fixture_timeline: Path,
                                           tmp_path: Path) -> None:
        path = self.marked_copy(fixture_timeline, tmp_path)
        timeline, _, _ = self.run(path)
        out = tmp_path / "out.xml"
        fcp7.write(timeline, out)
        written = fcp7.read(out).sequence.element.findall("marker")
        assert [e.findtext("name") for e in written] == ["the moment"]
        assert written[0].findtext("comment") == "keep me"


class TestOnTheTimelineStrip:
    def test_markers_reach_the_snapshot(self, marked) -> None:
        from syncmyvids.timelineview import snapshot

        snap = snapshot(marked.sequence)
        assert [m.name for m in snap.markers] == ["inside one", "between", "a range"]
        assert snap.markers[0].frame == 150

    def test_a_distant_marker_does_not_stretch_the_scale(self, marked) -> None:
        # A marker dropped an hour after the last take would otherwise squeeze
        # the footage into a few pixels. It is left off the right-hand edge;
        # the exported XML still carries it.
        from syncmyvids.timelineview import snapshot

        element = marked.sequence.element.findall("marker")[0]
        element.find("in").text = "500000"
        snap = snapshot(marked.sequence)
        assert snap.duration == 850, "the last clip's end, not the marker"
        assert any(m.frame == 500000 for m in snap.markers)

    def test_the_strip_and_its_markers_share_one_scale(self, marked) -> None:
        from syncmyvids.timelineview import layout_rects, snapshot, timeline_scale

        snap = snapshot(marked.sequence)
        rects, _, _ = layout_rects(snap, width=960)
        _, _, scale = timeline_scale(snap, 960)
        clip = next(r for r in rects if r.kind == "clip")
        assert clip.x0 == pytest.approx(46 + clip.clip.start * scale)
