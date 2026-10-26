# SPDX-License-Identifier: GPL-3.0-or-later
"""Turning a solved set of offsets into an actual timeline.

Placement rules, as specified:

* Matched clips are repositioned in place.  The reference track does not move,
  so the edit looks like the one that went in, only in sync.
* Clips that matched nothing are **not** left where they were and **not**
  dropped.  Each source track that has orphans gets a brand-new track appended
  below the existing ones, and its orphans are laid end to end there, starting
  after the last synced clip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..errors import InvalidTrackLabelError
from ..formats import fcp7
from ..model import Clip, MediaType, Sequence, SyncUnit, Track
from .drift import DriftEstimate
from .graph import UnitFeature
from .solve import Solution

_TRACK_LABEL = re.compile(r"^([VvAa])(\d+)$")

#: How far a device's clip-to-timeline offsets may disagree before the
#: device's clock is treated as unusable.  Sync is frame-accurate, so a few
#: seconds of spread already means one of those matches is wrong.
MAX_CLOCK_SPREAD_SECONDS = 5.0


@dataclass(slots=True)
class Placement:
    """What happened to one sync unit."""

    unit_index: int
    label: str
    status: str
    """One of ``synced``, ``island``, ``unsynced``, ``skipped``."""
    start_frames: int
    shift_frames: int = 0
    track_moves: dict[str, str] = field(default_factory=dict)
    clip_ids: list[str] = field(default_factory=list)
    """ids of the clipitems this unit covers, for callers that need to find
    them again after they have been moved to another track."""
    drift_ppm: Optional[float] = None
    speed_percent: Optional[float] = None
    reason: Optional[str] = None


@dataclass(slots=True)
class LayoutResult:
    placements: list[Placement]
    reference_track: Optional[str]
    last_synced_end_frames: int
    new_tracks: dict[str, str]
    sequence_duration_frames: int
    gaps_removed: int = 0
    frames_reclaimed: int = 0
    removed_tracks: list[str] = field(default_factory=list)


def close_gaps(sequence: Sequence, *, minimum_frames: int = 1) -> tuple[int, int]:
    """Remove every stretch of timeline where nothing plays on any track.

    A gap is only a gap if *no* track has a clip there -- a region where the
    picture is black but a recorder is still running is not dead air, and
    removing it would cut audio.

    Sync survives this because gaps never cut through a clip: every clip lies
    wholly before or wholly after each gap, so all clips sharing a moment shift
    left by the same amount and their relative alignment is untouched.

    Returns ``(gaps removed, frames reclaimed)``.
    """
    clips = [c for c in sequence.clips() if not c.has_transition_sentinel]
    if not clips:
        return 0, 0

    spans = sorted((c.start, c.end) for c in clips)
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps: list[tuple[int, int]] = []
    if merged[0][0] > 0:
        # Leading dead air counts too, so the edit starts at zero.
        gaps.append((0, merged[0][0]))
    for current, following in zip(merged, merged[1:], strict=False):
        if following[0] > current[1]:
            gaps.append((current[1], following[0]))
    gaps = [(s, e) for s, e in gaps if e - s >= max(1, minimum_frames)]
    if not gaps:
        return 0, 0

    for clip in clips:
        shift = sum(end - start for start, end in gaps if end <= clip.start)
        if shift:
            fcp7.shift_clip(clip, -shift)

    reclaimed = sum(end - start for start, end in gaps)
    remaining = [c.end for c in sequence.clips() if not c.has_transition_sentinel]
    fcp7.set_sequence_duration(sequence, max(remaining) if remaining else 0)
    return len(gaps), reclaimed


def parse_track_label(label: str) -> tuple[MediaType, int]:
    """Parse ``V1`` / ``A3`` into a (media type, 1-based index) pair."""
    match = _TRACK_LABEL.match(label.strip())
    if not match:
        raise InvalidTrackLabelError(
            f"invalid track label {label!r}; expected e.g. V1 or A2"
        )
    kind, number = match.groups()
    return ("video" if kind.upper() == "V" else "audio"), int(number)


def _track_key(clip: Clip) -> tuple[MediaType, int]:
    return clip.media_type, clip.track_index


def _label(media_type: MediaType, index: int) -> str:
    return f"{'V' if media_type == 'video' else 'A'}{index}"


def choose_reference(
    features: list[UnitFeature],
    component_nodes: list[int],
    requested: Optional[str] = None,
) -> tuple[MediaType, int]:
    """Pick the track that should stay put, and that drift is measured against.

    Cameras are preferred over audio recorders, even a longer-running one:
    picture is the one thing this tool will never retime, so anchoring to a
    camera keeps the reference free of any correction of its own.  Among the
    candidates, the track with the most matched material wins, so the largest
    part of the edit stays where the operator left it.
    """
    if requested:
        return parse_track_label(requested)

    totals: dict[tuple[MediaType, int], float] = {}
    for node in component_nodes:
        unit = features[node].unit
        key = _track_key(unit.primary_clip)
        totals[key] = totals.get(key, 0.0) + (features[node].duration or 0.0)
    if not totals:
        return "video", 1

    video_only = {k: v for k, v in totals.items() if k[0] == "video"}
    candidates = video_only or totals
    return max(candidates.items(), key=lambda kv: (kv[1], -kv[0][1]))[0]


def _apply_drift(
    unit: SyncUnit,
    estimate: DriftEstimate,
    placement: Placement,
) -> None:
    """Write a constant-speed correction onto an audio-recorder unit.

    Premiere refuses a speed effect unless the source range and the timeline
    range agree with the speed value, so the source out-point is re-derived from
    the rounded timeline duration rather than the other way round.
    """
    speed = estimate.speed_percent / 100.0
    if speed <= 0:
        return

    for clip in unit.clips:
        source_span = clip.out_point - clip.in_point
        if source_span <= 0:
            continue
        timeline_duration = max(1, int(round(source_span / speed)))
        # Re-derive the source out-point so the invariant holds exactly.
        corrected_out = clip.in_point + int(round(timeline_duration * speed))

        fcp7.set_clip_timing(
            clip,
            end=clip.start + timeline_duration,
            out_point=corrected_out,
        )
        fcp7.apply_speed_filter(clip, estimate.speed_percent)

    placement.drift_ppm = estimate.drift_ppm
    placement.speed_percent = estimate.speed_percent


def apply_layout(
    sequence: Sequence,
    features: list[UnitFeature],
    solution: Solution,
    *,
    drift_estimates: Optional[dict[int, DriftEstimate]] = None,
    reference_key: Optional[tuple[MediaType, int]] = None,
    unsynced_gap_seconds: float = 1.0,
    islands: str = "append",
    unsynced_placement: str = "inline",
    unsynced_label: Optional[str] = fcp7.DEFAULT_UNSYNCED_LABEL,
    unsynced_audio: str = "keep",
    unsynced_video: str = "keep",
    correct_drift: bool = True,
    drift_threshold_ppm: float = 5.0,
    close_timeline_gaps: bool = False,
    prune_empty_tracks: bool = True,
    stamps: Optional[dict[int, float]] = None,
    stereo_output: bool = True,
) -> LayoutResult:
    """Reposition every clip and build the unsynced tracks."""
    rate = sequence.rate
    drift_estimates = drift_estimates or {}
    placements: list[Placement] = []

    # Where every unit sat before we touched anything.  Captured up front
    # because `place` mutates the clips, and this is the only record of the
    # order the operator laid the footage out in.
    original_start = {
        node: features[node].unit.start
        for component in solution.components
        for node in component.nodes
    }
    original_start.update({n: features[n].unit.start for n in solution.singletons})

    # Components are laid out in shooting order, not in size order.  When a
    # shoot is sequential -- one camera and one recorder, take after take --
    # nothing in the audio connects one take to the next, so the solver returns
    # several disconnected groups whose order it cannot know.  The incoming
    # timeline does know it: the operator dropped the footage onto each track in
    # the order it was shot, so the earliest original position in a group places
    # that group relative to the others.
    ordered = sorted(
        solution.components,
        key=lambda c: (min(original_start[n] for n in c.nodes), min(c.nodes)),
    )
    main = ordered[0] if ordered else None
    extra = ordered[1:]

    absolute: dict[int, float] = {}

    # ---------------------------------------------------------------- main block
    if main is not None:
        if reference_key is None:
            reference_key = choose_reference(features, main.nodes)
        anchors = [n for n in main.nodes if _track_key(features[n].unit.primary_clip) == reference_key]
        if not anchors:
            anchors = main.nodes
        # Hold the earliest clip of the reference track exactly where it was.
        anchor = min(anchors, key=lambda n: (main.positions[n], n))
        shift = rate.frames_to_seconds(features[anchor].unit.start) - main.positions[anchor]
        for node in main.nodes:
            absolute[node] = main.positions[node] + shift

        # Timelines cannot start before zero; slide the whole block if needed.
        lowest = min(absolute.values())
        if lowest < 0:
            for node in absolute:
                absolute[node] -= lowest

    # -------------------------------------------------------------- apply shifts
    def place(node: int, start_seconds: float, status: str) -> Placement:
        unit = features[node].unit
        target = rate.seconds_to_frames(start_seconds)
        delta = target - unit.start
        for clip in unit.clips:
            fcp7.shift_clip(clip, delta)
        placement = Placement(
            unit_index=node,
            label=unit.label,
            status=status,
            start_frames=target,
            shift_frames=delta,
            clip_ids=[c.clip_id for c in unit.clips if c.clip_id],
        )
        placements.append(placement)
        return placement

    for node in sorted(absolute, key=lambda n: absolute[n]):
        placement = place(node, absolute[node], "synced")
        estimate = drift_estimates.get(node)
        unit = features[node].unit
        if (
            correct_drift
            and estimate is not None
            and estimate.measured
            and unit.is_audio_only  # never retime picture
            and abs(estimate.drift_ppm) >= drift_threshold_ppm
        ):
            _apply_drift(unit, estimate, placement)
        elif estimate is not None and estimate.measured:
            placement.drift_ppm = estimate.drift_ppm

    def placed_end() -> int:
        """End of the last clip that has actually been synced.

        Deliberately *not* the end of the sequence: orphans have not moved yet
        and still sit wherever the operator dropped them, which is usually
        after everything else.  Measuring the whole timeline would push the
        unsynced block past the leftovers' own original positions instead of
        placing it after the last synced clip.
        """
        ends = [
            clip.end
            for placement in placements
            if placement.status in ("synced", "island")
            for clip in features[placement.unit_index].unit.clips
            if not clip.has_transition_sentinel
        ]
        return max(ends) if ends else 0

    # -------------------------------------------------- islands and orphans
    gap = max(0.0, unsynced_gap_seconds)
    orphan_nodes: list[int] = list(solution.singletons)
    blocks: list = []
    for component in extra:
        if islands == "unsynced":
            orphan_nodes.extend(component.nodes)
        else:
            blocks.append(component)

    # "merge" collapses an orphan's two exploded-stereo clipitems into one.
    #
    # It is LOSSY and off by default.  Under explodedTracks="true" a
    # <sourcetrack><trackindex> selects a *channel* of the file, not the whole
    # stereo stream, so the surviving clipitem plays only the left channel --
    # confirmed by ear on a real import.  There is no lossless way to put both
    # channels on one track in this format; use "keep" or "split" instead.
    if unsynced_audio == "merge":
        for node in orphan_nodes:
            unit = features[node].unit
            audio = sorted(unit.audio_clips,
                           key=lambda c: (c.source_track_index or 0, c.track_index))
            if len(audio) < 2:
                continue
            keeper, extras = audio[0], audio[1:]
            for extra in extras:
                fcp7.remove_clip(sequence, extra)
                unit.clips.remove(extra)
            # Channel 1 of the file is the whole stereo stream once the clip is
            # no longer one half of an exploded pair.
            fcp7.set_source_channel(keeper, 1)

    new_track_map: dict[tuple[MediaType, int], int] = {}
    new_track_labels: dict[str, str] = {}

    def target_track(key: tuple[MediaType, int], media_type: MediaType) -> Optional[Track]:
        """The new track for a source track, created on first use."""
        if key not in new_track_map:
            # Mirror the source track so channel routing carries over -- except
            # for a merged audio track, where an outputchannelindex would pin
            # the clip back to one output channel and undo the merge.
            model = sequence.track(*key)
            if unsynced_audio == "merge" and media_type == "audio":
                model = None
            track = fcp7.append_track(sequence, media_type, like=model)
            new_track_map[key] = track.index
            new_track_labels[_label(*key)] = _label(media_type, track.index)
        return sequence.track(media_type, new_track_map[key])

    def occupied(track: Track, clip: Clip) -> bool:
        return any(
            other is not clip and clip.start < other.end and other.start < clip.end
            for other in track.clips
        )

    # Islands and orphans are laid out in a single chronological pass.  Doing
    # orphans afterwards packs the islands tight against each other and leaves
    # no room for the material that belongs between them -- on the calibration
    # project two orphans shot between the last two takes had nowhere to go and
    # ended up straddling the take that followed them.
    items: list[tuple[int, int, str, object]] = []
    for component in blocks:
        items.append((min(original_start[n] for n in component.nodes), 0, "block", component))
    for node in orphan_nodes:
        items.append((original_start[node], 1, "orphan", node))
    items.sort(key=lambda item: (item[0], item[1]))

    # Learn each device's clock offset from the clips that *did* sync.
    #
    # Cameras stamp every file with when it started recording.  The clocks are
    # not set to the same time, but each device's own clock is consistent, so
    # one matched clip is enough to pin that device's whole run of footage to
    # the timeline.  Everything else from that device can then be placed from
    # its timestamp -- which is far better than guessing from shooting order,
    # and is what puts an unmatched clip at the moment it was actually shot
    # instead of merely after its predecessor.
    stamps = stamps or {}
    clock_offset: dict[tuple[MediaType, int], float] = {}

    def learn_clocks(from_placements: list[Placement]) -> None:
        """Derive each device's clock-to-timeline offset from trusted positions."""
        samples: dict[tuple[MediaType, int], list[float]] = {}
        for placement in from_placements:
            node = placement.unit_index
            stamp = stamps.get(node)
            if stamp is None:
                continue
            key = _track_key(features[node].unit.primary_clip)
            samples.setdefault(key, []).append(
                rate.frames_to_seconds(placement.start_frames) - stamp
            )
        for key, values in samples.items():
            if key in clock_offset:
                continue
            values.sort()
            if len(values) == 1 or values[-1] - values[0] <= MAX_CLOCK_SPREAD_SECONDS:
                clock_offset[key] = values[len(values) // 2]

    if stamps:
        samples: dict[tuple[MediaType, int], list[float]] = {}
        for placement in placements:
            # Only from positions the audio actually determined.  An island's
            # absolute position is a layout choice, not a measurement, so
            # learning a device's clock from one would anchor that whole
            # device to an arbitrary spot.
            if placement.status != "synced":
                continue
            node = placement.unit_index
            stamp = stamps.get(node)
            if stamp is None:
                continue
            key = _track_key(features[node].unit.primary_clip)
            samples.setdefault(key, []).append(
                rate.frames_to_seconds(placement.start_frames) - stamp
            )
        for key, values in samples.items():
            values.sort()
            spread = values[-1] - values[0]
            # Disagreement across a device's own clips means at least one of
            # those syncs is wrong; trusting the median would then drag every
            # unmatched clip somewhere arbitrary, so leave the device alone.
            if len(values) == 1 or spread <= MAX_CLOCK_SPREAD_SECONDS:
                clock_offset[key] = values[len(values) // 2]

    # Reserve room at the head of the timeline for orphans that were shot
    # *before* their device's first synced clip.
    #
    # A device cannot record two things at once, so its orphans have to sit in
    # the gaps between its own synced clips.  Synced positions are fixed by the
    # audio and cannot move, so when the footage shot earlier does not fit in
    # front of them the only honest remedy is to start the whole timeline
    # later.  Shifting *everything* by the same amount preserves every sync
    # relationship exactly.  Without this the orphans are stacked straight
    # through their own device's synced clip and, finding it occupied, spill
    # onto a second pair of tracks -- which is what makes an export sprout
    # extra audio tracks.
    headroom = 0.0
    for node in orphan_nodes:
        unit = features[node].unit
        tracks_used = unit.source_tracks
        shot_at = original_start[node]

        # A clip whose own clock puts it before zero needs room made for it
        # just as much as a run of earlier takes does. Without this it is
        # clamped to the start of the timeline and lands wherever that is --
        # on the calibration shoot, 32.8 s from where it was shot, which is
        # the one clip of four cameras that stayed wrong.
        stamp = stamps.get(node)
        offset = clock_offset.get(_track_key(unit.primary_clip))
        if stamp is not None and offset is not None:
            headroom = max(headroom, -(stamp + offset))

        # Everything this device recorded before this orphan, that still has to
        # fit in front of the device's earliest synced clip.
        earlier = [
            rate.frames_to_seconds(features[n].unit.duration) + gap
            for n in orphan_nodes
            if original_start[n] <= shot_at
            and (features[n].unit.source_tracks & tracks_used)
        ]
        first_synced = [
            p.start_frames for p in placements
            if p.status in ("synced", "island")
            and (features[p.unit_index].unit.source_tracks & tracks_used)
            and original_start[p.unit_index] > shot_at
        ]
        if not first_synced:
            continue
        needed = sum(earlier)
        available = rate.frames_to_seconds(min(first_synced))
        headroom = max(headroom, needed - available)

    if headroom > 0:
        shift = rate.seconds_to_frames(headroom)
        for placement in placements:
            for clip in features[placement.unit_index].unit.clips:
                fcp7.shift_clip(clip, shift)
            placement.start_frames += shift
            placement.shift_frames += shift
        # The offsets were measured against the timeline that existed a moment
        # ago. Everything on it has just moved, so they are now wrong by
        # exactly that much -- and every clip still to be placed from a clock
        # would inherit the error.
        moved = rate.frames_to_seconds(shift)
        clock_offset = {key: value + moved for key, value in clock_offset.items()}

    cursor = rate.frames_to_seconds(placed_end())
    relocations: list[tuple[int, Placement]] = []
    track_cursors: dict[tuple[MediaType, int], float] = {}

    def device_spans(tracks: set) -> list[tuple[float, float]]:
        """Time already occupied on this device, from everything placed so far."""
        return [
            (rate.frames_to_seconds(features[p.unit_index].unit.start),
             rate.frames_to_seconds(features[p.unit_index].unit.end))
            for p in placements
            if features[p.unit_index].unit.source_tracks & tracks
        ]

    def _first_free_slot(start: float, length: float, tracks: set) -> float:
        """Earliest time at or after ``start`` where ``length`` fits, on this device."""
        spans = sorted(device_spans(tracks))
        moved = True
        while moved:
            moved = False
            for begin, finish in spans:
                if start < finish and begin < start + length:
                    start = finish + gap
                    moved = True
        return start

    def settled_ends(shot_at: int, tracks: set) -> list[float]:
        """Ends of already-placed units from the same device, shot earlier."""
        return [
            rate.frames_to_seconds(features[p.unit_index].unit.end)
            for p in placements
            if p.status in ("synced", "island")
            and original_start[p.unit_index] <= shot_at
            and (features[p.unit_index].unit.source_tracks & tracks)
        ]

    def island_start_from_clock(component) -> Optional[float]:
        """Where an island belongs, when its devices' clocks are known.

        An island is a group that synced to itself but to nothing else, so its
        internal geometry is measured and must be kept; only its position as a
        whole is in question. Appending it after everything else is a last
        resort, and on a real shoot a bad one: a device with even one matched
        clip has a known clock offset, and that fixes where the rest of its
        footage sits to the second.

        Measured on a four-camera shoot, the two clips that formed an island
        were the opening take of each phone. Appended, they landed 2534 s --
        forty-two minutes -- from where they were shot, while every other clip
        from those same phones was correct to a fraction of a second.

        The block moves as one body: each member whose device clock is known
        implies a start for the block, and the median of those is taken so a
        single odd member cannot drag the group. Returns None when no member's
        device has a known offset, and when the implied position would overlap
        something the same device already has on the timeline -- a device
        cannot record two things at once, so such a reading is wrong and
        appending is the honest fallback.
        """
        base = min(component.positions[n] for n in component.nodes)
        candidates: list[float] = []
        for node in component.nodes:
            stamp = stamps.get(node)
            if stamp is None:
                continue
            offset = clock_offset.get(_track_key(features[node].unit.primary_clip))
            if offset is None:
                continue
            candidates.append(stamp + offset - (component.positions[node] - base))
        if not candidates:
            return None

        candidates.sort()
        start = max(0.0, candidates[len(candidates) // 2])

        for node in component.nodes:
            unit = features[node].unit
            begin = start + (component.positions[node] - base)
            finish = begin + rate.frames_to_seconds(unit.duration)
            for other_begin, other_end in device_spans(unit.source_tracks):
                if begin < other_end and other_begin < finish:
                    return None
        return start

    for _key, _rank, kind, payload in items:
        if kind == "block":
            component = payload  # type: ignore[assignment]
            base = min(component.positions[n] for n in component.nodes)
            by_clock = island_start_from_clock(component)
            block_start = cursor + gap if by_clock is None else by_clock
            # Say which it was. A block positioned by a device clock can be
            # out by whatever that clock is out by, and one simply appended
            # is not positioned at all -- in both cases the clips *within* it
            # are still correct relative to each other, and that distinction
            # is the whole of what the operator needs to know.
            why = "appended" if by_clock is None else "clock"
            for node in sorted(component.nodes, key=lambda n: component.positions[n]):
                placement = place(
                    node, block_start + (component.positions[node] - base), "island")
                placement.reason = why
            cursor = max(cursor, rate.frames_to_seconds(placed_end()))
            continue

        node = payload  # type: ignore[assignment]
        unit = features[node].unit
        group_key = _track_key(unit.primary_clip)
        duration_seconds = rate.frames_to_seconds(unit.duration)

        if unsynced_placement == "inline":
            # Anchor to the same *device*, not to the timeline at large: a
            # camera's orphan belongs after the last camera clip that preceded
            # it.  Measuring against everything would let a single long recorder
            # take -- 81 minutes on the calibration project -- drag every orphan
            # to the far end of the timeline.
            offset = clock_offset.get(group_key)
            stamp = stamps.get(node)
            if offset is not None and stamp is not None:
                # The device's own clock says exactly when this was shot.
                start_seconds = max(0.0, stamp + offset)
            else:
                preceding = settled_ends(original_start[node], unit.source_tracks)
                anchor = (max(preceding) + gap) if preceding else 0.0
                start_seconds = max(anchor, track_cursors.get(group_key, 0.0))
            # Take the first free slot on this device.  One camera cannot record
            # two things at once, so an orphan must never be laid through a clip
            # of its own device -- which happens when the footage shot between
            # two synced takes is longer than the room the sync left between
            # them.  Stepping past the blocker keeps the track honest; the
            # report flags the ordering it had to break.
            start_seconds = _first_free_slot(
                start_seconds, duration_seconds, unit.source_tracks,
            )
        else:
            start_seconds = max(cursor + gap, track_cursors.get(group_key, 0.0))

        placement = place(node, start_seconds, "unsynced")
        placement.reason = _orphan_reason(features[node])
        track_cursors[group_key] = start_seconds + duration_seconds
        # Anything placed later must clear this clip, so the following island
        # block does not land on top of it.
        cursor = max(cursor, start_seconds + duration_seconds)

        if unsynced_label:
            for clip in unit.clips:
                fcp7.set_label(clip, unsynced_label)

        relocations.append((node, placement))

    # Track moves happen only once every clip is at its final position.  Doing
    # them inside the loop above compares an orphan against island clips that
    # have not been repositioned yet, which reports collisions that do not exist
    # and spills audio onto new tracks for no reason.
    for node, placement in relocations:
        for clip in list(features[node].unit.clips):
            old_key = _track_key(clip)
            source_track = sequence.track(*old_key)

            # A new track is for a clip that has nowhere else to go, not a
            # badge of shame. An unmatched clip is already marked in the label
            # colour, and once it is placed at the moment it was actually shot
            # -- which it now is, from its device's clock -- moving it to a
            # track of its own only splits one camera's footage across two
            # rows. On a four-camera shoot that turned 4 used video tracks into
            # 7, with three cameras appearing twice.
            #
            # So: stay put when the moment is free, move only when something
            # already occupies it. Exploded stereo in particular cannot be
            # folded onto one track without losing a channel, which is why the
            # audio side of this rule came first.
            mode = unsynced_video if clip.media_type == "video" else unsynced_audio
            if mode == "keep" and source_track is not None and not occupied(source_track, clip):
                continue

            destination = target_track(old_key, clip.media_type)
            if destination is None:  # pragma: no cover - defensive
                continue
            if source_track is not None and clip in source_track.clips:
                source_track.clips.remove(clip)
            fcp7.move_clip_to_track(clip, destination)
            placement.track_moves[_label(*old_key)] = _label(clip.media_type, destination.index)

    last_synced_end = placed_end()

    # Links reference peers by track and clip index; both changed above.
    fcp7.rebuild_link_indices(sequence)

    gaps_removed = frames_reclaimed = 0
    if close_timeline_gaps:
        # Runs last, once every clip is in its final place -- a gap can only be
        # identified against the finished layout.
        origins = {p.unit_index: p.start_frames - p.shift_frames for p in placements}
        gaps_removed, frames_reclaimed = close_gaps(sequence)
        for placement in placements:
            unit = features[placement.unit_index].unit
            placement.start_frames = unit.start
            placement.shift_frames = unit.start - origins[placement.unit_index]
        last_synced_end = placed_end()

    if stereo_output:
        fcp7.set_stereo_output(sequence)

    removed_tracks = fcp7.remove_empty_tracks(sequence) if prune_empty_tracks else []

    # The sequence must span everything, orphan tracks included.
    all_ends = [c.end for c in sequence.clips() if not c.has_transition_sentinel]
    duration = max(all_ends) if all_ends else 0
    fcp7.set_sequence_duration(sequence, duration)

    return LayoutResult(
        placements=placements,
        reference_track=_label(*reference_key) if reference_key else None,
        last_synced_end_frames=last_synced_end,
        new_tracks=new_track_labels,
        sequence_duration_frames=duration,
        gaps_removed=gaps_removed,
        frames_reclaimed=frames_reclaimed,
        removed_tracks=removed_tracks,
    )


def _orphan_reason(feature: UnitFeature) -> str:
    if feature.error:
        return feature.error
    return "no confident audio match against any other clip"
