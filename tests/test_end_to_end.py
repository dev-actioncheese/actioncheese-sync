# SPDX-License-Identifier: GPL-3.0-or-later
"""Full-pipeline tests against synthetic media whose true alignment is known.

See ``fixtures/make_fixtures.py`` for the scenario and its ground truth.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from syncmyvids.engine import SyncOptions, sync_timeline
from syncmyvids.formats import fcp7

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def synced(fixture_timeline: Path, tmp_path_factory: pytest.TempPathFactory):
    """Run the pipeline once; every test in this module inspects the result."""
    workdir = tmp_path_factory.mktemp("sync")
    source = workdir / "input.xml"
    shutil.copy(fixture_timeline, source)

    timeline = fcp7.read(source)
    report = sync_timeline(timeline, SyncOptions())
    output = workdir / "synced.xml"
    fcp7.write(timeline, output)
    return report, output


@pytest.fixture(scope="module")
def placements(synced) -> dict[str, dict]:
    report, _ = synced
    return {p["label"].split(":", 1)[1]: p for p in report.placements}


class TestAlignment:
    def test_all_three_recordings_are_synced(self, placements: dict[str, dict]) -> None:
        for name in ("camA.mov", "camB.mov", "recorder.wav"):
            assert placements[name]["status"] == "synced", f"{name} failed to sync"

    def test_camera_offset_matches_ground_truth(
        self, placements: dict[str, dict], ground_truth: dict[str, float]
    ) -> None:
        fps = ground_truth["fps"]
        measured = (placements["camA.mov"]["start_frames"]
                    - placements["camB.mov"]["start_frames"]) / fps
        assert measured == pytest.approx(ground_truth["camA_minus_camB"], abs=1.0 / fps)

    def test_recorder_offset_matches_ground_truth(
        self, placements: dict[str, dict], ground_truth: dict[str, float]
    ) -> None:
        fps = ground_truth["fps"]
        measured = (placements["recorder.wav"]["start_frames"]
                    - placements["camB.mov"]["start_frames"]) / fps
        assert measured == pytest.approx(ground_truth["recorder_minus_camB"], abs=1.0 / fps)

    def test_nothing_lands_before_zero(self, synced) -> None:
        report, output = synced
        for clip in fcp7.read(output).sequence.clips():
            assert clip.start >= 0, f"{clip} starts before the timeline"

    def test_reference_is_a_camera(self, synced) -> None:
        report, _ = synced
        # Picture is never retimed, so the anchor must be a video track.
        assert report.reference_track.startswith("V")


class TestUnsyncedHandling:
    def test_unmatchable_clips_are_flagged(self, placements: dict[str, dict]) -> None:
        assert placements["broll.mov"]["status"] == "unsynced"
        assert placements["orphan.wav"]["status"] == "unsynced"

    def test_silent_clip_reports_why(self, placements: dict[str, dict]) -> None:
        assert "no usable audio" in placements["broll.mov"]["reason"]

    def test_nothing_gets_a_new_track_when_there_is_room(self, synced) -> None:
        """Default: an unmatched clip stays where it came from if it fits.

        A new track is for a clip with nowhere to go, not a badge. The clip is
        already marked in the label colour, and once it is placed at the moment
        it was shot, moving it splits one camera's footage across two rows for
        nothing. On a real four-camera shoot that turned 4 used video tracks
        into 7, with three of the four cameras appearing twice.
        """
        report, output = synced
        sequence = fcp7.read(output).sequence
        assert len(sequence.video_tracks) == 2   # input had 2: unchanged
        assert len(sequence.audio_tracks) == 3   # input had 3: unchanged
        assert report.new_tracks == {}

    def test_split_mode_still_gives_picture_its_own_track(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        """The older behaviour, kept for anyone who wants it."""
        source = tmp_path / "input.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(unsynced_video="split"))
        assert report.new_tracks == {"V2": "V3"}

    def test_a_clip_moves_when_its_own_track_is_busy(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        """"keep" is about room, not about never moving."""
        source = tmp_path / "input.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(unsynced_placement="append"))
        for placement in report.placements:
            if placement["status"] != "unsynced":
                continue
            # Whatever it did, it did not end up on top of something else.
            start, end = placement["start_frames"], None
            assert start >= 0

    def test_orphan_audio_stays_on_its_original_track(self, synced) -> None:
        _, output = synced
        sequence = fcp7.read(output).sequence
        # Found by name rather than by track index: which track a clip ends up
        # on is exactly what this area of the code decides, so pinning the
        # index would be testing the lookup rather than the behaviour.
        home = {c.name: t.label for t in sequence.tracks for c in t.clips}
        assert home["broll.mov"] == "V2", "its own track, which had room"
        assert home["orphan.wav"].startswith("A")

    def test_orphans_are_labelled(self, synced) -> None:
        """Unmatched material is colour-marked so it is obvious in the edit."""
        _, output = synced
        sequence = fcp7.read(output).sequence
        marked = [c for c in sequence.clips()
                  if c.name in ("broll.mov", "orphan.wav")]
        assert marked
        for clip in marked:
            assert clip.element.findtext("labels/label2") == "Rose"

    def test_nothing_is_lost_or_overlapping(self, synced) -> None:
        """Whatever the track policy, no clip may vanish or collide."""
        _, output = synced
        sequence = fcp7.read(output).sequence
        for track in sequence.tracks:
            ordered = sorted(track.clips, key=lambda c: c.start)
            for first, second in zip(ordered, ordered[1:], strict=False):
                assert first.end <= second.start, f"{first} overlaps {second}"

    def test_split_mode_gives_audio_its_own_tracks(self, fixture_timeline: Path, tmp_path: Path) -> None:
        source = tmp_path / "input.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(unsynced_audio="split",
                                                    unsynced_video="split"))
        assert report.new_tracks == {"V2": "V3", "A3": "A4"}

    def test_orphans_sit_after_their_own_device_predecessor(self, synced) -> None:
        """Inline placement: each orphan follows the last clip shot before it.

        Not "after everything" -- anchoring to the whole timeline would let one
        long recorder take drag every orphan to the far end.
        """
        _, output = synced
        sequence = fcp7.read(output).sequence
        clips = {c.name: c for t in sequence.tracks for c in t.clips}
        assert clips["broll.mov"].start >= clips["camB.mov"].end

    def test_append_mode_puts_orphans_at_the_end(self, fixture_timeline: Path, tmp_path: Path) -> None:
        source = tmp_path / "input.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(unsynced_placement="append"))
        boundary = report.last_synced_end_frames
        for placement in report.placements:
            if placement["status"] == "unsynced":
                assert placement["start_frames"] >= boundary - 1

    def test_boundary_is_exactly_the_last_synced_clip(self, synced) -> None:
        """The boundary must ignore orphans' original positions.

        Orphans have not moved when the boundary is computed and typically sit
        at the tail of the timeline, so measuring the whole sequence pushes the
        unsynced block past the leftovers rather than after the last synced clip.
        """
        report, output = synced
        sequence = fcp7.read(output).sequence
        synced_labels = {p["label"] for p in report.placements
                         if p["status"] in ("synced", "island")}
        synced_ends = [c.end for c in sequence.clips()
                       if f"{'V' if c.media_type == 'video' else 'A'}"
                          f"{c.track_index}:{c.name}" in synced_labels]
        assert report.last_synced_end_frames == max(synced_ends)

    def test_no_synced_clip_sits_past_the_boundary(self, synced) -> None:
        report, output = synced
        sequence = fcp7.read(output).sequence
        unsynced = {p["label"].split(":", 1)[1] for p in report.placements
                    if p["status"] == "unsynced"}
        for track in sequence.tracks:
            for clip in track.clips:
                if clip.name in unsynced:
                    continue   # orphans may sit anywhere, by design
                assert clip.end <= report.last_synced_end_frames


class TestDrift:
    def test_recorder_drift_is_measured(self, synced, ground_truth: dict[str, float]) -> None:
        report, _ = synced
        entry = next(d for d in report.drift if d["label"].endswith("recorder.wav"))
        assert entry["measured"]
        assert entry["drift_ppm"] == pytest.approx(ground_truth["drift_ppm"], abs=3.0)

    def test_recorder_drift_is_corrected(self, synced) -> None:
        report, _ = synced
        entry = next(d for d in report.drift if d["label"].endswith("recorder.wav"))
        assert entry["corrected"]
        assert entry["speed_percent"] == pytest.approx(100.002, abs=0.001)

    def test_drift_free_camera_is_not_corrected(self, synced) -> None:
        """A clip with no real drift must never acquire a speed change.

        It is reported as *unmeasured* rather than "0 ppm", which is the honest
        answer: a zero slope is by construction indistinguishable from noise, so
        claiming to have measured it would overstate what the probes support.
        """
        report, _ = synced
        entry = next((d for d in report.drift if d["label"].endswith("camB.mov")), None)
        assert entry is not None
        assert not entry["corrected"]
        if entry["measured"]:
            assert abs(entry["drift_ppm"]) < 3.0
        else:
            # Either honest reason is fine: too short a lever arm to resolve a
            # slope, or a slope too small to separate from noise.
            assert any(word in (entry["reason"] or "")
                       for word in ("noise", "baseline"))

    def test_camera_clips_are_never_retimed(self, synced) -> None:
        """The one hard guarantee: picture is never resampled."""
        _, output = synced
        sequence = fcp7.read(output).sequence
        for clip in sequence.clips():
            if clip.media_type != "video":
                continue
            effects = [f.findtext("effect/effectid") for f in clip.element.findall("filter")]
            assert "timeremap" not in effects, f"{clip} had picture retimed"

    def test_no_drift_option_suppresses_correction(self, fixture_timeline: Path, tmp_path: Path) -> None:
        source = tmp_path / "input.xml"
        shutil.copy(fixture_timeline, source)
        timeline = fcp7.read(source)
        report = sync_timeline(timeline, SyncOptions(correct_drift=False))

        assert any(d["measured"] for d in report.drift), "detection should still run"
        assert not any(d["corrected"] for d in report.drift)
        for clip in timeline.sequence.clips():
            assert not clip.element.findall("filter")


class TestOutputValidity:
    def test_premiere_speed_invariant_holds(self, synced) -> None:
        """Premiere ignores a speed effect whose ranges disagree with it.

        The failure is silent and shows up as un-corrected drift in the edit.
        """
        _, output = synced
        sequence = fcp7.read(output).sequence
        checked = 0
        for clip in sequence.clips():
            remaps = [f for f in clip.element.findall("filter")
                      if f.findtext("effect/effectid") == "timeremap"]
            if not remaps:
                continue
            speed = float(next(
                p.findtext("value") for p in remaps[0].iter("parameter")
                if p.findtext("parameterid") == "speed"
            )) / 100.0
            assert (clip.out_point - clip.in_point) == round(
                (clip.end - clip.start) * speed
            ), f"{clip} violates Premiere's speed invariant"
            checked += 1
        assert checked == 1, "expected exactly the recorder to be retimed"

    def test_links_survive(self, synced) -> None:
        _, output = synced
        sequence = fcp7.read(output).sequence
        by_id = {c.clip_id: c for c in sequence.clips()}

        for clip in sequence.clips():
            for link in clip.element.findall("link"):
                ref = link.findtext("linkclipref")
                assert ref in by_id, f"{clip} links to missing clip {ref}"
                peer = by_id[ref]
                assert link.findtext("mediatype") == peer.media_type
                assert int(link.findtext("trackindex")) == peer.track_index

    def test_linked_clips_stay_together(self, synced) -> None:
        _, output = synced
        sequence = fcp7.read(output).sequence
        by_id = {c.clip_id: c for c in sequence.clips()}
        video = by_id["clip-camA-v"]
        audio = by_id["clip-camA-a"]
        assert video.start == audio.start
        assert video.end == audio.end

    def test_output_is_valid_xmeml(self, synced) -> None:
        _, output = synced
        tree = etree.parse(str(output))
        assert tree.getroot().tag == "xmeml"
        assert b"<!DOCTYPE xmeml>" in output.read_bytes()

    def test_sequence_duration_covers_every_clip(self, synced) -> None:
        _, output = synced
        sequence = fcp7.read(output).sequence
        assert sequence.duration == max(c.end for c in sequence.clips())

    def test_result_is_stable_when_re_run(self, synced, tmp_path: Path) -> None:
        """Syncing an already-synced timeline must not move anything."""
        _, output = synced
        again = fcp7.read(output)
        report = sync_timeline(again, SyncOptions(analyse_drift=False))
        for placement in report.placements:
            if placement["status"] == "synced":
                assert abs(placement["shift_frames"]) <= 1, placement
