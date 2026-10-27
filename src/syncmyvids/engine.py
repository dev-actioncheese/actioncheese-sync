# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level orchestration -- the importable API.

    from syncmyvids import engine
    from syncmyvids.formats import fcp7

    timeline = fcp7.read("timeline.xml")
    report = engine.sync_timeline(timeline)
    fcp7.write(timeline, "timeline-synced.xml")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import sources as sources_module
from .audio.features import FeatureCache
from .audio.ffmpeg import DEFAULT_SAMPLE_RATE
from .errors import SyncMyVidsError
from .formats import fcp7 as fcp7_module
from .formats import fcp7_build
from . import markers as markers_module
from . import metadata as metadata_module
from .model import Timeline, build_sync_units
from .report import SyncReport
from .sync import consistency as consistency_module
from .sync import corroborate as corroborate_module
from .sync import drift as drift_module
from .sync import graph, layout, solve

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncOptions:
    """Everything tunable about a sync run."""

    min_confidence: float = 0.45
    #: Reject a match whose winning lag is no better than the best rival lag.
    #:
    #: At 1.0, the old default, the gate was off: a correlation whose peak only
    #: *tied* the runner-up was accepted, though it says nothing about which of
    #: the two lags is right. Dance-floor music is close to periodic, so on a
    #: wedding party the surface is full of such ties, and three of them got
    #: into the solve -- winning by 0.4%, 0.7% and 0.8% -- one placing a clip
    #: 9313 s out. Clipping those ties took that project's internal error from
    #: 7072 s to 487 s and its frame-exact clips from 18 of 41 to 30.
    #:
    #: 1.05 was the first attempt, chosen from the only two projects we had.
    #: It is now set from measurement. Following Guilley et al.'s evaluation
    #: framework for resynchronisation algorithms, windows of real footage
    #: were shifted by a known amount to make positives, and windows sharing
    #: no moment at all to make negatives -- thousands of labelled trials
    #: needing no hand-synced reference. Against those:
    #:
    #:     gate   keeps good   admits noise      (dance music / other material)
    #:     1.05    96% / 100%     34% / 49%
    #:     1.20    85% / 100%    3.6% / 8.7%
    #:     1.30    74% / 100%    1.3% / 3.0%
    #:
    #: 1.20 cuts the matches accepted between recordings that share nothing by
    #: roughly ten times, at no cost whatever on ordinary material, where a
    #: correct match scores 4 to 20 and nothing is near the threshold. The
    #: cost falls only on music, where even correct matches score 1.0 to 1.8.
    #:
    #: That trade is right because the two errors are not equal: a wrong match
    #: drags a clip minutes out of place, while a rejected one sends it to the
    #: unsynced tracks where it is visible and quick to place by hand. End to
    #: end this loses 3 of 28 usable pairs on the wedding and gains a clip
    #: landing on the exact frame; the four-camera project is unchanged.
    #:
    #: Do not push it much higher. The ratio is a poor requirement on
    #: reverberant material -- 1.4 once rejected 9 of 13 correct matches --
    #: and 1.30 costs four more good pairs for no further gain.
    min_peak_ratio: float = 1.20
    corroborate: bool = True
    corroboration_floor: float = corroborate_module.DEFAULT_FLOOR
    """Weak matches down to here get a second chance if they survive a
    same-device collision check."""
    min_overlap_seconds: float = 20.0
    reference: Optional[str] = None
    unsynced_gap_seconds: float = 1.0
    islands: str = "append"
    unsynced_placement: str = "inline"
    """"inline" drops each orphan in after everything shot before it; "append"
    puts them all after the last synced clip."""
    unsynced_label: Optional[str] = fcp7_module.DEFAULT_UNSYNCED_LABEL
    unsynced_video: str = "keep"
    """Whether an unmatched clip's picture gets a track of its own.

    "keep" leaves it on the track it came from whenever that moment is free,
    and moves it only when something already occupies it. "split" always gives
    it a new track, which is the older behaviour: it makes everything unmatched
    selectable in one go, at the cost of splitting one camera's footage across
    two rows."""
    unsynced_audio: str = "keep"
    """How an unmatched clip's audio is placed.

    "keep" leaves it on the track it came from, so only the picture gets a new
    track; "split" gives it a new track per source channel; "merge" folds the
    two channels onto one track and is LOSSY -- exploded stereo cannot be
    combined without dropping a channel."""
    sequence_name: Optional[str] = None
    """Explicit name for the output sequence; overrides the suffix."""
    sequence_suffix: str = "_synced"
    correct_drift: bool = True
    close_gaps: bool = False
    """Remove every stretch of timeline where nothing plays on any track."""
    prune_empty_tracks: bool = True
    """Drop tracks that end up with no clips at all."""
    trust_synced: bool = False
    """Treat "already in sync" input as authoritative rather than as evidence.

    Off by default. Clips that arrive with stated times -- from an ActionCheese
    shoot index, say -- are placed from those times where nothing else can
    reach them, but where the audio has something to say the audio wins,
    because on real shoots it has proved about ten times the more accurate of
    the two. Turn this on when the stated times are known to be exact, or when
    the material has no usable audio to check them against."""
    use_clock: bool = True
    """Place unmatched clips from their recording timestamp, using the clock
    offset learned from the clips of the same device that did sync."""
    stereo_output: bool = True
    """Write the output sequence with a stereo master bus."""
    drift_threshold_ppm: float = 5.0
    drift_min_duration: float = drift_module.DEFAULT_MIN_DURATION_SECONDS
    analyse_drift: bool = True
    refine: bool = True
    jobs: int = 0
    """0 means "use the CPU count"."""
    cache_dir: Optional[Path] = None
    sample_rate: int = DEFAULT_SAMPLE_RATE

    @property
    def worker_count(self) -> int:
        return self.jobs if self.jobs > 0 else min(8, os.cpu_count() or 4)


