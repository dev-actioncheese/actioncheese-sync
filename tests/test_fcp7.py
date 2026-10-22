# SPDX-License-Identifier: GPL-3.0-or-later
"""xmeml reading and writing.

The overriding requirement is that a real project survives a round-trip: this
tool patches timing and must not become a lossy filter on everything else in a
Premiere export.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from syncmyvids.errors import XmemlParseError
from syncmyvids.formats import fcp7
from syncmyvids.model import build_sync_units
from syncmyvids.timebase import Rate

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="seq-1">
    <uuid>abc-123</uuid>
    <name>Test</name>
    <duration>1000</duration>
    <rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
    <media>
      <video>
        <track>
          <clipitem id="v1">
            <masterclipid>mc-1</masterclipid>
            <name>camA</name>
            <duration>500</duration>
            <rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
            <start>0</start><end>500</end><in>10</in><out>510</out>
            <pproTicksIn>0</pproTicksIn><pproTicksOut>100</pproTicksOut>
            <file id="f-1">
              <name>camA.mov</name>
              <pathurl>file://localhost/tmp/camA%20one.mov</pathurl>
              <rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
              <duration>900</duration>
            </file>
            <sourcetrack><mediatype>video</mediatype><trackindex>1</trackindex></sourcetrack>
            <link><linkclipref>v1</linkclipref><mediatype>video</mediatype>
                  <trackindex>1</trackindex><clipindex>1</clipindex></link>
            <link><linkclipref>a1</linkclipref><mediatype>audio</mediatype>
                  <trackindex>1</trackindex><clipindex>1</clipindex></link>
            <logginginfo><description>keep me</description></logginginfo>
          </clipitem>
        </track>
      </video>
      <audio>
        <track>
          <clipitem id="a1">
            <name>camA</name>
            <duration>500</duration>
            <rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
            <start>0</start><end>500</end><in>10</in><out>510</out>
            <file id="f-1"/>
            <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
            <link><linkclipref>v1</linkclipref><mediatype>video</mediatype>
                  <trackindex>1</trackindex><clipindex>1</clipindex></link>
            <link><linkclipref>a1</linkclipref><mediatype>audio</mediatype>
                  <trackindex>1</trackindex><clipindex>1</clipindex></link>
          </clipitem>
        </track>
        <track>
          <clipitem id="a2">
            <name>recorder</name>
            <duration>800</duration>
            <rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate>
            <start>100</start><end>900</end><in>0</in><out>800</out>
            <file id="f-2">
              <name>rec.wav</name>
              <pathurl>file://localhost/tmp/rec.wav</pathurl>
              <duration>800</duration>
            </file>
          </clipitem>
        </track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


@pytest.fixture
def sample_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample.xml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


class TestRead:
    def test_parses_structure(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        sequence = timeline.sequence
        assert sequence.name == "Test"
        assert sequence.rate == Rate(30, True)
        assert len(sequence.video_tracks) == 1
        assert len(sequence.audio_tracks) == 2

    def test_resolves_file_back_references(self, sample_path: Path) -> None:
        """``<file id="f-1"/>`` with no children must resolve to the full one.

        Premiere declares each file once and refers to it by id thereafter; a
        reader that misses this sees clips with no media path at all.
        """
        timeline = fcp7.read(sample_path)
        audio_clip = timeline.sequence.audio_tracks[0].clips[0]
        assert audio_clip.file is not None
        assert audio_clip.file.path == Path("/tmp/camA one.mov")

    def test_unquotes_pathurl(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        clip = timeline.sequence.video_tracks[0].clips[0]
        assert clip.file.path == Path("/tmp/camA one.mov")

    def test_rejects_fcpxml_with_a_useful_message(self, tmp_path: Path) -> None:
        path = tmp_path / "x.fcpxml"
        path.write_text('<?xml version="1.0"?><fcpxml version="1.11"></fcpxml>', encoding="utf-8")
        with pytest.raises(XmemlParseError, match="fcpxml"):
            fcp7.read(path)

    def test_rejects_file_without_sequences(self, tmp_path: Path) -> None:
        path = tmp_path / "x.xml"
        path.write_text('<?xml version="1.0"?><xmeml version="5"></xmeml>', encoding="utf-8")
        with pytest.raises(XmemlParseError, match="no <sequence>"):
            fcp7.read(path)


class TestSyncUnits:
    def test_links_group_into_one_unit(self, sample_path: Path) -> None:
        units = build_sync_units(fcp7.read(sample_path).sequence)
        # camA's picture and audio are one rigid unit; the recorder is its own.
        assert len(units) == 2
        camera = next(u for u in units if len(u.clips) == 2)
        assert not camera.is_audio_only
        assert camera.primary_clip.media_type == "video"

        recorder = next(u for u in units if len(u.clips) == 1)
        assert recorder.is_audio_only  # eligible for drift correction

    def test_unlinked_stereo_channels_group_together(self, tmp_path: Path) -> None:
        """Premiere exports a stereo recorder file as two *unlinked* clipitems.

        Taken from a real project export: the two mono channels carry no
        ``<link>`` at all, only differing ``sourcetrack`` indices.  Solving them
        independently would let the halves of one pair land at different
        offsets, which is audible as phasing.
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s"><name>S</name><duration>500</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media><video><track/></video><audio>
      <track><clipitem id="L"><name>rec.wav</name>
        <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
        <start>0</start><end>500</end><in>0</in><out>500</out>
        <file id="f"><name>rec.wav</name>
          <pathurl>file://localhost/tmp/rec.wav</pathurl></file>
        <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
      </clipitem></track>
      <track><clipitem id="R"><name>rec.wav</name>
        <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
        <start>0</start><end>500</end><in>0</in><out>500</out>
        <file id="f"/>
        <sourcetrack><mediatype>audio</mediatype><trackindex>2</trackindex></sourcetrack>
      </clipitem></track>
    </audio></media>
  </sequence>
