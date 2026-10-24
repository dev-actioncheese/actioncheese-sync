# SPDX-License-Identifier: GPL-3.0-or-later
"""A read-only timeline strip for the desktop app.

The log tells you *what* happened; this shows you the shape of it. Two of these
side by side -- the timeline as imported and as written -- is the fastest way to
see that clips moved where you expected, that the unmatched ones landed on their
own track, and that gaps closed.

Both views are drawn to a shared time scale so the two are directly comparable.
"""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Iterable, Optional

from .model import Sequence, build_sync_units
from .timebase import Rate, format_timecode

# Track fills, taken from the icon: cheese for picture, a deeper rind for
# sound, rose for anything the sync could not place. Sampled from the same
# gradient the logo uses, so the app and its icon are recognisably one thing.
COLOUR_VIDEO = "#FFC933"
COLOUR_AUDIO = "#E99A0A"
COLOUR_UNSYNCED = "#F43F5E"
#: Markers. The one cool colour on the strip, because it has to be seen
#: against cheese yellow, rind amber and rose alike.
COLOUR_MARKER = "#38BDF8"
COLOUR_GRID = "#3A3F4C"
COLOUR_ROW = "#1E222C"
COLOUR_ROW_ALT = "#242833"
COLOUR_BG = "#14161C"
COLOUR_TEXT = "#A9A294"
#: The waveform, and the holes. Dark enough to read on both yellows.
COLOUR_WAVE = "#14161C"
#: Pits, one shade down from each fill. The icon's cheese is pitted, so the
#: clips are too -- but a pit is texture, not information, so it stays close
#: enough to its fill that nobody mistakes it for a mark that means something.
COLOUR_PIT = {
    COLOUR_VIDEO: "#EFB525",
    COLOUR_AUDIO: "#CE8708",
    COLOUR_UNSYNCED: "#DC2E4C",
}
#: Below this a clip is a sliver, and a hole in it would just be dirt.
PIT_MIN_WIDTH = 26
#: Drawn around every clip so butted clips read as separate files.  Without it
#: eighteen takes laid end to end look like one continuous recording.
COLOUR_EDGE = "#14161C"

def _font_families() -> tuple[str, str]:
    """The UI and monospace faces this platform actually has.

    Menlo and Helvetica are macOS fonts. Asking for them elsewhere does not
    fail -- Tk quietly substitutes something -- but what it substitutes is
    rarely what the layout was measured against, so the window comes out
    looking subtly wrong rather than broken, which is harder to notice.
    """
    if sys.platform == "darwin":
        return "Helvetica", "Menlo"
    if sys.platform == "win32":
        return "Segoe UI", "Consolas"
    return "DejaVu Sans", "DejaVu Sans Mono"


UI_FONT, MONO_FONT = _font_families()

ROW_HEIGHT = 30
ROW_GAP = 2
GUTTER = 46
RULER_HEIGHT = 18


@dataclass(slots=True)
class ClipBox:
    clip_id: Optional[str]
    name: str
    start: int
    end: int
    unsynced: bool = False
    media: Optional[Path] = None
    source_offset: float = 0.0
    source_duration: Optional[float] = None
    peaks: tuple[float, ...] = ()
    """Filled in later, off the UI thread; empty means "not loaded yet"."""

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)


@dataclass(slots=True)
class MarkerPin:
    """A sequence marker, for drawing over the strip."""

    frame: int
    name: str


@dataclass(slots=True)
class TrackRow:
    label: str
    media_type: str
    clips: list[ClipBox] = field(default_factory=list)


@dataclass(slots=True)
class TimelineSnapshot:
    """A frozen picture of a sequence, safe to hand between threads."""

    name: str
    rate: Rate
    duration: int
    tracks: list[TrackRow] = field(default_factory=list)
    markers: list[MarkerPin] = field(default_factory=list)

    @property
    def clip_count(self) -> int:
        return sum(len(t.clips) for t in self.tracks)


