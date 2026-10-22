# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader and writer for the FCP7 XML interchange format (``xmeml``).

The writer deliberately *patches* the parsed tree rather than serialising the
model back out.  Real Premiere exports carry a lot of state we neither model
nor understand -- ``masterclipid``, ``uuid``, ``logginginfo``, colour data,
existing effects -- and rebuilding the document would quietly drop all of it.

Two Premiere-specific details matter and are handled here:

* ``<file>`` elements are declared once with full contents and referenced later
  by bare ``<file id="..."/>``.  A registry resolves those back-references.
* Premiere writes ``<pproTicksIn>``/``<pproTicksOut>`` alongside ``<in>``/
  ``<out>`` and *prefers them on import*.  Any change to in/out must update the
  tick values too or the edit silently reverts to the original source range.
"""

from __future__ import annotations

import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

from lxml import etree

from ..errors import XmemlParseError
from ..model import Clip, MediaFile, MediaType, Sequence, Timeline, Track
from ..timebase import Rate

#: Premiere expresses source in/out in ticks as well as frames.
PPRO_TICKS_PER_SECOND = 254016000000

#: Canonical child ordering for a <clipitem>.  Used only when we have to insert
#: an element that was not already present, so it lands somewhere sane.
_CLIPITEM_ORDER = [
    "masterclipid", "name", "enabled", "duration", "rate", "start", "end",
    "in", "out", "pproTicksIn", "pproTicksOut", "alphatype", "pixelaspectratio",
    "anamorphic", "file", "sourcetrack", "filter", "link", "logginginfo",
    "colorinfo", "labels", "comments",
]

_TRACK_ORDER = ["clipitem", "generatoritem", "transitionitem", "enabled", "locked", "outputchannelindex"]


# --------------------------------------------------------------------------- #
# small element helpers
# --------------------------------------------------------------------------- #

def _text(element: Optional[etree._Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _int(element: Optional[etree._Element], tag: str, default: Optional[int] = None) -> Optional[int]:
    raw = _text(element, tag)
    if raw is None:
        return default
    try:
        # Some apps emit float-ish values ("0.0") where the DTD says integer.
        return int(float(raw))
    except ValueError:
        return default


def _bool(element: Optional[etree._Element], tag: str, default: bool) -> bool:
    raw = _text(element, tag)
    if raw is None:
        return default
    return raw.upper() == "TRUE"


def _set_int(element: etree._Element, tag: str, value: int, order: list[str] = _CLIPITEM_ORDER) -> None:
    """Set (or insert) an integer-valued child element."""
    child = element.find(tag)
    if child is None:
        child = etree.Element(tag)
        _insert_ordered(element, child, order)
    child.text = str(int(value))


def _insert_ordered(parent: etree._Element, child: etree._Element, order: list[str]) -> None:
    """Insert ``child`` into ``parent`` respecting a canonical tag order."""
    try:
        rank = order.index(child.tag)
    except ValueError:
        parent.append(child)
        return
    for existing in parent:
        try:
            existing_rank = order.index(existing.tag)
        except ValueError:
            continue
        if existing_rank > rank:
            existing.addprevious(child)
            return
    parent.append(child)


def _parse_rate(element: Optional[etree._Element], fallback: Optional[Rate] = None) -> Optional[Rate]:
    """Read a ``<rate>`` child, falling back to an inherited rate.

    xmeml lets components inherit rate from their parent, so a missing
    ``<rate>`` is normal rather than an error.
    """
    if element is None:
        return fallback
    rate_el = element.find("rate")
    if rate_el is None:
        return fallback
    timebase = _int(rate_el, "timebase")
    if timebase is None:
        return fallback
    return Rate(timebase=timebase, ntsc=_bool(rate_el, "ntsc", False))


def pathurl_to_path(pathurl: Optional[str]) -> Optional[Path]:
    """Convert an xmeml ``<pathurl>`` into a filesystem path."""
    if not pathurl:
        return None
    parsed = urllib.parse.urlparse(pathurl)
    if parsed.scheme and parsed.scheme != "file":
        return None
    # "file://localhost/Volumes/x" -> netloc "localhost", path "/Volumes/x".
    # "file:///Volumes/x"          -> netloc "",          path "/Volumes/x".
    raw = urllib.parse.unquote(parsed.path) if parsed.scheme else urllib.parse.unquote(pathurl)
    return Path(raw) if raw else None


def path_to_pathurl(path: Path) -> str:
    """Render a filesystem path as an xmeml ``<pathurl>``."""
    quoted = urllib.parse.quote(str(path))
    return f"file://localhost{quoted}"


def seconds_to_ppro_ticks(seconds: float) -> int:
    return int(round(seconds * PPRO_TICKS_PER_SECOND))


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

class _FileRegistry:
    """Resolves ``<file id="x"/>`` back-references to their full declaration."""

    def __init__(self) -> None:
        self._by_id: dict[str, MediaFile] = {}

    def resolve(self, element: Optional[etree._Element], fallback_rate: Optional[Rate]) -> Optional[MediaFile]:
        if element is None:
            return None
        file_id = element.get("id")

        # A bare back-reference carries an id and nothing else.
        if file_id and len(element) == 0 and file_id in self._by_id:
            return self._by_id[file_id]

        pathurl = _text(element, "pathurl")
        media = MediaFile(
            file_id=file_id,
            name=_text(element, "name") or (file_id or "<unnamed>"),
            pathurl=pathurl,
            path=pathurl_to_path(pathurl),
            rate=_parse_rate(element, fallback_rate),
            duration_frames=_int(element, "duration"),
            element=element,
        )
        if file_id:
            # Later declarations of the same id are back-references or
            # duplicates; the first full one wins.
            self._by_id.setdefault(file_id, media)
            if media.pathurl is None and file_id in self._by_id:
                return self._by_id[file_id]
        return media


def _parse_clipitem(
    element: etree._Element,
    media_type: MediaType,
    track_index: int,
    registry: _FileRegistry,
    sequence_rate: Rate,
) -> Clip:
    rate = _parse_rate(element, sequence_rate) or sequence_rate
    media = registry.resolve(element.find("file"), rate)

    link_refs = [
        ref.text.strip()
        for ref in element.findall("link/linkclipref")
        if ref.text and ref.text.strip()
    ]

    sourcetrack = element.find("sourcetrack")
    return Clip(
        clip_id=element.get("id"),
        name=_text(element, "name") or (media.name if media else "<unnamed>"),
        media_type=media_type,
        track_index=track_index,
        start=_int(element, "start", 0) or 0,
        end=_int(element, "end", 0) or 0,
        in_point=_int(element, "in", 0) or 0,
        out_point=_int(element, "out", 0) or 0,
        rate=rate,
        file=media,
        link_refs=link_refs,
        source_track_index=_int(sourcetrack, "trackindex"),
        element=element,
    )


def _parse_sequence(element: etree._Element, registry: _FileRegistry) -> Sequence:
    rate = _parse_rate(element)
    if rate is None:
        raise XmemlParseError(
            f"sequence {_text(element, 'name') or '<unnamed>'!r} has no <rate><timebase>"
        )

    sequence = Sequence(
        name=_text(element, "name") or "Sequence",
        rate=rate,
        duration=_int(element, "duration", 0) or 0,
        timecode_start=_int(element.find("timecode"), "frame", 0) or 0,
        element=element,
    )

    media = element.find("media")
    if media is None:
        return sequence

    for media_type in ("video", "audio"):
        container = media.find(media_type)
        if container is None:
            continue
        pool = sequence.video_tracks if media_type == "video" else sequence.audio_tracks
        for index, track_el in enumerate(container.findall("track"), start=1):
            track = Track(
                media_type=media_type,  # type: ignore[arg-type]
                index=index,
                enabled=_bool(track_el, "enabled", True),
                locked=_bool(track_el, "locked", False),
                element=track_el,
            )
            for clip_el in track_el.findall("clipitem"):
                track.clips.append(
                    _parse_clipitem(clip_el, media_type, index, registry, rate)  # type: ignore[arg-type]
                )
            pool.append(track)

    return sequence


def read(path: Path | str) -> Timeline:
    """Parse an xmeml file into a :class:`Timeline`."""
    path = Path(path)
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False, load_dtd=False)
    try:
        tree = etree.parse(str(path), parser)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - depends on input
        raise XmemlParseError(f"{path}: not well-formed XML ({exc})") from exc

    root = tree.getroot()
    if root.tag != "xmeml":
        raise XmemlParseError(
            f"{path}: root element is <{root.tag}>, expected <xmeml>. "
            "Final Cut Pro X .fcpxml files are a different format and are not supported."
        )

    registry = _FileRegistry()
    # Only top-level sequences; a <sequence> nested inside a clipitem is a
    # nested-sequence clip, not a timeline of its own.
    sequences = [_parse_sequence(el, registry) for el in root.findall("sequence")]
    if not sequences:
        raise XmemlParseError(f"{path}: no <sequence> found")

    return Timeline(
        sequences=sequences,
        tree=tree,
        version=root.get("version", "5"),
        source_path=path,
    )


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def set_clip_timing(
    clip: Clip,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
    in_point: Optional[int] = None,
    out_point: Optional[int] = None,
) -> None:
    """Rewrite a clipitem's timing in both the model and the XML tree.

    ``pproTicksIn``/``pproTicksOut`` are kept in step whenever they exist,
    because Premiere trusts them over the frame values on import.
    """
    element = clip.element
    if element is None:
        raise ValueError(f"clip {clip} has no backing element")

    if start is not None:
        clip.start = start
        _set_int(element, "start", start)
    if end is not None:
        clip.end = end
        _set_int(element, "end", end)

    if in_point is not None:
        clip.in_point = in_point
        _set_int(element, "in", in_point)
        if element.find("pproTicksIn") is not None:
            _set_int(element, "pproTicksIn",
                     seconds_to_ppro_ticks(clip.rate.frames_to_seconds(in_point)))
    if out_point is not None:
        clip.out_point = out_point
        _set_int(element, "out", out_point)
        if element.find("pproTicksOut") is not None:
            _set_int(element, "pproTicksOut",
                     seconds_to_ppro_ticks(clip.rate.frames_to_seconds(out_point)))

    if start is not None or end is not None:
        _set_int(element, "duration", max(clip.end - clip.start, 0))


def shift_clip(clip: Clip, delta_frames: int) -> None:
    """Move a clip along the timeline without touching its source range."""
    if delta_frames == 0:
        return
    set_clip_timing(clip, start=clip.start + delta_frames, end=clip.end + delta_frames)


_LINK_ORDER = ["linkclipref", "mediatype", "trackindex", "clipindex", "groupindex"]


def rebuild_link_indices(sequence: Sequence) -> int:
    """Recompute every ``<link>``'s media type, track index and clip index.

    A link identifies its peer both by id (``linkclipref``) and by position
    (``mediatype`` + ``trackindex`` + ``clipindex``).  Moving a clip to another
    track invalidates the positional half, and a stale value silently unlinks
    audio from picture on import -- the failure shows up as clips that refuse to
    move together in the edit, long after the sync itself looked fine.

    Positions are derived from document order within each track, which is the
    same convention the exporting application used, so untouched clips come out
    with the values they went in with.  Returns the number of links updated.
    """
    if sequence.element is None:
        return 0
    media = sequence.element.find("media")
    if media is None:
        return 0

    positions: dict[str, tuple[str, int, int]] = {}
    for media_type in ("video", "audio"):
        container = media.find(media_type)
        if container is None:
            continue
        for track_index, track_el in enumerate(container.findall("track"), start=1):
            for clip_index, clip_el in enumerate(track_el.findall("clipitem"), start=1):
                clip_id = clip_el.get("id")
                if clip_id:
                    positions[clip_id] = (media_type, track_index, clip_index)

    updated = 0
    for clip_el in sequence.element.iter("clipitem"):
        for link in clip_el.findall("link"):
            ref = _text(link, "linkclipref")
            if not ref or ref not in positions:
                continue
            media_type, track_index, clip_index = positions[ref]

            media_el = link.find("mediatype")
            if media_el is None:
                media_el = etree.Element("mediatype")
                _insert_ordered(link, media_el, _LINK_ORDER)
            if media_el.text != media_type:
                media_el.text = media_type
                updated += 1

            if _int(link, "trackindex") != track_index:
                _set_int(link, "trackindex", track_index, order=_LINK_ORDER)
                updated += 1
            if _int(link, "clipindex") != clip_index:
                _set_int(link, "clipindex", clip_index, order=_LINK_ORDER)
                updated += 1

    return updated


def make_speed_filter(speed_percent: float, media_type: MediaType) -> etree._Element:
    """Build the time-remap filter Premiere understands as a constant speed change.

    Premiere validates that ``(out - in) == round((end - start) * speed)``; the
    caller is responsible for that invariant.  Mismatched values make Premiere
    report "parameters inconsistent, speed effect will be ignored".
    """
    filter_el = etree.Element("filter")
    effect = etree.SubElement(filter_el, "effect")
    etree.SubElement(effect, "name").text = "Time Remap"
    etree.SubElement(effect, "effectid").text = "timeremap"
    etree.SubElement(effect, "effectcategory").text = "motion"
    etree.SubElement(effect, "effecttype").text = "motion"
    etree.SubElement(effect, "mediatype").text = media_type

    def parameter(pid: str, name: str, value: str, vmin: Optional[str] = None, vmax: Optional[str] = None) -> None:
        param = etree.SubElement(effect, "parameter", authoringApp="PremierePro")
        etree.SubElement(param, "parameterid").text = pid
        etree.SubElement(param, "name").text = name
        if vmin is not None:
            etree.SubElement(param, "valuemin").text = vmin
        if vmax is not None:
            etree.SubElement(param, "valuemax").text = vmax
        etree.SubElement(param, "value").text = value

    parameter("variablespeed", "variablespeed", "0", "0", "1")
    parameter("speed", "speed", f"{speed_percent:.6f}", "-100000", "100000")
    parameter("reverse", "reverse", "FALSE")
    parameter("frameblending", "frameblending", "FALSE")
    return filter_el


def apply_speed_filter(clip: Clip, speed_percent: float) -> None:
    """Attach (or replace) a constant-speed time remap on a clip."""
    if clip.element is None:
        raise ValueError(f"clip {clip} has no backing element")
    for existing in clip.element.findall("filter"):
        if _text(existing, "effect/effectid") == "timeremap":
            clip.element.remove(existing)
    _insert_ordered(clip.element, make_speed_filter(speed_percent, clip.media_type), _CLIPITEM_ORDER)


#: Premiere's label palette has no entry literally called "Red"; Rose is its
#: red. Valid names: Violet, Iris, Caribbean, Lavender, Cerulean, Forest, Rose,
#: Mango, Purple, Blue, Teal, Magenta, Tan, Green, Brown, Yellow, White.
DEFAULT_UNSYNCED_LABEL = "Rose"


def set_label(clip: Clip, colour: str) -> None:
    """Set a clipitem's label colour, as shown in the NLE's timeline."""
    if clip.element is None:
        raise ValueError(f"clip {clip} has no backing element")
    labels = clip.element.find("labels")
    if labels is None:
        labels = etree.Element("labels")
        _insert_ordered(clip.element, labels, _CLIPITEM_ORDER)
    label2 = labels.find("label2")
    if label2 is None:
        label2 = etree.SubElement(labels, "label2")
    label2.text = colour


def append_track(sequence: Sequence, media_type: MediaType,
                 like: Optional[Track] = None) -> Track:
    """Append a new empty track of the given media type to the sequence.

    ``like`` names the track the new one is standing in for.  Its
    ``<outputchannelindex>`` is copied across, which matters whenever the source
    sequence was exported with ``explodedTracks="true"``: Premiere then writes a
    clip's stereo audio as two *mono* clipitems on separate tracks, and that
    element is what tells it which output channel each track feeds.  A new track
    without it does not inherit the original's routing, so a relocated stereo
    pair can collapse toward one channel.
    """
    if sequence.element is None:
        raise ValueError("sequence has no backing element")
    media = sequence.element.find("media")
    if media is None:
        media = etree.SubElement(sequence.element, "media")
    container = media.find(media_type)
    if container is None:
        container = etree.SubElement(media, media_type)

    track_el = etree.SubElement(container, "track")
    etree.SubElement(track_el, "enabled").text = "TRUE"
    etree.SubElement(track_el, "locked").text = "FALSE"

    if like is not None and like.element is not None:
        channel = like.element.find("outputchannelindex")
        if channel is not None and channel.text:
            etree.SubElement(track_el, "outputchannelindex").text = channel.text

    pool = sequence.video_tracks if media_type == "video" else sequence.audio_tracks
    track = Track(
        media_type=media_type,
        index=len(pool) + 1,
        enabled=True,
        locked=False,
        element=track_el,
    )
    pool.append(track)
    return track


def remove_clip(sequence: Sequence, clip: Clip) -> None:
    """Delete a clipitem and every ``<link>`` that referenced it.

    Leaving a dangling ``linkclipref`` behind points at a clip that no longer
    exists, which is the sort of thing an importer is entitled to choke on.
    """
    if clip.element is None:
        return
    clip_id = clip.clip_id

    parent = clip.element.getparent()
    if parent is not None:
        parent.remove(clip.element)

    if sequence.element is not None and clip_id:
        for other in sequence.element.iter("clipitem"):
            for link in other.findall("link"):
                if _text(link, "linkclipref") == clip_id:
                    other.remove(link)

    for track in sequence.tracks:
        if clip in track.clips:
            track.clips.remove(clip)


def set_source_channel(clip: Clip, channel: int) -> None:
    """Point an audio clipitem at a given channel (``<sourcetrack>``) of its file."""
    if clip.element is None:
        return
    source = clip.element.find("sourcetrack")
    if source is None:
        source = etree.Element("sourcetrack")
        _insert_ordered(clip.element, source, _CLIPITEM_ORDER)
        etree.SubElement(source, "mediatype").text = clip.media_type
    _set_int(source, "trackindex", channel, order=["mediatype", "trackindex"])
    clip.source_track_index = channel


def move_clip_to_track(clip: Clip, track: Track) -> None:
    """Physically relocate a clipitem element into another track element."""
    if clip.element is None or track.element is None:
        raise ValueError("cannot move clips without backing elements")

    source_parent = clip.element.getparent()
    if source_parent is not None:
        source_parent.remove(clip.element)
    _insert_ordered(track.element, clip.element, _TRACK_ORDER)

    clip.track_index = track.index
    track.clips.append(clip)


def set_stereo_output(sequence: Sequence) -> bool:
    """Reconfigure the sequence's master as a single stereo bus.

    Premiere exports these timelines with a *mono* master: ``numOutputChannels``
    of 1 and two separate one-channel output groups, one per channel.  Merging
    them into one two-channel group makes the sequence stereo while leaving each
    track's ``<outputchannelindex>`` untouched, so channel 1 still feeds left and
    channel 2 still feeds right -- the same routing, described as a stereo pair
    rather than two unrelated monos.

    Returns True when anything changed.
    """
    if sequence.element is None:
        return False
    audio = sequence.element.find("media/audio")
    if audio is None:
        return False

    groups = audio.findall("outputs/group")
    channels = sum(int(_text(g, "numchannels") or 1) for g in groups)
    if len(groups) == 1 and channels == 2:
        return False  # already stereo

    outputs = audio.find("outputs")
    if outputs is None:
        outputs = etree.Element("outputs")
        audio.insert(0, outputs)
    for group in list(outputs):
        outputs.remove(group)

    group = etree.SubElement(outputs, "group")
    etree.SubElement(group, "index").text = "1"
    etree.SubElement(group, "numchannels").text = "2"
    etree.SubElement(group, "downmix").text = "0"
    for index in (1, 2):
        channel = etree.SubElement(group, "channel")
        etree.SubElement(channel, "index").text = str(index)

    count = audio.find("numOutputChannels")
    if count is None:
        count = etree.Element("numOutputChannels")
        audio.insert(0, count)
    count.text = "2"
    return True


def remove_empty_tracks(sequence: Sequence) -> list[str]:
    """Drop tracks that ended up with no clips, and renumber what is left.

    Syncing can empty a track completely -- everything on it moved elsewhere --
    and an NLE will happily show a column of empty tracks forever.  Removing
    them changes the 1-based track numbering, so links have to be rebuilt
    afterwards or they point at the wrong rows.

    Returns the labels removed.
    """
    if sequence.element is None:
        return []
    media = sequence.element.find("media")
    if media is None:
        return []

    removed: list[str] = []
    for media_type in ("video", "audio"):
        container = media.find(media_type)
        if container is None:
            continue
        pool = sequence.video_tracks if media_type == "video" else sequence.audio_tracks
        keep: list[Track] = []
        for index, track in enumerate(sorted(pool, key=lambda t: t.index), start=1):
            if track.clips:
                keep.append(track)
                continue
            removed.append(f"{'V' if media_type == 'video' else 'A'}{index}")
            if track.element is not None and track.element.getparent() is not None:
                container.remove(track.element)
        for new_index, track in enumerate(keep, start=1):
            track.index = new_index
            for clip in track.clips:
                clip.track_index = new_index
        pool[:] = keep

    if removed:
        rebuild_link_indices(sequence)
    return removed


def set_sequence_name(sequence: Sequence, name: str, *, new_uuid: bool = True) -> None:
    """Rename the sequence, and give it a fresh identity by default.

    The output is a *different* sequence from the one that went in, so it
    carries a new ``<uuid>``: re-importing a sequence whose uuid already exists
    in the project invites the NLE to treat it as the same one rather than
    adding a second, and the whole point is to compare the two side by side.
    """
    if sequence.element is None:
        return
    sequence.name = name
    element = sequence.element.find("name")
    if element is None:
        element = etree.Element("name")
        _insert_ordered(element, element, ["uuid", "updatebehavior", "name", "duration"])
        sequence.element.insert(0, element)
    element.text = name

    if new_uuid:
        uuid_element = sequence.element.find("uuid")
        if uuid_element is not None:
            uuid_element.text = str(uuid.uuid4())


def set_sequence_duration(sequence: Sequence, duration: int) -> None:
    if sequence.element is None:
        return
    sequence.duration = duration
    _set_int(sequence.element, "duration", duration, order=["uuid", "updatebehavior", "name", "duration", "rate", "in", "out", "timecode", "media"])


def relocate_media(timeline: Timeline, media_root: Path) -> int:
    """Re-point every ``<pathurl>`` at ``media_root``, matching on filename.

    Useful when the XML was authored on another machine or the footage has been
    moved since export.  Returns the number of files relocated.
    """
    index: dict[str, Path] = {}
    for candidate in media_root.rglob("*"):
        if candidate.is_file():
            index.setdefault(candidate.name, candidate)

    relocated = 0
    for file_el in timeline.tree.getroot().iter("file"):
        pathurl_el = file_el.find("pathurl")
        if pathurl_el is None or not pathurl_el.text:
            continue
        current = pathurl_to_path(pathurl_el.text)
        if current is None or current.exists():
            continue
        replacement = index.get(current.name)
        if replacement is not None:
            pathurl_el.text = path_to_pathurl(replacement)
            relocated += 1
    return relocated


def write(timeline: Timeline, path: Path | str) -> None:
    """Serialise the (patched) tree back to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeline.tree.write(
        str(path),
        encoding="UTF-8",
        xml_declaration=True,
        doctype="<!DOCTYPE xmeml>",
    )