#: ``(fraction_done, stage description)``.  Fractions are wall-clock estimates
#: from measured runs, not equal shares: decoding dominates a cold run and
#: matching dominates a warm one, so a naive "one step per phase" bar would sit
#: at 20% for most of the wait.
ProgressCallback = Callable[[float, str], None]


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class LoadedInputs:
    """What the sync runs on, however it was assembled."""

    timeline: Timeline
    rigid_clip_ids: dict[str, list[str]] = field(default_factory=dict)
    """Per group, the clipitems whose positions relative to each other are
    already known and must be preserved."""
    marker_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Per marker name, the clipitems that share its clock, best first."""
    warnings: list[str] = field(default_factory=list)
    built: bool = False
    """True when the timeline was assembled here from folders rather than read
    from a file the operator exported."""
    summary: str = ""


def _is_actioncheese_export(path: Path) -> bool:
    head = path.read_bytes()[:8192].decode("utf-8", "replace")
    return "<name>ActionCheese " in head


def load_inputs(paths, *, sequence_name: Optional[str] = None,
                rate: Optional[object] = None,
                progress: Optional[Callable[[str], None]] = None) -> LoadedInputs:
    """Read a mix of timelines and camera folders into one timeline.

    A single timeline on its own takes the patching path it always has: its
    tree is kept and only numbers in it change, so every Premiere-specific
    detail survives. Anything else has to be assembled, because there is no
    one document to patch -- and assembling means the clips keep their media,
    their in and out points and their positions, but not effects or labels
    that were on them.
    """
    # An entry may be a single path, or a group of folders standing for one
    # camera that filled several cards.
    entries: list = []
    for item in paths:
        if isinstance(item, (list, tuple, set)):
            group = [Path(p).expanduser() for p in item]
            entries.append(group[0] if len(group) == 1 else group)
        else:
            entries.append(Path(item).expanduser())
    if not entries:
        raise SyncMyVidsError("no input given")

    timelines: list[Path] = []
    folders: list = []
    for entry in entries:
        if isinstance(entry, list):
            # Every member has to be a folder; a timeline cannot be half of a
            # camera, and silently ignoring one would lose its clips.
            for member in entry:
                if sources_module.classify(member) == "timeline":
                    raise SyncMyVidsError(
                        f"{member.name}: a timeline cannot be merged into a camera. "
                        "Add it as an input of its own.")
            folders.append(entry)
        elif sources_module.classify(entry) == "timeline":
            timelines.append(entry)
        else:
            folders.append(entry)

    for path in timelines:
        if _is_actioncheese_export(path):
            raise SyncMyVidsError(
                f"{path.name}: this is an ActionCheese export, which holds one sequence "
                f"per take. Pass the shoot folder ({path.parent.name}) instead -- it has "
                "every take on one shared clock.")

    if len(timelines) == 1 and not folders:
        timeline = fcp7_module.read(timelines[0])
        return LoadedInputs(timeline=timeline,
                            summary=f"{timeline.sequence.name}: "
                                    f"{sum(1 for _ in timeline.sequence.clips())} clip(s)")

    devices: list[sources_module.Device] = []
    shoot_markers: list[sources_module.ShootMarker] = []
    warnings: list[str] = []
    base_rate = rate
    base_name = sequence_name

    for path in timelines:
        if progress:
            progress(f"reading {path.name}")
        parsed = fcp7_module.read(path)
        converted, notes = sources_module.from_timeline(parsed, name=path.name)
        devices.extend(converted)
        warnings.extend(notes)
        if base_rate is None:
            # An exported timeline was built at a rate somebody chose; that
            # beats guessing from the footage.
            base_rate = parsed.sequence.rate
        if base_name is None:
            base_name = parsed.sequence.name

    if folders:
        ingested = sources_module.ingest(folders, progress=progress)
        devices.extend(ingested.devices)
        shoot_markers.extend(ingested.markers)
        warnings.extend(ingested.warnings)

    if not devices:
        raise SyncMyVidsError("none of the inputs contained any usable media")

    if base_rate is None:
        base_rate = sources_module.common_rate(devices)
    if base_name is None:
        first = folders[0] if folders else None
        if isinstance(first, list):
            first = first[0]
        base_name = first.name if first is not None else "Timeline"

    built = fcp7_build.build(devices, rate=base_rate, name=base_name,
                             markers=shoot_markers)
    summary = (f"{len(devices)} device(s), {sum(len(d.clips) for d in devices)} clip(s) "
               f"at {base_rate}" + (f", {len(shoot_markers)} marker(s)" if shoot_markers else "")
               + "\n" + sources_module.describe(
                   sources_module.Ingest(devices=devices)))
    return LoadedInputs(timeline=built.timeline, rigid_clip_ids=built.rigid_clip_ids,
                        marker_hints=built.marker_hints, warnings=warnings,
                        built=True, summary=summary)


_STAGES = [
    (0.00, "reading timeline"),
    (0.05, "decoding audio"),
    (0.55, "matching clips"),
    (0.80, "solving offsets"),
    (0.88, "measuring drift"),
    (0.95, "laying out timeline"),
    (1.00, "done"),
]


def _rigid_unit_groups(units, rigid_clip_ids: dict[str, list[str]]) -> list[dict[int, float]]:
    """Turn "these clipitems are already in sync" into unit-level geometry.

    The caller knows about clipitems; the solver knows about units. A unit
    belongs to a group if any of its clipitems does, and the geometry we keep
    is simply where the units already sit relative to each other -- the
    timeline was built from the stated times, so its layout *is* the statement.
    """
    if not rigid_clip_ids:
        return []
    owner: dict[str, str] = {}
    for group, ids in rigid_clip_ids.items():
        for clip_id in ids:
            owner[clip_id] = group

    grouped: dict[str, dict[int, float]] = {}
    for index, unit in enumerate(units):
        names = {c.clip_id for c in unit.clips if c.clip_id}
        found = {owner[name] for name in names if name in owner}
        if len(found) != 1:
            # A unit spanning two groups would mean two shoots share a
            # clipitem, which cannot happen; a unit in none is ordinary
            # material to be matched.
            continue
        group = found.pop()
        rate = unit.primary_clip.rate
        grouped.setdefault(group, {})[index] = rate.frames_to_seconds(unit.start)

    return [members for members in grouped.values() if len(members) > 1]


def _stated_edges(groups: list[dict[int, float]], *, trust: bool) -> list[graph.Edge]:
    """Chain each group's members together from the times we were given.

    A chain rather than every pair: n-1 edges relate n units just as completely
    as n(n-1)/2 do, and a hundred-clip shoot would otherwise add five thousand
    rows to the least-squares system for nothing.

    ``trust`` decides whether these are constraints or merely evidence. They
    are evidence by default, because measurement says they should be: see
    :data:`syncmyvids.sync.graph.PRIOR_WEIGHT`.
    """
    kind = "rigid" if trust else "prior"
    edges: list[graph.Edge] = []
    for members in groups:
        ordered = sorted(members, key=lambda node: members[node])
        for first, second in zip(ordered, ordered[1:]):
            edges.append(graph.Edge(
                a=first, b=second,
                delta_seconds=members[second] - members[first],
                confidence=1.0, peak_ratio=0.0, overlap_seconds=0.0, kind=kind,
            ))
    return edges


def _stated_disagreements(groups: list[dict[int, float]], solution,
                          units) -> list[dict]:
    """How far the audio moved each pair of clips that arrived already synced.

    Worth reporting rather than hiding: it is a direct measurement of how well
    whatever produced those timings is doing, and a camera whose stated times
    are consistently out by half a second is a fault worth knowing about.
    """
    placed: dict[int, float] = {}
    for component in solution.components:
        placed.update(component.positions)

    out: list[dict] = []
    for members in groups:
        ordered = sorted(members, key=lambda node: members[node])
        for first, second in zip(ordered, ordered[1:]):
            if first not in placed or second not in placed:
                continue
            stated = members[second] - members[first]
            solved = placed[second] - placed[first]
            if abs(solved - stated) < 0.02:
                continue
            out.append({
                "a": units[first].label,
                "b": units[second].label,
                "stated_delta": round(stated, 4),
                "solved_delta": round(solved, 4),
                "moved_by": round(solved - stated, 4),
            })
    return out


def _label_attempts(attempts: list[dict], strong, corroborated) -> None:
    """Stamp each scored pair with the decision that reached the timeline.

    ``stage`` is the detail: "solved" for a match confident enough on its own,
    "corroborated" for one admitted because it agreed with the solution,
    "weak" for one that passed the lowered matching floor and got no further,
    and "below-floor" for one never in contention. ``accepted`` becomes the
    plain answer to "did this pair move the timeline", which is the question
    a comparison against a known-good timeline can actually grade.
    """
    def key(edge) -> tuple[int, int]:
        return (edge.a, edge.b)

    solved = {key(e) for e in strong if not e.stated}
    extra = {key(e) for e in corroborated or () if not e.stated}
    for attempt in attempts:
        pair = (attempt.get("a_index"), attempt.get("b_index"))
        if pair in solved:
            stage = "solved"
        elif pair in extra:
            stage = "corroborated"
        elif attempt.get("passed_floor"):
            stage = "weak"
        else:
            stage = "below-floor"
        attempt["stage"] = stage
        attempt["accepted"] = stage in ("solved", "corroborated")


def sync_timeline(
    timeline: Timeline,
    options: Optional[SyncOptions] = None,
    progress: Optional[ProgressCallback] = None,
    rigid_clip_ids: Optional[dict[str, list[str]]] = None,
    marker_hints: Optional[dict[str, tuple[str, ...]]] = None,
) -> SyncReport:
    """Analyse and reposition a timeline in place.

    Mutates ``timeline``'s underlying XML tree; write it out with
    :func:`syncmyvids.formats.fcp7.write`.

    ``progress`` is called with a fraction in [0, 1] and a short description of
    the current stage, so a caller can show a bar without knowing anything about
    the pipeline.
    """
    options = options or SyncOptions()

    def report_progress(fraction: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), message)
    # Validate up front rather than when the reference is first needed: a typo
    # should fail immediately, not be silently ignored on a timeline where
    # nothing happened to sync.
    if options.reference:
        layout.parse_track_label(options.reference)

    sequence = timeline.sequence
    warnings: list[str] = []

    units = build_sync_units(sequence)
    if not units:
        warnings.append("sequence contains no clips")

    stranded = [u for u in units if u.primary_clip.has_transition_sentinel]
    if stranded:
        warnings.append(
            f"{len(stranded)} clip(s) abut a transition (start/end of -1) and were left in place; "
            "their true position depends on the neighbouring transition"
        )

    # Anchor markers before a single clip moves: a marker records a moment,
    # and after the sync the only way back to that moment is the clip that was
    # playing at it.
    sequence_markers = markers_module.collect(sequence, marker_hints)
    if sequence_markers:
        log.info("following %d sequence marker(s) through the sync", len(sequence_markers))

    report_progress(0.02, "reading timeline")
    cache = FeatureCache(options.cache_dir, sample_rate=options.sample_rate)

    report_progress(0.05, f"decoding audio for {len(units)} clips")
    log.info("extracting audio features for %d clips", len(units))
    features = graph.extract_features(
        units, cache, jobs=options.worker_count,
        progress=lambda f: report_progress(0.05 + 0.50 * f,
                                           f"decoding audio ({int(f * 100)}%)"),
    )
    for feature in features:
        if feature.error:
            warnings.append(f"{feature.unit.label}: {feature.error}")

    usable = sum(1 for f in features if f.usable)
    log.info("matching %d usable clips (%d pairs)", usable, usable * (usable - 1) // 2)
    # Match down to the corroboration floor so the second pass has candidates to
    # work with, then solve on the confident subset only.
    floor = min(options.corroboration_floor, options.min_confidence)
    edges, attempts = graph.match_pairs(
        features, cache,
        min_confidence=floor,
        min_peak_ratio=options.min_peak_ratio,
        min_overlap_seconds=options.min_overlap_seconds,
        refine=options.refine,
        progress=lambda f: report_progress(0.55 + 0.25 * f,
                                           f"matching clips ({int(f * 100)}%)"),
    )
    report_progress(0.80, "solving offsets")
    # A rescued match is as good as a strong one -- better, on the evidence:
    # it was kept because no other alignment of those two recordings comes
    # close, which is the question the solver actually needs answered.
    strong = [e for e in edges
              if e.confidence >= options.min_confidence or e.distinctive]
    rescued = [e for e in strong if e.distinctive and e.confidence < options.min_confidence]
    if rescued:
        log.info("%d match(es) admitted on an isolated peak despite low confidence",
                 len(rescued))

    stated_groups = _rigid_unit_groups(units, rigid_clip_ids or {})
    stated = _stated_edges(stated_groups, trust=options.trust_synced)
    if stated:
        log.info("%d group(s) of already-synced clips contribute %d stated relationship(s)",
                 len(stated_groups), len(stated))

    solution = solve.solve(len(units), strong + stated)
    if options.trust_synced:
        solve.snap_rigid(solution, stated_groups)

    corroborated: list[dict] = []
    extra: list = []
    if options.corroborate and len(strong) < len(edges):
        extra, corroborated = corroborate_module.corroborate(
            features, solution, edges,
            min_confidence=options.min_confidence,
            floor=floor,
        )
        if extra:
            log.info("corroboration admitted %d further match(es)", len(extra))
            solution = solve.solve(len(units), strong + extra + stated)
            if options.trust_synced:
                solve.snap_rigid(solution, stated_groups)

    # Say what actually became of every pair we scored.
    #
    # The matching pass runs at a lowered floor so corroboration has something
    # to work with, so its own "accepted" means only "passed that floor" --
    # a third of those pairs never reach the solver. A report that calls them
    # accepted invites exactly the wrong conclusion: that the matcher took a
    # match it in fact threw away. The only decision worth learning from is
    # the one that shaped the timeline.
    _label_attempts(attempts, strong, extra)

    stamps: dict[int, float] = {}
    if options.use_clock:
        for index, unit in enumerate(units):
            media = unit.primary_clip.file
            if media is None or media.path is None:
                continue
            stamp = metadata_module.creation_time(media.path)
            if stamp is not None:
                # Offset by the clip's in-point: a subclip starts later than the
                # file's own recording start.
                stamps[index] = stamp + unit.primary_clip.rate.frames_to_seconds(
                    unit.primary_clip.in_point)

    # A device cannot record two things at once, so drop any solved position
    # that contradicts its own device's shooting order.  Runs after
    # corroboration so it judges the final set of matches.
    shot_order = {index: unit.start for index, unit in enumerate(units)}
    solution, order_conflicts = consistency_module.enforce_device_order(
        features, solution, edges, shot_order, stamps=stamps,
    )
    for entry in order_conflicts:
        warnings.append(
            f"{entry['clip']}: {entry['reason']} (conflicts with {entry['conflicts_with']})"
        )

    # Positions across all components; drift only ever compares peers that share
    # a component, so merging the (component-relative) frames is safe here.
    positions: dict[int, float] = {}
    for component in solution.components:
        positions.update(component.positions)

    # Resolve the reference once, up front: it anchors the layout *and* defines
    # the baseline every drift figure is measured against.
    reference_key = None
    if solution.components:
        reference_key = layout.choose_reference(
            features, solution.components[0].nodes, options.reference
        )

    drift_estimates: dict[int, drift_module.DriftEstimate] = {}
    if options.analyse_drift and positions:
        report_progress(0.88, "measuring clock drift")
        log.info("estimating clock drift")
        drift_estimates = drift_module.estimate_drift(
            features, positions, edges, cache,
            min_duration_seconds=options.drift_min_duration,
            reference_key=reference_key,
            jobs=options.worker_count,
        )

    report_progress(0.95, "laying out timeline")
    result = layout.apply_layout(
        sequence, features, solution,
        drift_estimates=drift_estimates,
        reference_key=reference_key,
        unsynced_gap_seconds=options.unsynced_gap_seconds,
        islands=options.islands,
        unsynced_placement=options.unsynced_placement,
        unsynced_label=options.unsynced_label,
        unsynced_audio=options.unsynced_audio,
        unsynced_video=options.unsynced_video,
        correct_drift=options.correct_drift,
        drift_threshold_ppm=options.drift_threshold_ppm,
        close_timeline_gaps=options.close_gaps,
        prune_empty_tracks=options.prune_empty_tracks,
        stamps=stamps,
        stereo_output=options.stereo_output,
    )

    markers_moved = markers_module.reposition(sequence, sequence_markers)
    lost_anchors = markers_module.orphaned(sequence_markers, sequence)
    if lost_anchors:
        warnings.append(
            f"{len(lost_anchors)} marker(s) were on clips that are no longer in the "
            f"sequence and stayed where they were: {', '.join(lost_anchors[:5])}")

    # Rename last: the output is a new sequence and should not collide with the
    # one already in the project bin.
    if options.sequence_name:
        fcp7_module.set_sequence_name(sequence, options.sequence_name)
    elif options.sequence_suffix and not sequence.name.endswith(options.sequence_suffix):
        fcp7_module.set_sequence_name(sequence, sequence.name + options.sequence_suffix)

    placements = [
        {
            "label": p.label,
            "status": p.status,
            "start_frames": p.start_frames,
            "shift_frames": p.shift_frames,
            "shift_seconds": round(sequence.rate.frames_to_seconds(p.shift_frames), 4),
            "track_moves": p.track_moves,
            "clip_ids": p.clip_ids,
            "drift_ppm": None if p.drift_ppm is None else round(p.drift_ppm, 3),
            "speed_percent": None if p.speed_percent is None else round(p.speed_percent, 6),
            "reason": p.reason,
        }
        for p in result.placements
    ]
    corrected = {p.unit_index for p in result.placements if p.speed_percent is not None}

    drift_rows = [
        {
            "label": features[index].unit.label,
            "measured": estimate.measured,
            "drift_ppm": round(estimate.drift_ppm, 3),
            "speed_percent": round(estimate.speed_percent, 6),
            "span_seconds": round(estimate.span_seconds, 2),
            "total_error_seconds": round(estimate.total_error_seconds, 4),
            "probe_count": estimate.probe_count,
            "fit_rms_ms": round(estimate.fit_rms_seconds * 1000, 3),
            "peer": None if estimate.peer_index is None else features[estimate.peer_index].unit.label,
            "corrected": index in corrected,
            "audio_only": features[index].unit.is_audio_only,
            "reason": estimate.reason,
        }
        for index, estimate in sorted(drift_estimates.items())
    ]

    report_progress(1.0, "done")
    return SyncReport(
        source=str(timeline.source_path) if timeline.source_path else None,
        sequence_name=sequence.name,
        rate=sequence.rate,
        unit_count=len(units),
        reference_track=result.reference_track,
        last_synced_end_frames=result.last_synced_end_frames,
        sequence_duration_frames=result.sequence_duration_frames,
        placements=placements,
        pairs=attempts,
        conflicts=solution.conflicts,
        corroborated=corroborated,
        markers_total=len(sequence_markers),
        markers_moved=markers_moved,
        stated_disagreements=_stated_disagreements(stated_groups, solution, units),
        removed_tracks=result.removed_tracks,
        gaps_removed=result.gaps_removed,
        frames_reclaimed=result.frames_reclaimed,
        drift=drift_rows,
        new_tracks=result.new_tracks,
        warnings=warnings,
    )
