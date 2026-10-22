# SPDX-License-Identifier: GPL-3.0-or-later
"""The timeline strip shown in the desktop app."""

from __future__ import annotations

import pytest

from syncmyvids.model import Clip, Sequence, Track
from syncmyvids.timebase import Rate
from syncmyvids.timelineview import (
    COLOUR_AUDIO,
    COLOUR_UNSYNCED,
    COLOUR_VIDEO,
    GUTTER,
    layout_rects,
    snapshot,
)

RATE = Rate(25, False)


def clip(name: str, start: int, end: int, media_type: str = "video",
         track: int = 1, clip_id: str | None = None) -> Clip:
    return Clip(clip_id=clip_id or name, name=name, media_type=media_type,
                track_index=track, start=start, end=end, in_point=0,
                out_point=end - start, rate=RATE, file=None)


def sequence(*tracks: Track) -> Sequence:
    video = [t for t in tracks if t.media_type == "video"]
    audio = [t for t in tracks if t.media_type == "audio"]
    duration = max((c.end for t in tracks for c in t.clips), default=0)
    return Sequence(name="S", rate=RATE, duration=duration,
                    video_tracks=video, audio_tracks=audio)


class TestSnapshot:
    """Rows are per *device*, not per track.

    A camera's own audio is part of the same clip, so drawing it as its own row
    just repeats the row above; a stereo recorder is one recording, not two.
    """

    def test_hides_empty_tracks(self) -> None:
        seq = sequence(
            Track("video", 1, [clip("a", 0, 100)]),
            Track("video", 2, []),
        )
        assert [t.label for t in snapshot(seq).tracks] == ["V1"]

    def test_camera_audio_is_not_a_row_of_its_own(self) -> None:
        video = clip("cam", 0, 100, clip_id="v")
        left = clip("cam", 0, 100, "audio", 1, "aL")
        right = clip("cam", 0, 100, "audio", 2, "aR")
        for c in (video, left, right):
            c.link_refs = ["v", "aL", "aR"]
        snap = snapshot(sequence(
            Track("video", 1, [video]),
            Track("audio", 1, [left]),
            Track("audio", 2, [right]),
        ))
        assert [t.label for t in snap.tracks] == ["V1"]
        assert snap.clip_count == 1

    def test_stereo_recorder_collapses_to_one_row(self) -> None:
        left = clip("rec.wav", 0, 100, "audio", 1, "L")
        right = clip("rec.wav", 0, 100, "audio", 2, "R")
        for c in (left, right):
            c.link_refs = ["L", "R"]
        snap = snapshot(sequence(
            Track("audio", 1, [left]), Track("audio", 2, [right])))
        assert [t.label for t in snap.tracks] == ["A1"]
        assert snap.clip_count == 1

    def test_video_rows_come_before_audio_rows(self) -> None:
        snap = snapshot(sequence(
            Track("audio", 3, [clip("rec", 0, 50, "audio", 3, "r")]),
            Track("video", 1, [clip("cam", 0, 50, clip_id="c")]),
        ))
        assert [t.label for t in snap.tracks] == ["V1", "A3"]

    def test_clip_carries_its_media_for_the_waveform(self) -> None:
        snap = snapshot(sequence(Track("video", 1, [clip("a", 0, 100)])))
        box = snap.tracks[0].clips[0]
        assert box.peaks == ()          # not loaded yet
        assert box.source_duration is not None

    def test_marks_unsynced_by_clip_id_not_by_name(self) -> None:
        """An unmatched clip has usually moved track by the time this runs.

        Matching on "V1:name" would silently stop marking anything the layout
        relocated -- which is every unmatched clip.
        """
        seq = sequence(Track("video", 4, [clip("orphan", 0, 50, clip_id="x1")]))
        snap = snapshot(seq, {"x1"})
        assert snap.tracks[0].clips[0].unsynced

        untouched = snapshot(seq, {"V1:orphan"})
        assert not untouched.tracks[0].clips[0].unsynced

    def test_duration_covers_the_last_clip(self) -> None:
        seq = sequence(Track("video", 1, [clip("a", 0, 10), clip("b", 900, 1000)]))
        assert snapshot(seq).duration == 1000

    def test_is_a_copy_not_a_live_view(self) -> None:
        """It is handed across threads while the engine moves the real clips."""
        moving = clip("a", 0, 100)
        seq = sequence(Track("video", 1, [moving]))
        snap = snapshot(seq)
        moving.start, moving.end = 500, 600
        assert snap.tracks[0].clips[0].start == 0


