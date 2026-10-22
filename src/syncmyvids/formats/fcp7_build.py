# SPDX-License-Identifier: GPL-3.0-or-later
"""Building an xmeml timeline from folders of media.

The rest of this package *patches* a timeline someone else wrote: it copies the
tree and edits numbers in place, which is the only way to avoid throwing away
the hundred Premiere-specific attributes a real export carries. That works
because there is always an input document.

When the input is a folder of camera cards there is no document, so one has to
be written. The structure here is copied from what Premiere Pro itself exports
-- same element order, same ``pproTicks`` alongside every frame count, same
``<link>`` blocks -- because the surest way to write something Premiere will
import is to write what Premiere writes.

One simplification: each device gets a single audio track and each clip a
single audio clipitem reading source track 1, rather than Premiere's exploded
one-track-per-channel layout. Exploded stereo is the reason unmatched clips
used to land split across two tracks, and nothing here needs it -- we are
laying out whole clips, not re-channelling them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lxml import etree

from ..model import Timeline
from ..sources import Device, ShootMarker
from ..timebase import Rate, format_timecode
from . import fcp7

log = logging.getLogger(__name__)


def _text(parent: etree._Element, tag: str, value) -> etree._Element:
    element = etree.SubElement(parent, tag)
    element.text = str(value)
    return element


def _rate(parent: etree._Element, rate: Rate) -> etree._Element:
    element = etree.SubElement(parent, "rate")
    _text(element, "timebase", rate.timebase)
    _text(element, "ntsc", "TRUE" if rate.ntsc else "FALSE")
    return element


def _timecode(parent: etree._Element, rate: Rate, frame: int = 0) -> None:
    element = etree.SubElement(parent, "timecode")
    _rate(element, rate)
    _text(element, "string", format_timecode(frame, rate, drop_frame=False))
    _text(element, "frame", frame)
    _text(element, "displayformat", "NDF")


def _sample_characteristics(parent: etree._Element, rate: Rate,
                            width: int, height: int) -> None:
    characteristics = etree.SubElement(parent, "samplecharacteristics")
    _rate(characteristics, rate)
    _text(characteristics, "width", width)
    _text(characteristics, "height", height)
    _text(characteristics, "anamorphic", "FALSE")
    _text(characteristics, "pixelaspectratio", "square")
    _text(characteristics, "fielddominance", "none")


class _Builder:
    def __init__(self, rate: Rate, name: str) -> None:
        self.rate = rate
        self.name = name
        self.file_ids: dict[Path, str] = {}
        self.next_file = 1
        self.next_clip = 1
        self.next_master = 1
        self.master_ids: dict[Path, str] = {}

    # -- ids ---------------------------------------------------------------
    def clip_id(self) -> str:
        value = f"clipitem-{self.next_clip}"
        self.next_clip += 1
        return value

    def master_id(self, path: Path) -> str:
        if path not in self.master_ids:
            self.master_ids[path] = f"masterclip-{self.next_master}"
            self.next_master += 1
        return self.master_ids[path]

    # -- file --------------------------------------------------------------
    def file_element(self, parent: etree._Element, clip) -> None:
        """A full ``<file>`` the first time a path is used, a reference after.

        Premiere expects exactly one definition per file id; repeating the
        whole block for every clipitem of a multi-clip file makes an import
        that silently keeps only the last one.
        """
        path = clip.path
        if path in self.file_ids:
            etree.SubElement(parent, "file", id=self.file_ids[path])
            return

        file_id = f"file-{self.next_file}"
        self.next_file += 1
        self.file_ids[path] = file_id

        info = clip.info
        rate = Rate(round(info.frame_rate), False) if info.frame_rate else self.rate
        element = etree.SubElement(parent, "file", id=file_id)
        _text(element, "name", path.name)
        _text(element, "pathurl", fcp7.path_to_pathurl(path))
        _rate(element, rate)
        _text(element, "duration", max(1, rate.seconds_to_frames(info.duration)))
        _timecode(element, rate, 0)
        media = etree.SubElement(element, "media")
        if info.has_video:
            video = etree.SubElement(media, "video")
            _sample_characteristics(video, rate, info.width or 1920, info.height or 1080)
        if info.has_audio:
            audio = etree.SubElement(media, "audio")
            characteristics = etree.SubElement(audio, "samplecharacteristics")
            _text(characteristics, "depth", 16)
            _text(characteristics, "samplerate", 48000)
            _text(characteristics, "channelcount", max(1, info.audio_channels))

    # -- clipitem ----------------------------------------------------------
    def clipitem(self, track: etree._Element, clip, *, clip_id: str,
                 start: int, media_type: str, links: list[tuple[str, str, int, int]]) -> None:
        length = max(1, self.rate.seconds_to_frames(clip.duration))
        in_point = self.rate.seconds_to_frames(clip.in_point)
        item = etree.SubElement(track, "clipitem", id=clip_id)
        _text(item, "masterclipid", self.master_id(clip.path))
        _text(item, "name", clip.name)
        _text(item, "enabled", "TRUE")
        _text(item, "duration", length)
        _rate(item, self.rate)
        _text(item, "start", start)
        _text(item, "end", start + length)
        _text(item, "in", in_point)
        _text(item, "out", in_point + length)
        # Premiere reads the tick values in preference to the frame counts, so
        # they have to agree with them or the clip lands somewhere else.
        _text(item, "pproTicksIn", fcp7.seconds_to_ppro_ticks(
            self.rate.frames_to_seconds(in_point)))
        _text(item, "pproTicksOut", fcp7.seconds_to_ppro_ticks(
            self.rate.frames_to_seconds(in_point + length)))
        if media_type == "video":
            _text(item, "alphatype", "none")
            _text(item, "pixelaspectratio", "square")
            _text(item, "anamorphic", "FALSE")
        self.file_element(item, clip)
        if media_type == "audio":
            source = etree.SubElement(item, "sourcetrack")
            _text(source, "mediatype", "audio")
            _text(source, "trackindex", 1)
        for ref, kind, track_index, clip_index in links:
            link = etree.SubElement(item, "link")
            _text(link, "linkclipref", ref)
            _text(link, "mediatype", kind)
            _text(link, "trackindex", track_index)
            _text(link, "clipindex", clip_index)


def _positions(device: Device, rate: Rate, origin: float) -> list[int]:
    """Where each of a device's clips starts, in frames.

    A clip that knows when it was recorded goes there -- that is a far better
    starting point for the matcher than a butt-join, and for material that
    never matches it is the answer. One that does not follows the previous
    clip, because a device records one thing at a time and the layout stage
    relies on that order holding.
    """
    starts: list[int] = []
    cursor = 0
    for clip in device.clips:
        if clip.timeline_start is not None:
            # Carried over from an input timeline: it already has a position,
            # and second-guessing it would move material the operator placed.
            frame = rate.seconds_to_frames(clip.timeline_start)
        elif clip.started is not None:
            frame = rate.seconds_to_frames(clip.started - origin)
            frame = max(frame, cursor)
        else:
            frame = cursor
        starts.append(frame)
        cursor = frame + max(1, rate.seconds_to_frames(clip.duration))
    return starts


@dataclass(slots=True)
class Built:
    """A timeline assembled here, and what the pipeline needs to know about it."""

    timeline: Timeline
    rigid_clip_ids: dict[str, list[str]] = field(default_factory=dict)
    """Per group, the clipitems whose positions relative to each other were
    stated rather than measured."""
    marker_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Per marker name, the clipitems that share its clock, best first -- so it
    stays with the right camera when several cover the same moment, and never
    follows a card whose clock was wrong."""