</xmeml>
"""
        path = tmp_path / "stereo.xml"
        path.write_text(xml, encoding="utf-8")
        units = build_sync_units(fcp7.read(path).sequence)
        assert len(units) == 1
        assert {c.clip_id for c in units[0].clips} == {"L", "R"}

    def test_same_file_at_different_positions_stays_separate(self, tmp_path: Path) -> None:
        """The grouping rule must not merge two genuine uses of one file."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s"><name>S</name><duration>900</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media><video><track/></video><audio><track>
      <clipitem id="one"><name>rec.wav</name>
        <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
        <start>0</start><end>400</end><in>0</in><out>400</out>
        <file id="f"><name>rec.wav</name>
          <pathurl>file://localhost/tmp/rec.wav</pathurl></file>
      </clipitem>
      <clipitem id="two"><name>rec.wav</name>
        <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
        <start>400</start><end>900</end><in>500</in><out>1000</out>
        <file id="f"/>
      </clipitem>
    </track></audio></media>
  </sequence>
</xmeml>
"""
        path = tmp_path / "reused.xml"
        path.write_text(xml, encoding="utf-8")
        units = build_sync_units(fcp7.read(path).sequence)
        assert len(units) == 2

    def test_unit_spans_all_member_clips(self, sample_path: Path) -> None:
        units = build_sync_units(fcp7.read(sample_path).sequence)
        camera = next(u for u in units if len(u.clips) == 2)
        assert camera.start == 0
        assert camera.end == 500


class TestWrite:
    def test_round_trip_preserves_untouched_content(self, sample_path: Path, tmp_path: Path) -> None:
        """Reading and writing without edits must change nothing of substance."""
        timeline = fcp7.read(sample_path)
        output = tmp_path / "out.xml"
        fcp7.write(timeline, output)

        def flatten(path: Path) -> list[tuple[str, str]]:
            root = etree.parse(str(path)).getroot()
            return [(root.getroottree().getelementpath(e),
                     (e.text or "").strip()) for e in root.iter()]

        assert flatten(output) == flatten(sample_path)

    def test_shift_updates_start_end_and_duration(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        clip = timeline.sequence.video_tracks[0].clips[0]
        fcp7.shift_clip(clip, 250)
        assert clip.start == 250 and clip.end == 750
        assert clip.element.findtext("start") == "250"
        assert clip.element.findtext("end") == "750"
        # Source range must be untouched by a pure move.
        assert clip.element.findtext("in") == "10"
        assert clip.element.findtext("out") == "510"

    def test_changing_source_range_updates_ppro_ticks(self, sample_path: Path) -> None:
        """Premiere prefers its tick values over the frame values on import.

        Leaving them stale makes the edit silently revert to the original
        source range, which looks like the sync simply not having worked.
        """
        timeline = fcp7.read(sample_path)
        clip = timeline.sequence.video_tracks[0].clips[0]
        fcp7.set_clip_timing(clip, in_point=20, out_point=520)
        expected = fcp7.seconds_to_ppro_ticks(clip.rate.frames_to_seconds(520))
        assert clip.element.findtext("pproTicksOut") == str(expected)

    def test_speed_filter_is_premiere_shaped(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        clip = timeline.sequence.audio_tracks[1].clips[0]
        fcp7.apply_speed_filter(clip, 100.002)

        effect = clip.element.find("filter/effect")
        assert effect.findtext("effectid") == "timeremap"
        assert effect.findtext("mediatype") == "audio"
        parameters = {p.findtext("parameterid"): p.findtext("value")
                      for p in effect.findall("parameter")}
        assert parameters["speed"] == "100.002000"
        assert parameters["variablespeed"] == "0"
        assert parameters["reverse"] == "FALSE"

    def test_speed_filter_is_replaced_not_duplicated(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        clip = timeline.sequence.audio_tracks[1].clips[0]
        fcp7.apply_speed_filter(clip, 100.002)
        fcp7.apply_speed_filter(clip, 100.004)
        assert len(clip.element.findall("filter")) == 1


class TestLinks:
    def test_rebuild_is_a_no_op_on_untouched_document(self, sample_path: Path) -> None:
        """Recomputation must reproduce the exporter's own convention.

        If it does not, every sync would gratuitously rewrite links it had no
        reason to touch -- and we would have no way to tell a real fix from a
        convention mismatch.
        """
        timeline = fcp7.read(sample_path)
        assert fcp7.rebuild_link_indices(timeline.sequence) == 0

    def test_new_track_inherits_channel_routing(self, tmp_path: Path) -> None:
        """A relocated stereo pair must keep feeding the same output channels.

        Premiere exports with ``explodedTracks="true"``, writing a clip's stereo
        audio as two *mono* clipitems on separate tracks; each track's
        ``<outputchannelindex>`` is what routes it left or right.  A new track
        without that element does not inherit the original's routing, so moved
        audio can collapse toward one channel.
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s" explodedTracks="true"><name>S</name><duration>100</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media><video><track/></video><audio>
      <track><enabled>TRUE</enabled><outputchannelindex>1</outputchannelindex></track>
      <track><enabled>TRUE</enabled><outputchannelindex>2</outputchannelindex></track>
    </audio></media>
  </sequence>
