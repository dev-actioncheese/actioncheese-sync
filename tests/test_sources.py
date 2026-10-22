# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a mix of timelines and camera folders."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from syncmyvids import engine, metadata, sources
from syncmyvids.errors import SyncMyVidsError
from syncmyvids.formats import fcp7_build
from syncmyvids.sync import graph, solve
from syncmyvids.timebase import Rate

from conftest import MEDIA_DIR, requires_ffmpeg

#: Seconds from the Unix epoch to Apple's, for building index fixtures the way
#: the app writes them.
APPLE = sources.APPLE_EPOCH_OFFSET


def camera_folder(root: Path, name: str, sources_and_names: list[tuple[str, str]]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for source, target in sources_and_names:
        shutil.copy(MEDIA_DIR / source, folder / target)
    return folder


class TestClassify:
    def test_a_timeline_is_recognised_by_its_root_element(self, tmp_path: Path) -> None:
        path = tmp_path / "t.xml"
        path.write_text('<?xml version="1.0"?><xmeml version="5"/>', encoding="utf-8")
        assert sources.classify(path) == "timeline"

    def test_a_plain_folder_is_a_camera(self, tmp_path: Path) -> None:
        (tmp_path / "cam").mkdir()
        assert sources.classify(tmp_path / "cam") == "camera"

    def test_a_shoot_index_makes_it_an_actioncheese_folder(self, tmp_path: Path) -> None:
        folder = tmp_path / "2026-09-27"
        folder.mkdir()
        (folder / sources.SHOOT_INDEX).write_text("{}", encoding="utf-8")
        assert sources.classify(folder) == "actioncheese"

    def test_a_sony_sidecar_says_to_pass_the_folder(self, tmp_path: Path) -> None:
        # Half the files on a Sony card are .XML, so pointing at one is an easy
        # mistake and deserves a better answer than "not a timeline".
        path = tmp_path / "C6205M01.XML"
        path.write_text('<?xml version="1.0"?><NonRealTimeMeta xmlns="urn:x"/>',
                        encoding="utf-8")
        with pytest.raises(SyncMyVidsError, match="Sony clip sidecar"):
            sources.classify(path)

    def test_fcpxml_is_named_as_the_wrong_format(self, tmp_path: Path) -> None:
        path = tmp_path / "t.fcpxml"
        path.write_text('<?xml version="1.0"?><fcpxml version="1.11"/>', encoding="utf-8")
        with pytest.raises(SyncMyVidsError, match="not supported"):
            sources.classify(path)

    def test_a_missing_path_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(SyncMyVidsError, match="no such file"):
            sources.classify(tmp_path / "nope.xml")


class TestDeviceNaming:
    def test_a_card_folder_takes_its_parent_s_name(self, tmp_path: Path) -> None:
        # Five Sony cards all have their footage in a folder called CLIP.
        folder = tmp_path / "wedding-cam-2" / "CLIP"
        folder.mkdir(parents=True)
        assert sources._device_name(folder) == "wedding-cam-2"

    def test_a_named_folder_keeps_its_name(self, tmp_path: Path) -> None:
        folder = tmp_path / "sony-camera-1"
        folder.mkdir()
        assert sources._device_name(folder) == "sony-camera-1"


@requires_ffmpeg
class TestCameraFolder:
    def test_clips_are_read_and_ordered(self, fixture_timeline: Path, tmp_path: Path) -> None:
        folder = camera_folder(tmp_path, "cam-1",
                               [("camA.mov", "b.mov"), ("broll.mov", "a.mov")])
        device, _ = sources.read_camera_folder(folder)
        assert device.name == "cam-1"
        assert len(device.clips) == 2
        assert not device.rigid

    def test_stills_are_skipped_and_reported(self, fixture_timeline: Path,
                                             tmp_path: Path) -> None:
        folder = camera_folder(tmp_path, "cam-1", [("camA.mov", "a.mov")])
        (folder / "DSC001.jpg").write_bytes(b"not really a jpeg")
        device, warnings = sources.read_camera_folder(folder)
        assert len(device.clips) == 1
        assert any("still image" in w for w in warnings)

    def test_media_is_found_however_the_card_nests_it(self, fixture_timeline: Path,
                                                      tmp_path: Path) -> None:
        deep = tmp_path / "card" / "PRIVATE" / "M4ROOT" / "CLIP"
        deep.mkdir(parents=True)
        shutil.copy(MEDIA_DIR / "camA.mov", deep / "C0001.MP4")
        device, _ = sources.read_camera_folder(tmp_path / "card")
        assert [c.name for c in device.clips] == ["C0001.MP4"]


@requires_ffmpeg
class TestActionCheeseFolder:
    def shoot(self, tmp_path: Path) -> Path:
        folder = tmp_path / "2026-09-27"
        folder.mkdir()
        shutil.copy(MEDIA_DIR / "camA.mov", folder / "Take 01 - phone one.mov")
        shutil.copy(MEDIA_DIR / "camB.mov", folder / "Take 01 - phone two.mov")
        index = {"clips": [
            {"camera": "phone one", "file": "Take 01 - phone one.mov",
             "start": 1000.0 - APPLE, "duration": 12.0, "isVideo": True, "frameRate": 30},
            {"camera": "phone two", "file": "Take 01 - phone two.mov",
             "start": 1002.5 - APPLE, "duration": 12.0, "isVideo": True, "frameRate": 30},
        ]}
        (folder / sources.SHOOT_INDEX).write_text(json.dumps(index), encoding="utf-8")
        return folder

    def test_each_camera_becomes_a_device_in_one_group(self, fixture_timeline: Path,
                                                       tmp_path: Path) -> None:
        devices, _, _ = sources.read_actioncheese_folder(self.shoot(tmp_path))
        assert [d.name for d in devices] == ["phone one", "phone two"]
        assert all(d.rigid for d in devices)
        assert len({d.group for d in devices}) == 1, "one shoot is one group"

    def test_the_index_supplies_the_start_times(self, fixture_timeline: Path,
                                                tmp_path: Path) -> None:
        devices, _, _ = sources.read_actioncheese_folder(self.shoot(tmp_path))
        starts = {d.name: d.clips[0].started for d in devices}
        assert starts["phone two"] - starts["phone one"] == pytest.approx(2.5)

    def test_a_camera_folder_inside_the_shoot_is_its_own_device(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        # Dropping the cards next to the phone footage is the obvious thing to
        # do with a shoot folder, so it has to work.
        folder = self.shoot(tmp_path)
        camera_folder(folder, "sony-camera-1", [("broll.mov", "C0001.MP4")])
        devices, _, _ = sources.read_actioncheese_folder(folder)
        by_name = {d.name: d for d in devices}
        assert "sony-camera-1" in by_name
        assert not by_name["sony-camera-1"].rigid, "a card has its own clock"

    def test_actioncheese_s_own_exports_are_not_mistaken_for_cameras(
            self, fixture_timeline: Path, tmp_path: Path) -> None:
        folder = self.shoot(tmp_path)
        (folder / "XML per take").mkdir()
        devices, _, _ = sources.read_actioncheese_folder(folder)
        assert [d.name for d in devices] == ["phone one", "phone two"]


@requires_ffmpeg
class TestIngest:
    def test_one_file_reached_by_two_inputs_is_used_once(self, fixture_timeline: Path,
                                                         tmp_path: Path) -> None:
        card = camera_folder(tmp_path, "card", [("camA.mov", "a.mov")])
        result = sources.ingest([tmp_path, card])
        paths = [c.path for d in result.devices for c in d.clips]
        assert len(paths) == len(set(paths)) == 1
        assert any("more than one input" in w for w in result.warnings)

    def test_devices_with_the_same_name_are_told_apart(self, fixture_timeline: Path,
                                                       tmp_path: Path) -> None:
        first = tmp_path / "one" / "CLIP"
        second = tmp_path / "one" / "b" / "CLIP"
        for folder, media in ((first, "camA.mov"), (second, "camB.mov")):
            folder.mkdir(parents=True)
            shutil.copy(MEDIA_DIR / media, folder / "c.mov")
        result = sources.ingest([first, second])
        assert len({d.name for d in result.devices}) == len(result.devices)


class TestCommonRate:
    def test_the_rate_most_of_the_footage_uses_wins(self) -> None:
        def clip(fps: float, seconds: float):
            info = metadata.MediaInfo(path=Path("x"), duration=seconds,
                                      has_video=True, frame_rate=fps)
            return sources.SourceClip(path=Path("x"), name="x", duration=seconds, info=info)

        devices = [sources.Device(name="a", clips=[clip(25.0, 600.0)]),
                   sources.Device(name="b", clips=[clip(50.0, 60.0)])]
        assert sources.common_rate(devices) == Rate(25, False)

    def test_ntsc_rates_are_recognised(self) -> None:
        info = metadata.MediaInfo(path=Path("x"), duration=10.0, has_video=True,
                                  frame_rate=30000 / 1001)
        device = sources.Device(name="a", clips=[
            sources.SourceClip(path=Path("x"), name="x", duration=10.0, info=info)])
        assert sources.common_rate([device]) == Rate(30, True)

    def test_audio_only_devices_fall_back(self) -> None:
        info = metadata.MediaInfo(path=Path("x"), duration=10.0, has_audio=True)
        device = sources.Device(name="a", clips=[
            sources.SourceClip(path=Path("x"), name="x", duration=10.0, info=info)])
        assert sources.common_rate([device], fallback=Rate(24, False)) == Rate(24, False)


@requires_ffmpeg
class TestBuild:
    def devices(self, tmp_path: Path) -> list[sources.Device]:
        first = camera_folder(tmp_path, "cam-a", [("camA.mov", "a.mov")])
        second = camera_folder(tmp_path, "cam-b", [("camB.mov", "b.mov")])
        return [sources.read_camera_folder(first)[0], sources.read_camera_folder(second)[0]]

    def test_the_built_timeline_reads_back(self, fixture_timeline: Path,
                                           tmp_path: Path) -> None:
        built = fcp7_build.build(self.devices(tmp_path), rate=Rate(30, False),
                                 out_path=tmp_path / "built.xml")
        sequence = built.timeline.sequence
        assert len(sequence.video_tracks) == 2
        assert len(sequence.audio_tracks) == 2
        assert sequence.duration > 0

    def test_one_device_gets_one_track_pair(self, fixture_timeline: Path,
                                            tmp_path: Path) -> None:
        # The old exploded-stereo layout is what used to split unmatched clips
        # across two audio tracks; a built timeline never does that.
        built = fcp7_build.build(self.devices(tmp_path), rate=Rate(30, False),
                                 out_path=tmp_path / "built.xml")
        for track in built.timeline.sequence.audio_tracks:
            assert len(track.clips) == 1

    def test_picture_and_sound_are_linked(self, fixture_timeline: Path,
                                          tmp_path: Path) -> None:
        from syncmyvids.model import build_sync_units

        built = fcp7_build.build(self.devices(tmp_path), rate=Rate(30, False),
                                 out_path=tmp_path / "built.xml")
        units = build_sync_units(built.timeline.sequence)
        assert len(units) == 2, "a clip's picture and sound are one unit"
        assert all(len(u.clips) == 2 for u in units)

    def test_a_shoot_s_clips_are_reported_as_one_group(self, fixture_timeline: Path,
                                                       tmp_path: Path) -> None:
        folder = tmp_path / "shoot"
        folder.mkdir()
        shutil.copy(MEDIA_DIR / "camA.mov", folder / "one.mov")
        (folder / sources.SHOOT_INDEX).write_text(json.dumps({"clips": [
            {"camera": "phone", "file": "one.mov", "start": 0.0, "duration": 12.0,
             "isVideo": True, "frameRate": 30}]}), encoding="utf-8")
        devices, _, _ = sources.read_actioncheese_folder(folder)
        built = fcp7_build.build(devices, rate=Rate(30, False),
                                 out_path=tmp_path / "built.xml")
        groups = built.rigid_clip_ids
        assert len(groups) == 1
        assert len(next(iter(groups.values()))) == 2, "video and audio clipitems"


class TestStatedRelationships:
    def group(self) -> list[dict[int, float]]:
        return [{0: 0.0, 1: 2.5, 2: 5.0}]

    def test_members_are_chained_not_fully_connected(self) -> None:
        edges = engine._stated_edges(self.group(), trust=False)
        assert len(edges) == 2, "n-1 edges relate n units"

    def test_a_prior_loses_to_a_confident_measurement(self) -> None:
        prior = engine._stated_edges(self.group(), trust=False)[0]
        measured = graph.Edge(a=0, b=1, delta_seconds=2.4, confidence=0.75,
                              peak_ratio=2.0, overlap_seconds=60.0)
        assert measured.weight > prior.weight

    def test_a_prior_beats_nothing_at_all(self) -> None:
        prior = engine._stated_edges(self.group(), trust=False)[0]
        assert prior.weight > 0.0
        assert prior.stated and not prior.rigid

    def test_trusting_makes_it_outweigh_any_measurement(self) -> None:
        rigid = engine._stated_edges(self.group(), trust=True)[0]
        best_possible = graph.Edge(a=0, b=1, delta_seconds=2.4, confidence=1.0,
                                   peak_ratio=9.0, overlap_seconds=10_000.0)
        assert rigid.weight > best_possible.weight * 1000
        assert rigid.rigid

    def test_snapping_restores_the_stated_geometry_exactly(self) -> None:
        component = solve.Component(nodes=[0, 1, 2],
                                    positions={0: 10.001, 1: 12.498, 2: 15.004})
        solution = solve.Solution(components=[component], conflicts=[], singletons=[])
        assert solve.snap_rigid(solution, self.group()) == 1
        placed = component.positions
        assert placed[1] - placed[0] == pytest.approx(2.5, abs=1e-12)
        assert placed[2] - placed[1] == pytest.approx(2.5, abs=1e-12)

    def test_snapping_keeps_the_group_where_the_solve_put_it(self) -> None:
        component = solve.Component(nodes=[0, 1, 2],
                                    positions={0: 10.0, 1: 12.5, 2: 15.0})
        solution = solve.Solution(components=[component], conflicts=[], singletons=[])
        solve.snap_rigid(solution, self.group())
        # Mean position preserved: snapping fixes shape, not location.
        assert sum(component.positions.values()) / 3 == pytest.approx(12.5)


class TestOverlapRule:
    def test_short_clips_are_asked_for_a_fraction_of_themselves(self) -> None:
        # A 17 s take can never provide 20 s of overlap. Before this rule such
        # a shoot matched nothing at all.
        shorter = 17.0
        required = min(20.0, graph.OVERLAP_FRACTION * shorter)
        assert max(required, graph.OVERLAP_FLOOR_SECONDS) < shorter

    def test_long_clips_still_owe_the_full_twenty_seconds(self) -> None:
        required = min(20.0, graph.OVERLAP_FRACTION * 600.0)
        assert required == 20.0

    def test_very_short_clips_are_refused_outright(self) -> None:
        required = max(min(20.0, graph.OVERLAP_FRACTION * 4.0),
                       graph.OVERLAP_FLOOR_SECONDS)
        assert required > 4.0, "a 4 s clip cannot satisfy the floor, so never matches"


class TestBrokenClocks:
    def clip(self, started: float, name: str) -> sources.SourceClip:
        info = metadata.MediaInfo(path=Path(name), duration=10.0, has_video=True,
                                  frame_rate=25.0, started=started)
        return sources.SourceClip(path=Path(name), name=name, duration=10.0,
                                  info=info, started=started)

    def test_a_dead_clock_battery_is_not_believed(self) -> None:
        # A camera that stamps 1970 would otherwise stretch the built timeline
        # across five decades and squeeze the real material into one pixel.
        now = 1_790_000_000.0
        good = sources.Device(name="cam-a", clips=[
            self.clip(now, "a"), self.clip(now + 60, "b"), self.clip(now + 120, "c")])
        broken = sources.Device(name="cam-b", clips=[self.clip(0.0, "d")])
        result = sources.Ingest(devices=[good, broken])
        sources._drop_impossible_clocks(result)
        assert broken.clips[0].started is None
        assert [c.started for c in good.clips] == [now, now + 60, now + 120]
        assert any("clock is wrong" in w for w in result.warnings)

    def test_a_shoot_spanning_days_is_left_alone(self) -> None:
        now = 1_790_000_000.0
        device = sources.Device(name="cam", clips=[
            self.clip(now, "a"), self.clip(now + 86400, "b"), self.clip(now + 3 * 86400, "c")])
        result = sources.Ingest(devices=[device])
        sources._drop_impossible_clocks(result)
        assert all(c.started is not None for c in device.clips)
        assert result.warnings == []


@requires_ffmpeg
class TestSplittingAJobFolder:
    def test_a_card_next_to_a_recorder_becomes_two_devices(self, fixture_timeline: Path,
                                                           tmp_path: Path) -> None:
        # Read as one device these could never match each other, because a
        # device is never matched against itself.
        job = tmp_path / "wedding"
        camera_folder(job, "CLIP", [("camA.mov", "C0001.MP4")])
        camera_folder(job, "MUSIC", [("recorder.wav", "ZOOM0001.wav")])
        assert {p.name for p in sources.split_into_devices(job)} == {"CLIP", "MUSIC"}

    def test_a_single_nested_chain_stays_one_device(self, fixture_timeline: Path,
                                                    tmp_path: Path) -> None:
        deep = tmp_path / "card" / "PRIVATE" / "M4ROOT" / "CLIP"
        deep.mkdir(parents=True)
        shutil.copy(MEDIA_DIR / "camA.mov", deep / "C0001.MP4")
        assert sources.split_into_devices(tmp_path / "card") == [tmp_path / "card"]

    def test_loose_media_in_the_folder_keeps_it_whole(self, fixture_timeline: Path,
                                                      tmp_path: Path) -> None:
        # Files sitting directly in the folder belong to it, so it is a device
        # in its own right and its subfolders are part of it.
        job = tmp_path / "shoot"
        camera_folder(job, "a", [("camA.mov", "one.mov")])
        camera_folder(job, "b", [("camB.mov", "two.mov")])
        shutil.copy(MEDIA_DIR / "broll.mov", job / "loose.mov")
        assert sources.split_into_devices(job) == [job]

    def test_ingest_reports_the_split(self, fixture_timeline: Path, tmp_path: Path) -> None:
        job = tmp_path / "wedding"
        camera_folder(job, "CLIP", [("camA.mov", "C0001.MP4")])
        camera_folder(job, "MUSIC", [("recorder.wav", "ZOOM0001.wav")])
        result = sources.ingest([job])
        # CLIP and MUSIC mean nothing on their own, and the usual "walk up to a
        # real name" rule would call both of them "wedding".
        assert {d.name for d in result.devices} == {"wedding CLIP", "wedding MUSIC"}
        assert any("read as 2 devices" in w for w in result.warnings)

    def test_a_job_folder_inside_a_shoot_splits_too(self, fixture_timeline: Path,
                                                    tmp_path: Path) -> None:
        shoot = tmp_path / "2026-09-27"
        shoot.mkdir()
        shutil.copy(MEDIA_DIR / "camA.mov", shoot / "Take 01 - phone.mov")
        (shoot / sources.SHOOT_INDEX).write_text(json.dumps({"clips": [
            {"camera": "phone", "file": "Take 01 - phone.mov", "start": 0.0,
             "duration": 12.0, "isVideo": True, "frameRate": 30}]}), encoding="utf-8")
        job = shoot / "wedding"
        camera_folder(job, "CLIP", [("camB.mov", "C0001.MP4")])
        camera_folder(job, "MUSIC", [("recorder.wav", "ZOOM0001.wav")])
        devices, _, _ = sources.read_actioncheese_folder(shoot)
        assert {d.name for d in devices} == {"phone", "wedding CLIP", "wedding MUSIC"}