def build(devices: list[Device], *, rate: Rate, name: str = "ActionCheese Sync",
          markers: Optional[list[ShootMarker]] = None,
          out_path: Optional[Path] = None) -> Built:
    """Write a timeline holding every device's clips, and read it back.

    Reading our own output back rather than assembling model objects by hand is
    deliberate: it means the pipeline downstream sees a timeline that came out
    of the same reader as every other one, so there is no second code path to
    keep honest.
    """
    devices = [d for d in devices if d.clips]
    if not devices:
        raise ValueError("no clips to build a timeline from")

    dated = [c.started for d in devices for c in d.clips if c.started is not None]
    origin = min(dated) if dated else 0.0

    builder = _Builder(rate, name)
    root = etree.Element("xmeml", version="5")
    sequence = etree.SubElement(root, "sequence", id="sequence-1")
    _text(sequence, "name", name)
    duration_element = _text(sequence, "duration", 0)
    _rate(sequence, rate)
    media = etree.SubElement(sequence, "media")
    video = etree.SubElement(media, "video")
    audio_root = etree.SubElement(media, "audio")

    widest = max((c.info.width or 0 for d in devices for c in d.clips), default=1920) or 1920
    tallest = max((c.info.height or 0 for d in devices for c in d.clips), default=1080) or 1080
    video_format = etree.SubElement(video, "format")
    _sample_characteristics(video_format, rate, widest, tallest)

    # Two output channels: a mono master turns every stereo source into one
    # channel on import, which is how an earlier version quietly lost the right
    # channel of every recorder.
    _text(audio_root, "numOutputChannels", 2)
    audio_format = etree.SubElement(audio_root, "format")
    characteristics = etree.SubElement(audio_format, "samplecharacteristics")
    _text(characteristics, "depth", 16)
    _text(characteristics, "samplerate", 48000)
    outputs = etree.SubElement(audio_root, "outputs")
    for index in (1, 2):
        group = etree.SubElement(outputs, "group")
        _text(group, "index", index)
        _text(group, "numchannels", 1)
        _text(group, "downmix", 0)
        channel = etree.SubElement(group, "channel")
        _text(channel, "index", index)

    # Plan the tracks first: a <link> has to name the track and position of its
    # partner, which means knowing the layout before writing any of it.
    video_devices = [d for d in devices if any(c.has_video for c in d.clips)]
    audio_devices = [d for d in devices if any(c.has_audio for c in d.clips)]
    video_track_of = {id(d): i + 1 for i, d in enumerate(video_devices)}
    audio_track_of = {id(d): i + 1 for i, d in enumerate(audio_devices)}

    group_clip_ids: dict[str, list[str]] = {}
    device_clip_ids: dict[str, set[str]] = {}
    video_items: dict[int, list[tuple]] = {}
    audio_items: dict[int, list[tuple]] = {}

    for device in devices:
        starts = _positions(device, rate, origin)
        video_index = audio_index = 0
        for clip, start in zip(device.clips, starts):
            video_id = builder.clip_id() if clip.has_video else None
            audio_id = builder.clip_id() if clip.has_audio else None
            links: list[tuple[str, str, int, int]] = []
            if video_id:
                video_index += 1
                links.append((video_id, "video", video_track_of[id(device)], video_index))
            if audio_id:
                audio_index += 1
                links.append((audio_id, "audio", audio_track_of[id(device)], audio_index))
            # A single-stream clip needs no link block at all; two of them have
            # to name each other or Premiere treats picture and sound as
            # unrelated and moving one leaves the other behind.
            paired = links if video_id and audio_id else []
            if video_id:
                video_items.setdefault(video_track_of[id(device)], []).append(
                    (clip, video_id, start, paired))
            if audio_id:
                audio_items.setdefault(audio_track_of[id(device)], []).append(
                    (clip, audio_id, start, paired))
            device_clip_ids.setdefault(device.name, set()).update(
                i for i in (video_id, audio_id) if i)
            if device.group:
                group_clip_ids.setdefault(device.group, []).extend(
                    i for i in (video_id, audio_id) if i)

    end_frames = [0]
    for index, device in enumerate(video_devices, start=1):
        track = etree.SubElement(video, "track")
        for clip, clip_id, start, links in video_items.get(index, []):
            builder.clipitem(track, clip, clip_id=clip_id, start=start,
                             media_type="video", links=links)
            end_frames.append(start + max(1, rate.seconds_to_frames(clip.duration)))
        _text(track, "enabled", "TRUE")
        _text(track, "locked", "FALSE")

    for index, device in enumerate(audio_devices, start=1):
        track = etree.SubElement(audio_root, "track")
        for clip, clip_id, start, links in audio_items.get(index, []):
            builder.clipitem(track, clip, clip_id=clip_id, start=start,
                             media_type="audio", links=links)
            end_frames.append(start + max(1, rate.seconds_to_frames(clip.duration)))
        _text(track, "enabled", "TRUE")
        _text(track, "locked", "FALSE")
        # Alternate the channel so two recorders do not stack on one side.
        _text(track, "outputchannelindex", 1 if index % 2 else 2)

    duration_element.text = str(max(end_frames))
    _timecode(sequence, rate, 0)

    # Markers go in before the document is written, so they come back through
    # the reader like everything else.
    marker_hints: dict[str, set[str]] = {}
    for marker in markers or []:
        frame = rate.seconds_to_frames(marker.at - origin)
        element = etree.Element("marker")
        for tag, value in (("name", marker.name), ("comment", marker.comment),
                           ("in", max(0, frame)), ("out", -1)):
            child = etree.SubElement(element, tag)
            child.text = str(value)
        # After <media>, where the format puts it.
        media.addnext(element) if not sequence.findall("marker") else \
            sequence.findall("marker")[-1].addnext(element)
        # Cameras first, in the order the app listed them; then the rest of
        # the shoot, whose clips were timed by the same clock. Without that
        # fallback a marker taken between takes follows whichever clip happens
        # to be nearest -- which on a card with a wrong clock drags it along.
        hint: list[str] = []
        for device_name in marker.devices:
            hint.extend(sorted(device_clip_ids.get(device_name, ())))
        if marker.group:
            for device in devices:
                if device.group == marker.group:
                    hint.extend(c for c in sorted(device_clip_ids.get(device.name, ()))
                                if c not in hint)
        if hint:
            marker_hints[marker.name] = tuple(hint)

    tree = etree.ElementTree(root)
    destination = out_path or _scratch_path(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(destination), encoding="UTF-8", xml_declaration=True, pretty_print=True)
    log.info("built %s from %d device(s), %d marker(s)",
             destination, len(devices), len(markers or []))
    return Built(timeline=fcp7.read(destination), rigid_clip_ids=group_clip_ids,
                 marker_hints=marker_hints)


def _scratch_path(name: str) -> Path:
    import tempfile
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "timeline"
    return Path(tempfile.gettempdir()) / "actioncheese-sync" / f"{safe}.xml"