class TestLayout:
    def test_clip_positions_scale_to_the_width(self) -> None:
        seq = sequence(Track("video", 1, [clip("a", 0, 500), clip("b", 500, 1000)]))
        rects, _, _ = layout_rects(snapshot(seq), width=1008)
        clips = [r for r in rects if r.kind == "clip"]
        usable = 1008 - GUTTER - 8
        assert clips[0].x0 == pytest.approx(GUTTER)
        assert clips[0].x1 == pytest.approx(GUTTER + usable / 2)
        assert clips[1].x1 == pytest.approx(GUTTER + usable)

    def test_a_very_short_clip_still_gets_pixels(self) -> None:
        """Otherwise a two-frame clip is invisible and looks like data loss.

        Three pixels, not two: each clip is outlined so butted clips read as
        separate files, and the two edges would otherwise consume the whole box.
        """
        seq = sequence(Track("video", 1, [clip("blink", 0, 2), clip("long", 100, 10000)]))
        rects, _, _ = layout_rects(snapshot(seq), width=960)
        blink = next(r for r in rects if r.clip and r.clip.name == "blink")
        assert blink.x1 - blink.x0 >= 3

    def test_butted_clips_stay_separately_bounded(self) -> None:
        """Eighteen takes laid end to end must not read as one recording."""
        seq = sequence(Track("video", 1, [clip("a", 0, 500), clip("b", 500, 1000)]))
        rects, _, _ = layout_rects(snapshot(seq), width=960)
        first, second = [r for r in rects if r.kind == "clip"]
        # They meet exactly; the 1 px outline on each side draws the boundary.
        assert first.x1 == pytest.approx(second.x0)

    def test_shared_span_makes_two_views_comparable(self) -> None:
        short = sequence(Track("video", 1, [clip("a", 0, 500)]))
        rects, _, _ = layout_rects(snapshot(short), width=1008, span=1000)
        clips = [r for r in rects if r.kind == "clip"]
        usable = 1008 - GUTTER - 8
        # Half the shared span, not the full width of its own timeline.
        assert clips[0].x1 == pytest.approx(GUTTER + usable / 2)

    def test_colours_distinguish_video_audio_and_unmatched(self) -> None:
        seq = sequence(
            Track("video", 1, [clip("v", 0, 100, clip_id="v")]),
            Track("audio", 1, [clip("a", 0, 100, "audio", 1, "a")]),
            Track("video", 2, [clip("o", 200, 300, clip_id="o")]),
        )
        rects, _, _ = layout_rects(snapshot(seq, {"o"}), width=960)
        fills = {r.clip.name: r.fill for r in rects if r.kind == "clip"}
        assert fills == {"v": COLOUR_VIDEO, "a": COLOUR_AUDIO, "o": COLOUR_UNSYNCED}

    def test_rows_are_drawn_for_every_device(self) -> None:
        seq = sequence(
            Track("video", 1, [clip("a", 0, 100, clip_id="a")]),
            Track("audio", 3, [clip("r", 0, 100, "audio", 3, "r")]),
        )
        rects, _, height = layout_rects(snapshot(seq), width=960)
        assert len([r for r in rects if r.kind == "row"]) == 2
        assert height > 0

    def test_ruler_ticks_are_labelled_and_ordered(self) -> None:
        seq = sequence(Track("video", 1, [clip("a", 0, 25 * 60 * 30)]))  # 30 min
        _, ticks, _ = layout_rects(snapshot(seq), width=960)
        assert len(ticks) >= 2
        assert [x for x, _ in ticks] == sorted(x for x, _ in ticks)
        assert all(":" in text for _, text in ticks)

    def test_empty_snapshot_does_not_divide_by_zero(self) -> None:
        seq = sequence(Track("video", 1, []))
        rects, ticks, height = layout_rects(snapshot(seq), width=960)
        assert height > 0
        assert not [r for r in rects if r.kind == "clip"]