</xmeml>
"""
        path = tmp_path / "exploded.xml"
        path.write_text(xml, encoding="utf-8")
        sequence = fcp7.read(path).sequence

        left = fcp7.append_track(sequence, "audio", like=sequence.audio_tracks[0])
        right = fcp7.append_track(sequence, "audio", like=sequence.audio_tracks[1])
        assert left.element.findtext("outputchannelindex") == "1"
        assert right.element.findtext("outputchannelindex") == "2"

    def test_new_track_without_a_model_has_no_routing(self, sample_path: Path) -> None:
        sequence = fcp7.read(sample_path).sequence
        track = fcp7.append_track(sequence, "audio")
        assert track.element.find("outputchannelindex") is None

    def test_rebuild_follows_a_moved_clip(self, sample_path: Path) -> None:
        timeline = fcp7.read(sample_path)
        sequence = timeline.sequence
        audio_clip = sequence.audio_tracks[0].clips[0]

        new_track = fcp7.append_track(sequence, "audio")
        sequence.audio_tracks[0].clips.remove(audio_clip)
        fcp7.move_clip_to_track(audio_clip, new_track)
        fcp7.rebuild_link_indices(sequence)

        video_clip = sequence.video_tracks[0].clips[0]
        link = next(l for l in video_clip.element.findall("link")
                    if l.findtext("linkclipref") == "a1")
        assert link.findtext("trackindex") == str(new_track.index)
        assert link.findtext("mediatype") == "audio"


class TestPaths:
    @pytest.mark.parametrize(
        ("pathurl", "expected"),
        [
            ("file://localhost/Volumes/X/a.mov", Path("/Volumes/X/a.mov")),
            ("file:///Volumes/X/a.mov", Path("/Volumes/X/a.mov")),
            ("file://localhost/tmp/a%20b.mov", Path("/tmp/a b.mov")),
        ],
    )
    def test_pathurl_to_path(self, pathurl: str, expected: Path) -> None:
        assert fcp7.pathurl_to_path(pathurl) == expected

    def test_path_round_trip(self) -> None:
        path = Path("/Volumes/Media Drive/take one.mov")
        assert fcp7.pathurl_to_path(fcp7.path_to_pathurl(path)) == path

    def test_relocate_media(self, sample_path: Path, tmp_path: Path) -> None:
        media_root = tmp_path / "footage"
        media_root.mkdir()
        (media_root / "rec.wav").write_bytes(b"")

        timeline = fcp7.read(sample_path)
        assert fcp7.relocate_media(timeline, media_root) == 1
        reread = [f for f in timeline.tree.getroot().iter("file")
                  if f.findtext("name") == "rec.wav"][0]
        assert fcp7.pathurl_to_path(reread.findtext("pathurl")) == media_root / "rec.wav"
