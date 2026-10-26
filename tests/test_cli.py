# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI behaviour and awkward-input handling."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from syncmyvids.cli import main
from syncmyvids.engine import load_inputs
from syncmyvids.errors import SyncMyVidsError
from syncmyvids.engine import SyncOptions, sync_timeline
from syncmyvids.formats import fcp7

MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="s1">
    <name>Empty</name>
    <duration>100</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media><video><track/></video><audio><track/></audio></media>
  </sequence>
</xmeml>
"""

NO_MEDIA = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="s1">
    <uuid>11111111-2222-3333-4444-555555555555</uuid>
    <name>Missing</name>
    <duration>500</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video><track>
        <clipitem id="c1"><name>gone</name><duration>250</duration>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>0</start><end>250</end><in>0</in><out>250</out>
          <file id="f1"><name>gone.mov</name>
            <pathurl>file://localhost/nowhere/gone.mov</pathurl></file>
        </clipitem>
        <clipitem id="c2"><name>gone2</name><duration>250</duration>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>250</start><end>500</end><in>0</in><out>250</out>
          <file id="f2"><name>gone2.mov</name>
            <pathurl>file://localhost/nowhere/gone2.mov</pathurl></file>
        </clipitem>
      </track></video>
      <audio><track/></audio>
    </media>
  </sequence>
</xmeml>
"""


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestCliArguments:
    def test_requires_output_or_dry_run(self, tmp_path: Path, capsys) -> None:
        source = write(tmp_path, "in.xml", MINIMAL)
        assert main(["sync", str(source)]) == 2
        assert "--dry-run" in capsys.readouterr().err

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        source = write(tmp_path, "in.xml", MINIMAL)
        assert main(["sync", str(source), "--dry-run"]) == 0
        assert list(tmp_path.iterdir()) == [source]

    def test_probe_lists_tracks(self, tmp_path: Path, capsys) -> None:
        source = write(tmp_path, "in.xml", NO_MEDIA)
        assert main(["probe", str(source)]) == 0
        out = capsys.readouterr().out
        assert "Missing" in out and "V1:" in out
        assert "media missing" in out

    def test_fcpxml_is_rejected_cleanly(self, tmp_path: Path, capsys) -> None:
        source = write(tmp_path, "in.fcpxml",
                       '<?xml version="1.0"?><fcpxml version="1.11"/>')
        assert main(["probe", str(source)]) == 1
        assert "not supported" in capsys.readouterr().err

    def test_report_is_written(self, tmp_path: Path) -> None:
        source = write(tmp_path, "in.xml", MINIMAL)
        report = tmp_path / "r.json"
        assert main(["sync", str(source), "--dry-run", "--report", str(report)]) == 0
        assert '"summary"' in report.read_text(encoding="utf-8")


class TestDegenerateTimelines:
    def test_empty_sequence(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "in.xml", MINIMAL))
        report = sync_timeline(timeline)
        assert report.unit_count == 0
        assert "no clips" in " ".join(report.warnings)

    def test_missing_media_becomes_orphans_not_a_crash(self, tmp_path: Path) -> None:
        """Relinking is the operator's job; we must still produce a usable file."""
        timeline = fcp7.read(write(tmp_path, "in.xml", NO_MEDIA))
        report = sync_timeline(timeline)
        assert report.synced_count == 0
        assert report.unsynced_count == 2
        assert all("media not found" in p["reason"] for p in report.placements)

    def test_nothing_syncs_starts_the_orphan_block_at_zero(self, tmp_path: Path) -> None:
        """With no synced clip there is nothing to come after, so start at 0."""
        timeline = fcp7.read(write(tmp_path, "in.xml", NO_MEDIA))
        report = sync_timeline(timeline, SyncOptions(unsynced_gap_seconds=0.0))
        assert report.last_synced_end_frames == 0
        assert min(p["start_frames"] for p in report.placements) == 0

    def test_output_of_degenerate_run_is_still_valid(self, tmp_path: Path) -> None:
        source = write(tmp_path, "in.xml", NO_MEDIA)
        timeline = fcp7.read(source)
        sync_timeline(timeline)
        output = tmp_path / "out.xml"
        fcp7.write(timeline, output)
        # Must still be readable by our own reader, under the derived name.
        assert fcp7.read(output).sequence.name == "Missing_synced"

    def test_sequence_is_renamed_and_reidentified(self, tmp_path: Path) -> None:
        """The output is a new sequence, so it must not collide in the bin."""
        source = write(tmp_path, "in.xml", NO_MEDIA)
        before = fcp7.read(source)
        original_uuid = before.sequence.element.findtext("uuid")

        timeline = fcp7.read(source)
        report = sync_timeline(timeline)
        assert report.sequence_name == "Missing_synced"
        assert timeline.sequence.element.findtext("uuid") != original_uuid

    def test_suffix_is_not_applied_twice(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "in.xml", NO_MEDIA))
        sync_timeline(timeline)
        sync_timeline(timeline)
        assert timeline.sequence.name == "Missing_synced"

    def test_explicit_name_wins(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "in.xml", NO_MEDIA))
        sync_timeline(timeline, SyncOptions(sequence_name="Wedding Sync"))
        assert timeline.sequence.name == "Wedding Sync"