def snapshot(sequence: Sequence, unsynced_ids: Optional[Iterable[str]] = None) -> TimelineSnapshot:
    """Capture a sequence's layout.

    Taken before and after the run, so it must copy the values rather than hold
    on to clips the engine is about to move.

    Unmatched clips are identified by ``clip_id``, not by a track-qualified
    name: an unmatched clip has usually been moved to a different track by the
    time this runs, so "V1:whatever.MP4" would no longer match anything.
    """
    flagged = set(unsynced_ids or ())

    # One row per *device*, not per track.  A camera contributes a picture row
    # and nothing else -- its own audio is part of the same clip and showing it
    # separately just repeats the row above.  A stereo recorder contributes one
    # row, not one per channel: the two are the same recording.
    rows: dict[tuple[str, int], TrackRow] = {}
    for unit in build_sync_units(sequence):
        if unit.video_clips:
            anchor = min(unit.video_clips, key=lambda c: c.track_index)
        else:
            anchor = min(unit.audio_clips, key=lambda c: c.track_index)
        key = (anchor.media_type, anchor.track_index)
        row = rows.get(key)
        if row is None:
            label = f"{'V' if anchor.media_type == 'video' else 'A'}{anchor.track_index}"
            row = TrackRow(label=label, media_type=anchor.media_type)
            rows[key] = row

        # Waveforms come from the unit's audio wherever it lives -- a camera row
        # draws the camera's own sound even though that clip is not shown.
        audio = unit.audio_clips[0] if unit.audio_clips else anchor
        media = audio.file.path if audio.file else None
        offset, span = audio.source_span_seconds()
        row.clips.append(ClipBox(
            clip_id=anchor.clip_id,
            name=anchor.name,
            start=unit.start,
            end=unit.end,
            unsynced=any(c.clip_id in flagged for c in unit.clips),
            media=media,
            source_offset=offset,
            source_duration=span if span > 0 else None,
        ))

    tracks = [rows[k] for k in sorted(rows, key=lambda k: (k[0] != "video", k[1]))]
    for row in tracks:
        row.clips.sort(key=lambda c: c.start)

    pins: list[MarkerPin] = []
    if sequence.element is not None:
        for element in sequence.element.findall("marker"):
            try:
                frame = int((element.findtext("in") or "0").strip())
            except ValueError:
                continue
            pins.append(MarkerPin(frame=frame,
                                  name=(element.findtext("name") or "").strip()))
    pins.sort(key=lambda m: m.frame)

    # Deliberately *not* stretched to reach a distant marker. One dropped an
    # hour after the last take would squeeze two minutes of footage into a few
    # pixels; it is skipped at the right-hand edge instead, and the exported
    # XML still carries it.
    ends = [c.end for t in tracks for c in t.clips]
    return TimelineSnapshot(
        name=sequence.name,
        rate=sequence.rate,
        duration=max(ends) if ends else sequence.duration,
        tracks=tracks,
        markers=pins,
    )


