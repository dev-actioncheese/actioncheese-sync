# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory model of an xmeml timeline.

Every object here keeps a handle on the ``lxml`` element it came from.  That is
deliberate: the writer never rebuilds XML from this model, it patches a copy of
the original tree.  Anything we do not model -- Premiere's ``masterclipid``,
colour metadata, existing effects -- therefore survives a round-trip untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, Optional

from .timebase import Rate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lxml import etree

MediaType = Literal["video", "audio"]

#: xmeml uses -1 in <start>/<end> to mean "abutted to a transition"; the real
#: value has to be derived from the neighbouring transitionitem.
TRANSITION_SENTINEL = -1


@dataclass(slots=True)
class MediaFile:
    """A ``<file>`` reference: the media a clip draws from."""

    file_id: Optional[str]
    name: str
    pathurl: Optional[str]
    path: Optional[Path]
    rate: Optional[Rate] = None
    duration_frames: Optional[int] = None
    element: "etree._Element | None" = field(default=None, repr=False)

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.exists()


@dataclass(slots=True)
class Clip:
    """A ``<clipitem>``: one clip placed on one track."""

    clip_id: Optional[str]
    name: str
    media_type: MediaType
    track_index: int
    """1-based index of the containing track within its media type."""

    start: int
    end: int
    in_point: int
    out_point: int
    rate: Rate
    """Rate the clip's own in/out are expressed in."""

    file: Optional[MediaFile]
    link_refs: list[str] = field(default_factory=list)
    source_track_index: Optional[int] = None
    """``<sourcetrack><trackindex>``: which stream inside the media file."""

    element: "etree._Element | None" = field(default=None, repr=False)

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def has_transition_sentinel(self) -> bool:
        return self.start == TRANSITION_SENTINEL or self.end == TRANSITION_SENTINEL

    def source_span_seconds(self) -> tuple[float, float]:
        """The (offset, duration) of this clip inside its media file, in seconds."""
        offset = self.rate.frames_to_seconds(self.in_point)
        duration = self.rate.frames_to_seconds(self.out_point - self.in_point)
        return offset, duration

    def __str__(self) -> str:
        prefix = "V" if self.media_type == "video" else "A"
        return f"{prefix}{self.track_index}:{self.name}"


@dataclass(slots=True)
class Track:
    """A ``<track>`` inside ``<video>`` or ``<audio>``."""

    media_type: MediaType
    index: int
    clips: list[Clip] = field(default_factory=list)
    enabled: bool = True
    locked: bool = False
    element: "etree._Element | None" = field(default=None, repr=False)

    @property
    def label(self) -> str:
        return f"{'V' if self.media_type == 'video' else 'A'}{self.index}"


@dataclass(slots=True)
class Sequence:
    """A ``<sequence>``: the timeline itself."""

    name: str
    rate: Rate
    duration: int
    video_tracks: list[Track] = field(default_factory=list)
    audio_tracks: list[Track] = field(default_factory=list)
    timecode_start: int = 0
    element: "etree._Element | None" = field(default=None, repr=False)

    @property
    def tracks(self) -> list[Track]:
        return [*self.video_tracks, *self.audio_tracks]

    def clips(self) -> Iterable[Clip]:
        for track in self.tracks:
            yield from track.clips

    def track(self, media_type: MediaType, index: int) -> Optional[Track]:
        pool = self.video_tracks if media_type == "video" else self.audio_tracks
        for track in pool:
            if track.index == index:
                return track
        return None


@dataclass(slots=True)
class Timeline:
    """A parsed xmeml document."""

    sequences: list[Sequence]
    tree: "etree._ElementTree" = field(repr=False)
    version: str = "5"
    source_path: Optional[Path] = None

    @property
    def sequence(self) -> Sequence:
        """The sequence we operate on.

        Multi-sequence exports are rare from a sync-prep workflow; when one
        turns up we take the longest, which is the working timeline in every
        real export we have seen.
        """
        if not self.sequences:
            raise ValueError("timeline contains no sequences")
        return max(self.sequences, key=lambda s: s.duration)


@dataclass(slots=True)
class SyncUnit:
    """A set of clipitems that must move together.

    A camera clip and its linked audio channels form one rigid body: solving
    them separately would let a camera's own audio slide away from its picture.
    Units are the atoms the sync solver actually works on.
    """

    key: str
    clips: list[Clip]

    @property
    def video_clips(self) -> list[Clip]:
        return [c for c in self.clips if c.media_type == "video"]

    @property
    def audio_clips(self) -> list[Clip]:
        return [c for c in self.clips if c.media_type == "audio"]

    @property
    def is_audio_only(self) -> bool:
        """True for standalone recorder clips -- the only ones we may retime.

        A unit with no video clipitem came from an audio recorder rather than a
        camera, so applying a speed change to it cannot affect picture.
        """
        return not self.video_clips

    @property
    def primary_clip(self) -> Clip:
        """The clip whose audio represents this unit."""
        video = self.video_clips
        return video[0] if video else self.clips[0]

    @property
    def start(self) -> int:
        return min(c.start for c in self.clips)

    @property
    def end(self) -> int:
        return max(c.end for c in self.clips)

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def source_tracks(self) -> set[tuple[MediaType, int]]:
        return {(c.media_type, c.track_index) for c in self.clips}

    @property
    def label(self) -> str:
        return str(self.primary_clip)


def build_sync_units(sequence: Sequence) -> list[SyncUnit]:
    """Group a sequence's clips into rigid units via their ``<link>`` elements.

    Links are followed transitively and symmetrically: xmeml only guarantees
    that *some* member of a link group names the others, not that every member
    names every other.
    """
    clips = [c for c in sequence.clips() if c.clip_id or c.file]
    by_id: dict[str, Clip] = {c.clip_id: c for c in clips if c.clip_id}

    parent: dict[int, int] = {id(c): id(c) for c in clips}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for clip in clips:
        for ref in clip.link_refs:
            other = by_id.get(ref)
            if other is not None and id(other) in parent:
                union(id(clip), id(other))

    # Not every NLE links what belongs together.  Premiere exports a stereo
    # recorder file as two *unlinked* mono clipitems, one per channel, so the
    # link graph alone would let the two halves of one pair be solved
    # independently and land at different offsets -- which is audible as
    # phasing.  Clips sharing a file, a source range and a timeline position are
    # channels of a single take by definition, so group them too.
    identical: dict[tuple, list[Clip]] = {}
    for clip in clips:
        if clip.file is None:
            continue
        file_key = clip.file.path or clip.file.file_id or clip.file.name
        key = (file_key, clip.in_point, clip.out_point, clip.start, clip.end)
        identical.setdefault(key, []).append(clip)
    for members in identical.values():
        for other in members[1:]:
            union(id(members[0]), id(other))

    groups: dict[int, list[Clip]] = {}
    for clip in clips:
        groups.setdefault(find(id(clip)), []).append(clip)

    units: list[SyncUnit] = []
    for members in groups.values():
        # Stable ordering so unit keys and reports are reproducible.
        members.sort(key=lambda c: (c.media_type, c.track_index, c.start))
        anchor = next((c for c in members if c.media_type == "video"), members[0])
        key = anchor.clip_id or f"{anchor.media_type}{anchor.track_index}@{anchor.start}"
        units.append(SyncUnit(key=key, clips=members))

    units.sort(key=lambda u: (u.start, u.label))
    return units