GAPPY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <sequence id="s1"><name>Gappy</name><duration>400</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video><track>
        <clipitem id="v-a"><name>a</name>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>50</start><end>100</end><in>0</in><out>50</out></clipitem>
        <clipitem id="v-b"><name>b</name>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>300</start><end>400</end><in>0</in><out>100</out></clipitem>
      </track></video>
      <audio><track>
        <clipitem id="a-a"><name>rec</name>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>200</start><end>350</end><in>0</in><out>150</out></clipitem>
      </track></audio>
    </media>
  </sequence>
</xmeml>
"""


class TestCloseGaps:
    """Occupied spans here are [50,100] video, [200,350] audio, [300,400] video.

    So the timeline is busy over [50,100] and [200,400]: a 50-frame lead-in and
    a 100-frame hole are dead air, while [300,350] is *not* -- picture and sound
    overlap there.
    """

    def _sequence(self, tmp_path: Path):
        return fcp7.read(write(tmp_path, "gappy.xml", GAPPY)).sequence

    def test_removes_lead_in_and_interior_gap(self, tmp_path: Path) -> None:
        from syncmyvids.sync.layout import close_gaps

        sequence = self._sequence(tmp_path)
        removed, reclaimed = close_gaps(sequence)
        assert removed == 2
        assert reclaimed == 150  # 50 lead-in + 100 interior
        starts = {c.name: c.start for c in sequence.clips()}
        # Each clip loses every gap that lies wholly before it: "a" only the
        # 50-frame lead-in, "rec" and "b" both gaps (150 frames).
        assert starts == {"a": 0, "rec": 50, "b": 150}

    def test_relative_timing_is_preserved(self, tmp_path: Path) -> None:
        """Gaps never cut a clip, so everything sharing a moment shifts alike."""
        from syncmyvids.sync.layout import close_gaps

        sequence = self._sequence(tmp_path)
        before = {c.name: c.start for c in sequence.clips()}
        close_gaps(sequence)
        after = {c.name: c.start for c in sequence.clips()}
        # "rec" and "b" overlap in time; their separation must not change.
        assert after["b"] - after["rec"] == before["b"] - before["rec"]

    def test_audio_only_stretches_are_not_gaps(self, tmp_path: Path) -> None:
        """A region with sound but no picture is not dead air."""
        from syncmyvids.sync.layout import close_gaps

        sequence = self._sequence(tmp_path)
        close_gaps(sequence)
        starts = {c.name: c.start for c in sequence.clips()}
        # [200,300] carries only the recorder; it must survive intact.
        assert starts["b"] - starts["rec"] == 100

    def test_leaves_a_gapless_timeline_alone(self, tmp_path: Path) -> None:
        from syncmyvids.sync.layout import close_gaps

        sequence = self._sequence(tmp_path)
        close_gaps(sequence)
        assert close_gaps(sequence) == (0, 0)

    def test_sequence_duration_shrinks(self, tmp_path: Path) -> None:
        from syncmyvids.sync.layout import close_gaps

        sequence = self._sequence(tmp_path)
        close_gaps(sequence)
        assert sequence.duration == 250

    def test_disabled_by_default(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "gappy.xml", GAPPY))
        report = sync_timeline(timeline)
        assert report.gaps_removed == 0

    def test_option_leaves_no_dead_air(self, tmp_path: Path) -> None:
        """The contract is the outcome, not the count.

        With inline placement the orphans are already packed from zero, so a run
        may legitimately find nothing to remove; what must hold either way is
        that the finished timeline starts at zero and has no empty stretch.
        """
        timeline = fcp7.read(write(tmp_path, "gappy.xml", GAPPY))
        sync_timeline(timeline, SyncOptions(close_gaps=True))
        spans = sorted((c.start, c.end) for c in timeline.sequence.clips())
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        assert merged[0][0] == 0
        assert len(merged) == 1, "timeline still contains a stretch with nothing playing"

    def test_option_removes_a_real_gap(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "gappy.xml", GAPPY))
        report = sync_timeline(timeline, SyncOptions(
            close_gaps=True, unsynced_placement="append", unsynced_gap_seconds=4.0,
        ))
        assert report.gaps_removed > 0


class TestIslandOrdering:
    """Disconnected groups must be laid out in shooting order, not size order.

    A sequential shoot -- one camera, one recorder, take after take -- gives the
    solver several groups with nothing in the audio connecting them, so it
    cannot know their order.  The incoming timeline does: the operator dropped
    the footage on in the order it was shot.  Ordering by group size instead
    scrambled a real project into 0448, 0451, 0447, 0450, 0452, 0453.
    """

    def _timeline(self, tmp_path: Path) -> Path:
        # Two pairs that sync internally but not to each other.  The *small*
        # group is shot first, so size order and shooting order disagree.
        clips = [
            ("early1", "video", 1, 0, 100), ("early2", "audio", 1, 0, 100),
            ("late1", "video", 1, 200, 300), ("late2", "audio", 1, 200, 300),
            ("late3", "audio", 2, 200, 300),
        ]
        items = {"video": [], "audio": {1: [], 2: []}}
        for name, kind, track, start, end in clips:
            xml = (f'<clipitem id="{name}"><name>{name}</name>'
                   f'<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>'
                   f'<start>{start}</start><end>{end}</end>'
                   f'<in>0</in><out>{end - start}</out>'
                   f'<file id="f-{name}"><name>{name}.mov</name>'
                   f'<pathurl>file://localhost/nowhere/{name}.mov</pathurl></file></clipitem>')
            if kind == "video":
                items["video"].append(xml)
            else:
                items["audio"][track].append(xml)
        body = (f'<video><track>{"".join(items["video"])}</track></video>'
                f'<audio><track>{"".join(items["audio"][1])}</track>'
                f'<track>{"".join(items["audio"][2])}</track></audio>')
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
               '<xmeml version="5"><sequence id="s"><name>Order</name>'
               '<duration>400</duration>'
               '<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>'
               f'<media>{body}</media></sequence></xmeml>')
        return write(tmp_path, "order.xml", xml)

    def test_groups_keep_their_original_relative_order(self, tmp_path: Path) -> None:
        """Media is missing, so every clip is an orphan -- ordering still holds."""
        timeline = fcp7.read(self._timeline(tmp_path))
        report = sync_timeline(timeline, SyncOptions(unsynced_gap_seconds=0.0))
        order = [p["label"].split(":", 1)[1] for p in
                 sorted(report.placements, key=lambda p: p["start_frames"])]
        assert order.index("early1") < order.index("late1")


class TestReferenceSelection:
    def test_explicit_reference_is_honoured(self, fixture_timeline: Path, tmp_path: Path) -> None:
        source = tmp_path / "in.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(reference="V2"))
        assert report.reference_track == "V2"
        # The named track must not move.
        camb = next(p for p in report.placements if p["label"] == "V2:camB.mov")
        assert camb["shift_frames"] == 0

    def test_invalid_reference_label_is_rejected(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "in.xml", NO_MEDIA))
        with pytest.raises(ValueError, match="invalid track label"):
            sync_timeline(timeline, SyncOptions(reference="banana"))


EXPLODED = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="s" explodedTracks="true"><uuid>aaaa-bbbb</uuid><name>Ex</name>
    <duration>400</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video><track><clipitem id="v"><name>cam</name>
        <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
        <start>0</start><end>200</end><in>0</in><out>200</out>
        <file id="f"><name>cam.mov</name>
          <pathurl>file://localhost/nowhere/cam.mov</pathurl>
          <media><audio><channelcount>2</channelcount></audio></media></file>
        <link><linkclipref>v</linkclipref></link>
        <link><linkclipref>aL</linkclipref></link>
        <link><linkclipref>aR</linkclipref></link>
      </clipitem></track></video>
      <audio>
        <track><clipitem id="aL"><name>cam</name>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>0</start><end>200</end><in>0</in><out>200</out>
          <file id="f"/>
          <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
          <link><linkclipref>v</linkclipref></link><link><linkclipref>aL</linkclipref></link>
          <link><linkclipref>aR</linkclipref></link>
        </clipitem><enabled>TRUE</enabled><outputchannelindex>1</outputchannelindex></track>
        <track><clipitem id="aR"><name>cam</name>
          <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
          <start>0</start><end>200</end><in>0</in><out>200</out>
          <file id="f"/>
          <sourcetrack><mediatype>audio</mediatype><trackindex>2</trackindex></sourcetrack>
          <link><linkclipref>v</linkclipref></link><link><linkclipref>aL</linkclipref></link>
          <link><linkclipref>aR</linkclipref></link>
        </clipitem><enabled>TRUE</enabled><outputchannelindex>2</outputchannelindex></track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""


class TestUnsyncedAudioMerge:
    """Premiere exports stereo as two mono clipitems (explodedTracks="true").

    Merge mode keeps one of them to stand for the whole stereo stream, so an
    unmatched clip needs one new audio track rather than a pair.
    """

    def _run(self, tmp_path: Path, mode: str):
        timeline = fcp7.read(write(tmp_path, "ex.xml", EXPLODED))
        sync_timeline(timeline, SyncOptions(unsynced_audio=mode,
                                            unsynced_gap_seconds=0.0))
        return timeline.sequence

    def test_merge_makes_one_new_audio_track(self, tmp_path: Path) -> None:
        # Indices shift once empty tracks are pruned, so count what carries
        # clips rather than naming a track number.
        sequence = self._run(tmp_path, "merge")
        used = [t for t in sequence.audio_tracks if t.clips]
        assert len(used) == 1
        assert len(used[0].clips) == 1

    def test_merge_drops_no_routing_on_the_new_track(self, tmp_path: Path) -> None:
        """An outputchannelindex would pin it back to one channel."""
        sequence = self._run(tmp_path, "merge")
        track = next(t for t in sequence.audio_tracks if t.clips)
        assert track.element.find("outputchannelindex") is None

    def test_merge_keeps_channel_one(self, tmp_path: Path) -> None:
        sequence = self._run(tmp_path, "merge")
        clip = next(t for t in sequence.audio_tracks if t.clips).clips[0]
        assert clip.element.findtext("sourcetrack/trackindex") == "1"

    def test_merge_leaves_no_dangling_links(self, tmp_path: Path) -> None:
        """The discarded channel must not be referenced by anything."""
        sequence = self._run(tmp_path, "merge")
        known = {c.clip_id for c in sequence.clips()}
        for clip in sequence.clips():
            for link in clip.element.findall("link"):
                assert link.findtext("linkclipref") in known

    def test_merge_removes_the_second_channel_clipitem(self, tmp_path: Path) -> None:
        sequence = self._run(tmp_path, "merge")
        assert "aR" not in {c.clip_id for c in sequence.clips()}
        assert "aL" in {c.clip_id for c in sequence.clips()}

    def test_split_keeps_both_channels_and_routing(self, tmp_path: Path) -> None:
        sequence = self._run(tmp_path, "split")
        used = [t for t in sequence.audio_tracks if t.clips]
        assert len(used) == 2
        assert [t.element.findtext("outputchannelindex") for t in used] == ["1", "2"]
        assert {"aL", "aR"} <= {c.clip_id for c in sequence.clips()}

    def test_empty_tracks_are_pruned(self, tmp_path: Path) -> None:
        """Nothing should be left showing an empty column in the NLE."""
        sequence = self._run(tmp_path, "keep")
        assert all(t.clips for t in sequence.tracks)

    def test_pruning_can_be_turned_off(self, tmp_path: Path) -> None:
        timeline = fcp7.read(write(tmp_path, "ex.xml", EXPLODED))
        # "split" so a new track is certainly created; the point of the test is
        # that an empty one survives pruning, not how it came to exist.
        sync_timeline(timeline, SyncOptions(prune_empty_tracks=False,
                                            unsynced_gap_seconds=0.0,
                                            unsynced_video="split",
                                            unsynced_audio="split"))
        assert any(not t.clips for t in timeline.sequence.tracks)


class TestMultipleInputs:
    """A mix of timelines and folders on one command line."""

    def test_a_lone_timeline_still_takes_the_patching_path(self, tmp_path: Path) -> None:
        # The single-XML case must keep its tree: everything Premiere puts in
        # an export survives only because that document is edited, not rebuilt.
        source = write(tmp_path, "in.xml", MINIMAL)
        loaded = load_inputs([source])
        assert loaded.built is False
        assert loaded.timeline.source_path == source

    def test_an_empty_input_list_is_refused(self) -> None:
        with pytest.raises(SyncMyVidsError, match="no input"):
            load_inputs([])

    def test_an_actioncheese_export_points_at_the_folder(self, tmp_path: Path) -> None:
        # Its XML holds one sequence per take, so reading it would silently
        # keep only the longest take.
        shoot = tmp_path / "2026-09-27"
        shoot.mkdir()
        export = shoot / "ActionCheese 2026-09-27 – Premiere.xml"
        export.write_text(
            '<?xml version="1.0"?><xmeml version="5"><project>'
            "<name>ActionCheese 2026-09-27</name></project></xmeml>", encoding="utf-8")
        with pytest.raises(SyncMyVidsError, match="shoot folder"):
            load_inputs([export])

    def test_a_timeline_and_a_folder_are_combined(self, tmp_path: Path) -> None:
        source = write(tmp_path, "a.xml", NO_MEDIA)
        camera = tmp_path / "camera"
        camera.mkdir()
        loaded = load_inputs([source, camera])
        assert loaded.built is True, "more than one input has to be assembled"
        # Offline clips are carried through rather than dropped: a timeline
        # whose drive is unplugged must still come out the other end.
        assert sum(1 for _ in loaded.timeline.sequence.clips()) == 2
        assert any("not available" in w for w in loaded.warnings)

    def test_sync_accepts_several_inputs_on_the_command_line(self, tmp_path: Path) -> None:
        source = write(tmp_path, "a.xml", NO_MEDIA)
        camera = tmp_path / "camera"
        camera.mkdir()
        assert main(["sync", str(source), str(camera), "--dry-run"]) == 0

    def test_nothing_usable_is_a_clean_error(self, tmp_path: Path, capsys) -> None:
        source = write(tmp_path, "a.xml", MINIMAL)
        camera = tmp_path / "camera"
        camera.mkdir()
        assert main(["sync", str(source), str(camera), "--dry-run"]) == 1
        assert "usable media" in capsys.readouterr().err


class TestTheDefaultsAreRealValues:
    """Every numeric default must be a number the matcher can compare.

    SyncOptions is a slots dataclass, so reading a field off the *class*
    yields a member_descriptor rather than the default. Taking a CLI default
    from one shipped a parser whose --min-peak-ratio was that descriptor, and
    every real sync died on `peak_ratio >= min_peak_ratio`. Nothing caught it:
    the CLI tests all sync degenerate timelines where no pair is ever scored,
    so the matcher is never reached.
    """

    def defaults(self) -> dict:
        from syncmyvids.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["sync", "in.xml", "--dry-run"])
        return vars(args)

    def test_no_default_is_a_descriptor(self) -> None:
        import dataclasses

        for name, value in self.defaults().items():
            assert not isinstance(value, dataclasses.Field), name
            assert type(value).__name__ != "member_descriptor", (
                f"--{name.replace('_', '-')} defaults to a class attribute "
                f"read off a slots dataclass, not to a value")

    def test_the_numeric_defaults_are_numbers(self) -> None:
        for name in ("min_confidence", "min_peak_ratio", "min_overlap",
                     "unsynced_gap"):
            value = self.defaults()[name]
            assert isinstance(value, (int, float)), f"{name} is {value!r}"
            assert value == value, f"{name} is NaN"

    def test_the_cli_agrees_with_the_library(self) -> None:
        # Someone changing a default in one place should not have to remember
        # the other.
        from syncmyvids.engine import SyncOptions

        options = SyncOptions()
        got = self.defaults()
        assert got["min_peak_ratio"] == options.min_peak_ratio
        assert got["min_confidence"] == options.min_confidence

    def test_a_default_run_can_score_a_pair(self) -> None:
        # The call that actually broke.
        from syncmyvids.engine import SyncOptions
        from syncmyvids.sync.graph import is_acceptable

        options = SyncOptions(min_peak_ratio=self.defaults()["min_peak_ratio"])
        assert is_acceptable(0.9, 2.0, 100.0,
                             min_confidence=options.min_confidence,
                             min_peak_ratio=options.min_peak_ratio) is True
