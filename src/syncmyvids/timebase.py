# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame-rate arithmetic for FCP7 XML timelines.

xmeml expresses every position as an integer frame count interpreted against a
``<rate>`` element, which carries an integer ``<timebase>`` plus an ``<ntsc>``
boolean.  When ``ntsc`` is true the real rate is ``timebase * 1000/1001`` --
so a timebase of 30 means 29.97 fps, and 24 means 23.976 fps.  Everything in
this module exists so the rest of the package can work in seconds without ever
guessing at that conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# Timebases for which SMPTE drop-frame timecode is defined.  Drop-frame exists
# to keep NTSC wall-clock error bounded; it is meaningless at any other rate.
DROP_FRAME_TIMEBASES = frozenset({30, 60})


@dataclass(frozen=True, slots=True)
class Rate:
    """A frame rate as xmeml encodes it."""

    timebase: int
    ntsc: bool = False

    @property
    def fps(self) -> Fraction:
        """Exact frame rate as a Fraction, e.g. 30000/1001 for 29.97."""
        if self.ntsc:
            return Fraction(self.timebase * 1000, 1001)
        return Fraction(self.timebase, 1)

    @property
    def fps_float(self) -> float:
        return float(self.fps)

    def frames_to_seconds(self, frames: int | float) -> float:
        return float(frames / self.fps)

    def seconds_to_frames(self, seconds: float) -> int:
        """Convert seconds to the nearest whole frame.

        Rounding is half-away-from-zero rather than Python's default
        banker's rounding, so that a computed offset of exactly half a frame
        moves in a consistent direction instead of alternating with parity.
        """
        exact = seconds * self.fps_float
        return int(exact + 0.5) if exact >= 0 else -int(-exact + 0.5)

    def seconds_to_frames_exact(self, seconds: float) -> float:
        """Convert seconds to a fractional frame count (no rounding)."""
        return seconds * self.fps_float

    @property
    def supports_drop_frame(self) -> bool:
        return self.ntsc and self.timebase in DROP_FRAME_TIMEBASES

    def __str__(self) -> str:
        fps = self.fps_float
        # 23.976 and 29.97 are the conventional spellings; plain rates are ints.
        text = f"{fps:.3f}".rstrip("0").rstrip(".")
        return f"{text} fps"


def format_timecode(frames: int, rate: Rate, drop_frame: bool | None = None) -> str:
    """Render a frame count as SMPTE timecode.

    Drop-frame is used when the rate supports it unless overridden.  The
    separator before the frame field is ``;`` for drop-frame and ``:``
    otherwise, matching the convention FCP and Premiere both use.
    """
    if drop_frame is None:
        drop_frame = rate.supports_drop_frame
    if drop_frame and not rate.supports_drop_frame:
        raise ValueError(f"drop-frame timecode is not defined for {rate}")

    negative = frames < 0
    frames = abs(int(frames))
    nominal = rate.timebase

    if drop_frame:
        # Two frames are dropped every minute except every tenth minute (times
        # the 60/30 ratio for 59.94).  Convert the absolute frame count into a
        # drop-frame label by adding back the frames that were never counted.
        dropped_per_minute = 2 * (nominal // 30)
        frames_per_10min = nominal * 600 - dropped_per_minute * 9
        frames_per_minute = nominal * 60 - dropped_per_minute

        ten_minute_blocks, remainder = divmod(frames, frames_per_10min)
        frames += dropped_per_minute * 9 * ten_minute_blocks
        if remainder >= dropped_per_minute:
            frames += dropped_per_minute * ((remainder - dropped_per_minute) // frames_per_minute)

    ff = frames % nominal
    total_seconds = frames // nominal
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600

    sep = ";" if drop_frame else ":"
    sign = "-" if negative else ""
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


def parse_timecode(text: str, rate: Rate) -> int:
    """Parse SMPTE timecode into a frame count.

    A ``;`` or ``.`` before the frame field marks drop-frame.
    """
    text = text.strip()
    negative = text.startswith("-")
    if negative:
        text = text[1:]

    drop_frame = ";" in text or "." in text
    parts = text.replace(";", ":").replace(".", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"malformed timecode: {text!r}")
    hh, mm, ss, ff = (int(p) for p in parts)

    nominal = rate.timebase
    if drop_frame and not rate.supports_drop_frame:
        raise ValueError(f"drop-frame timecode is not defined for {rate}")

    frames = ((hh * 60 + mm) * 60 + ss) * nominal + ff
    if drop_frame:
        dropped_per_minute = 2 * (nominal // 30)
        total_minutes = hh * 60 + mm
        frames -= dropped_per_minute * (total_minutes - total_minutes // 10)

    return -frames if negative else frames
