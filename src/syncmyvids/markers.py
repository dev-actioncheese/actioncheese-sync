# SPDX-License-Identifier: GPL-3.0-or-later
"""Sequence markers, and keeping them on the moment they mark.

A marker records a moment in the room -- "this is the bit we want" -- not a
position on a timeline. Syncing moves every clip, so a marker left at the frame
it was written to ends up pointing at whatever happens to be there afterwards,
which is worse than losing it: it is silently wrong.

So each marker is anchored to a clip before anything moves, and rewritten from
that clip's new position afterwards. The clip it belongs to is the one playing
at that moment, preferring a clip from a camera the marker was actually taken
on; a marker with nothing under it -- dropped between takes -- follows the
nearest clip in time, which keeps it the same distance from the material it
sits between.

Only *sequence* markers need this. A marker inside a clipitem is already
measured from that clip's own in-point, so it travels with the clip for free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from lxml import etree

from .model import Clip, Sequence

log = logging.getLogger(__name__)

#: xmeml writes a point marker as one with an out of -1.
POINT_MARKER = -1


@dataclass(slots=True)
class AnchoredMarker:
    """A marker, and the clip whose movement it should follow."""

    element: "etree._Element"
    name: str
    clip_id: Optional[str]
    offset: int
    """Frames from the anchor clip's start. Negative when the marker sits
    before the clip it follows, which is normal for one dropped between
    takes."""
    span: int
    """``out - in`` for a range marker; :data:`POINT_MARKER` for a point."""

    @property
    def anchored(self) -> bool:
        return self.clip_id is not None


def _marker_elements(sequence: Sequence) -> list["etree._Element"]:
    if sequence.element is None:
        return []
    return list(sequence.element.findall("marker"))


def _int(element: "etree._Element", tag: str, default: int = 0) -> int:
    text = element.findtext(tag)
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


#: Sorts after every real preference rank.
_UNRANKED = 1 << 30


def _choose_anchor(clips: list[Clip], frame: int,
                   preferred: Optional[tuple[str, ...]]) -> Optional[Clip]:
    """The clip a marker at ``frame`` belongs to.

    ``preferred`` is the clipitems that share the marker's clock, best first.
    Only those are considered when any of them are present: several cameras
    cover one moment and they are about to be moved into agreement, but the one
    whose clock produced the marker's time is the one whose position means
    something right now.

    A clip covering the moment beats one merely near it.
    """
    if not clips:
        return None
    rank = {clip_id: index for index, clip_id in enumerate(preferred or ())}
    candidates = [c for c in clips if c.clip_id in rank] or clips

    covering = [c for c in candidates if c.start <= frame < c.end]
    if covering:
        return min(covering, key=lambda c: (rank.get(c.clip_id, _UNRANKED),
                                            frame - c.start))

    # Nothing was rolling. Follow whatever is nearest, so the marker keeps its
    # distance from the takes it sits between.
    return min(candidates,
               key=lambda c: (min(abs(frame - c.start), abs(frame - c.end)),
                              rank.get(c.clip_id, _UNRANKED)))


def collect(sequence: Sequence,
            preferred: Optional[dict[str, tuple[str, ...]]] = None) -> list[AnchoredMarker]:
    """Read the sequence's markers and tie each to a clip. Call before syncing."""
    clips = [c for c in sequence.clips() if not c.has_transition_sentinel]
    anchored: list[AnchoredMarker] = []
    for element in _marker_elements(sequence):
        name = (element.findtext("name") or "").strip()
        start = _int(element, "in")
        end = _int(element, "out", POINT_MARKER)
        clip = _choose_anchor(clips, start, (preferred or {}).get(name))
        anchored.append(AnchoredMarker(
            element=element,
            name=name,
            clip_id=clip.clip_id if clip else None,
            offset=start - clip.start if clip else 0,
            span=(end - start) if end != POINT_MARKER else POINT_MARKER,
        ))
    return anchored


def reposition(sequence: Sequence, markers: Iterable[AnchoredMarker]) -> int:
    """Rewrite every anchored marker from its clip's new position.

    Returns how many moved. A marker whose clip has gone -- removed from the
    sequence entirely -- is left where it is rather than deleted: losing a note
    someone made is worse than leaving it slightly adrift, and the report says
    how many were affected.
    """
    by_id = {c.clip_id: c for c in sequence.clips() if c.clip_id}
    moved = 0
    for marker in markers:
        clip = by_id.get(marker.clip_id or "")
        if clip is None:
            continue
        start = max(0, clip.start + marker.offset)
        element = marker.element
        before = _int(element, "in")
        _set(element, "in", start)
        _set(element, "out", POINT_MARKER if marker.span == POINT_MARKER
             else max(start, start + marker.span))
        if start != before:
            moved += 1
    return moved


def orphaned(markers: Iterable[AnchoredMarker], sequence: Sequence) -> list[str]:
    """Names of markers whose anchor clip is no longer in the sequence."""
    present = {c.clip_id for c in sequence.clips() if c.clip_id}
    return [m.name for m in markers if m.clip_id and m.clip_id not in present]


def _set(element: "etree._Element", tag: str, value: int) -> None:
    child = element.find(tag)
    if child is None:
        child = etree.SubElement(element, tag)
    child.text = str(value)


def append(sequence: Sequence, name: str, comment: str, start: int,
           end: int = POINT_MARKER) -> Optional["etree._Element"]:
    """Add a marker to a sequence, in the position the format expects.

    xmeml orders a sequence's children, and a marker belongs after ``<media>``.
    Premiere tolerates worse, but writing what the format says costs nothing.
    """
    if sequence.element is None:
        return None
    element = etree.Element("marker")
    for tag, value in (("name", name), ("comment", comment),
                       ("in", start), ("out", end)):
        child = etree.SubElement(element, tag)
        child.text = str(value)

    media = sequence.element.find("media")
    existing = sequence.element.findall("marker")
    if existing:
        existing[-1].addnext(element)
    elif media is not None:
        media.addnext(element)
    else:
        sequence.element.append(element)
    return element