def pit_positions(clip: ClipBox, width: float, height: float) -> list[tuple[float, float, float]]:
    """Cheese holes for one clip, as ``(cx, cy, radius)`` offsets from its corner.

    Derived from the clip's own name so they hold still across redraws: holes
    that danced every time the window resized would read as something changing
    rather than as the surface of the thing.
    """
    if width < PIT_MIN_WIDTH or height < 10:
        return []
    seed = 0
    for character in clip.name:
        seed = (seed * 131 + ord(character)) & 0xFFFFFFFF

    count = 1 + int(min(2, width // 110))
    pits: list[tuple[float, float, float]] = []
    for index in range(count):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        radius = height * (0.10 + 0.07 * ((seed >> 7) % 100) / 100)
        # Spread across the clip, kept a radius clear of every edge.
        slot = (index + 0.5) / count
        jitter = ((seed >> 15) % 100) / 100 - 0.5
        cx = width * min(max(slot + jitter * 0.28 / count, 0.0), 1.0)
        cy = height * (0.22 + 0.56 * (((seed >> 21) % 100) / 100))
        cx = min(max(cx, radius + 1), width - radius - 1)
        cy = min(max(cy, radius + 1), height - radius - 1)
        if cx - radius > 1 and cx + radius < width - 1:
            pits.append((cx, cy, radius))
    return pits


def load_peaks(snap: TimelineSnapshot, cache, buckets: int = 600) -> None:
    """Fill in each clip's waveform, in place.

    Slow the first time (it decodes), instant afterwards (the cache keeps the
    envelopes), so callers should run it off the UI thread.
    """
    for track in snap.tracks:
        for clip in track.clips:
            if clip.peaks or clip.media is None or not clip.media.exists():
                continue
            try:
                values = cache.peaks(clip.media, start=clip.source_offset,
                                     duration=clip.source_duration, buckets=buckets)
            except Exception:  # noqa: BLE001 - a missing waveform is cosmetic
                continue
            clip.peaks = tuple(float(v) for v in values)


@dataclass(frozen=True, slots=True)
class Rect:
    """One drawable box, in pixels."""

    x0: float
    y0: float
    x1: float
    y1: float
    fill: str
    kind: str          # "row" | "clip"
    clip: Optional[ClipBox] = None


def timeline_scale(snap: TimelineSnapshot, width: int,
                   span: Optional[int] = None) -> tuple[int, int, float]:
    """``(total frames, usable pixels, pixels per frame)``.

    Kept in one place because markers are drawn over the same strip the boxes
    are laid out on, and a second copy of this arithmetic would be a second
    chance for the two to disagree.
    """
    total = max(1, span or snap.duration)
    usable = max(10, width - GUTTER - 8)
    return total, usable, usable / total


def layout_rects(snap: TimelineSnapshot, width: int,
                 span: Optional[int] = None) -> tuple[list[Rect], list[tuple[float, str]], int]:
    """Geometry for one snapshot: boxes, ruler ticks, and total height.

    Shared by the on-screen canvas and the image renderer so the two can never
    drift apart -- a picture in the README that does not match what the app
    draws would be worse than no picture.
    """
    total, usable, scale = timeline_scale(snap, width, span)

    rects: list[Rect] = []
    y = RULER_HEIGHT + 4
    for index, track in enumerate(snap.tracks):
        rects.append(Rect(GUTTER, y, GUTTER + usable, y + ROW_HEIGHT,
                          COLOUR_ROW_ALT if index % 2 else COLOUR_ROW, "row"))
        base = COLOUR_VIDEO if track.media_type == "video" else COLOUR_AUDIO
        for clip in track.clips:
            left = GUTTER + clip.start * scale
            # Never let a short clip vanish, and leave room for the 1 px edge on
            # each side: a sliver still shows that something is there, which is
            # the point of an overview.
            right = max(left + 3, GUTTER + clip.end * scale)
            rects.append(Rect(left, y + 1, right, y + ROW_HEIGHT - 1,
                              COLOUR_UNSYNCED if clip.unsynced else base, "clip", clip))
        y += ROW_HEIGHT + ROW_GAP

    ticks: list[tuple[float, str]] = []
    seconds = total / snap.rate.fps_float
    wanted = max(1, int(usable // 130))
    step_minutes = max(1, round(seconds / 60 / max(1, wanted)))
    step = int(step_minutes * 60 * snap.rate.fps_float)
    if step > 0:
        position = 0
        while position <= total:
            ticks.append((GUTTER + position * scale,
                          format_timecode(position, snap.rate)[:-3]))
            position += step
    return rects, ticks, y + 4


def render_png(snap: TimelineSnapshot, path, *, width: int = 960,
               span: Optional[int] = None, title: str = "") -> None:
    """Draw a snapshot to a PNG, using the same geometry as the live view."""
    from PIL import Image, ImageDraw  # local: only needed for docs and tests

    rects, ticks, height = layout_rects(snap, width, span)
    top = 20 if title else 0
    image = Image.new("RGB", (width, height + top), COLOUR_BG)
    draw = ImageDraw.Draw(image)
    if title:
        draw.text((6, 5), title, fill=COLOUR_TEXT)

    for x, text in ticks:
        draw.line([(x, top + RULER_HEIGHT - 4), (x, top + RULER_HEIGHT)], fill=COLOUR_GRID)
        draw.text((x + 2, top + RULER_HEIGHT - 14), text, fill=COLOUR_TEXT)
    draw.line([(GUTTER, top + RULER_HEIGHT), (width - 8, top + RULER_HEIGHT)], fill=COLOUR_GRID)

    row = 0
    for rect in rects:
        draw.rectangle([rect.x0, rect.y0 + top, rect.x1, rect.y1 + top], fill=rect.fill)
        if rect.kind == "clip" and rect.clip is not None:
            for cx, cy, radius in pit_positions(rect.clip, rect.x1 - rect.x0,
                                                rect.y1 - rect.y0):
                x, y = rect.x0 + cx, rect.y0 + top + cy
                draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                             fill=COLOUR_PIT.get(rect.fill, rect.fill))
        if rect.kind == "clip" and rect.clip is not None and rect.clip.peaks:
            clip = rect.clip
            width = rect.x1 - rect.x0
            middle = (rect.y0 + rect.y1) / 2 + top
            half = (rect.y1 - rect.y0) / 2 - 1
            count = len(clip.peaks)
            columns = int(min(width, count))
            for column in range(max(1, columns)):
                level = clip.peaks[int(column * count / max(1, columns))]
                x = rect.x0 + column * width / max(1, columns)
                extent = max(0.5, level * half)
                draw.line([(x, middle - extent), (x, middle + extent)], fill=COLOUR_WAVE)
        if rect.kind == "clip":
            draw.rectangle([rect.x0, rect.y0 + top, rect.x1, rect.y1 + top],
                           outline=COLOUR_EDGE, width=1)
        if rect.kind == "row":
            draw.text((6, rect.y0 + top + 3), snap.tracks[row].label, fill=COLOUR_TEXT)
            row += 1

    _, _, scale = timeline_scale(snap, image.width, span)
    for pin in snap.markers:
        x = GUTTER + pin.frame * scale
        if x > image.width - 4:
            continue
        draw.line([(x, top + RULER_HEIGHT + 2), (x, height + top - 4)],
                  fill=COLOUR_MARKER)
        draw.polygon([(x, top + RULER_HEIGHT + 8), (x - 4, top + RULER_HEIGHT + 1),
                      (x + 4, top + RULER_HEIGHT + 1)], fill=COLOUR_MARKER)
    image.save(path)


class TimelineCanvas(ttk.Frame):
    """Draws one :class:`TimelineSnapshot`; redraws itself on resize."""

    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.title = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.title, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=2, pady=(0, 3))

        self.canvas = tk.Canvas(self, background=COLOUR_BG, highlightthickness=0, height=120)
        self.canvas.grid(row=1, column=0, sticky="nsew")

        self._heading = title
        self._snapshot: Optional[TimelineSnapshot] = None
        self._scale_duration: Optional[int] = None
        self._boxes: list[tuple[int, int, int, int, ClipBox, Rate]] = []
        self._marker_boxes: list[tuple[float, MarkerPin]] = []

        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _event: self._set_title())

    # ------------------------------------------------------------------ public
    def show(self, snap: Optional[TimelineSnapshot], scale_duration: Optional[int] = None) -> None:
        self._snapshot = snap
        self._scale_duration = scale_duration
        self._set_title()
        self.redraw()

    def clear(self) -> None:
        self.show(None)

    # ----------------------------------------------------------------- drawing
    def _set_title(self, detail: str = "") -> None:
        snap = self._snapshot
        if snap is None:
            self.title.set(self._heading)
            return
        if detail:
            self.title.set(f"{self._heading}  —  {detail}")
            return
        self.title.set(
            f"{self._heading}  —  {snap.name}   "
            f"{len(snap.tracks)} tracks, {snap.clip_count} clips, "
            f"{format_timecode(snap.duration, snap.rate)}"
        )

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        self._boxes.clear()
        self._marker_boxes.clear()

        snap = self._snapshot
        if snap is None or not snap.tracks:
            canvas.create_text(GUTTER, 20, anchor="w", fill=COLOUR_TEXT,
                               text="run a sync to see the timeline",
                               font=(UI_FONT, 11))
            return

        width = max(canvas.winfo_width(), 200)
        rects, ticks, height = layout_rects(snap, width, self._scale_duration)

        canvas.create_line(GUTTER, RULER_HEIGHT, width - 8, RULER_HEIGHT, fill=COLOUR_GRID)
        for x, text in ticks:
            canvas.create_line(x, RULER_HEIGHT - 4, x, RULER_HEIGHT, fill=COLOUR_GRID)
            canvas.create_text(x + 2, RULER_HEIGHT - 10, anchor="w", text=text,
                               fill=COLOUR_TEXT, font=(MONO_FONT, 8))

        row = 0
        for rect in rects:
            canvas.create_rectangle(rect.x0, rect.y0, rect.x1, rect.y1,
                                    fill=rect.fill, outline="")
            if rect.kind == "clip":
                if rect.clip is not None:
                    for cx, cy, radius in pit_positions(
                            rect.clip, rect.x1 - rect.x0, rect.y1 - rect.y0):
                        x, y = rect.x0 + cx, rect.y0 + cy
                        canvas.create_oval(x - radius, y - radius, x + radius,
                                           y + radius,
                                           fill=COLOUR_PIT.get(rect.fill, rect.fill),
                                           outline="")
                if rect.clip is not None and rect.clip.peaks:
                    self._draw_wave(canvas, rect)
                # Edge last, so the waveform cannot paint over the boundary.
                canvas.create_rectangle(rect.x0, rect.y0, rect.x1, rect.y1,
                                        fill="", outline=COLOUR_EDGE, width=1)
            if rect.kind == "row":
                canvas.create_text(GUTTER - 6, (rect.y0 + rect.y1) / 2, anchor="e",
                                   text=snap.tracks[row].label, fill=COLOUR_TEXT,
                                   font=(MONO_FONT, 9))
                row += 1
            elif rect.clip is not None:
                self._boxes.append((int(rect.x0), int(rect.y0), int(rect.x1),
                                    int(rect.y1), rect.clip, snap.rate))

        self._draw_markers(canvas, snap, width, height)
        canvas.configure(height=height)

    def _draw_markers(self, canvas: tk.Canvas, snap: TimelineSnapshot,
                      width: int, height: int) -> None:
        """A line down the strip and a tab at the top, per marker.

        Drawn last so it sits over the clips: a marker is a note about the
        material, and hiding it behind the material would defeat it.
        """
        if not snap.markers:
            return
        _, _, scale = timeline_scale(snap, width, self._scale_duration)
        for pin in snap.markers:
            x = GUTTER + pin.frame * scale
            if x > width - 4:
                continue
            canvas.create_line(x, RULER_HEIGHT + 2, x, height - 4,
                               fill=COLOUR_MARKER, dash=(3, 3))
            canvas.create_polygon(x, RULER_HEIGHT + 8, x - 4, RULER_HEIGHT + 1,
                                  x + 4, RULER_HEIGHT + 1,
                                  fill=COLOUR_MARKER, outline="")
            self._marker_boxes.append((x, pin))

    def _draw_wave(self, canvas: tk.Canvas, rect: Rect) -> None:
        """Draw the clip's peak envelope inside its box."""
        clip = rect.clip
        if clip is None or not clip.peaks:
            return
        width = rect.x1 - rect.x0
        if width < 3:
            return
        middle = (rect.y0 + rect.y1) / 2
        half = (rect.y1 - rect.y0) / 2 - 1
        count = len(clip.peaks)
        # One vertical stroke per pixel column, so a long clip is not drawn from
        # thousands of overlapping lines.
        columns = int(min(width, count))
        for column in range(columns):
            level = clip.peaks[int(column * count / columns)]
            x = rect.x0 + column * width / columns
            extent = max(0.5, level * half)
            canvas.create_line(x, middle - extent, x, middle + extent,
                               fill=COLOUR_WAVE)

    def _hover(self, event: tk.Event) -> None:
        snap = self._snapshot
        if snap is not None and event.y < RULER_HEIGHT + 14:
            for x, pin in self._marker_boxes:
                if abs(event.x - x) <= 5:
                    self._set_title(
                        f"{pin.name}   {format_timecode(pin.frame, snap.rate)}")
                    return
        for left, top, right, bottom, clip, rate in self._boxes:
            if left <= event.x <= right and top <= event.y <= bottom:
                self._set_title(
                    f"{clip.name}   {format_timecode(clip.start, rate)} → "
                    f"{format_timecode(clip.end, rate)}"
                    f"{'   (unmatched)' if clip.unsynced else ''}"
                )
                return
        self._set_title()
