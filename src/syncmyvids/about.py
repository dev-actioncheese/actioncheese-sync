# SPDX-License-Identifier: GPL-3.0-or-later
"""Author, contact and user-facing help text.

Kept in one module so the GUI dialogs, the CLI and the README never drift apart.
"""

from __future__ import annotations

from . import __version__

#: The product name.  The Python package stays ``syncmyvids`` -- renaming the
#: module would break every import and script for no user-visible gain -- so
#: this is the single place the displayed name lives.
APP_NAME = "ActionCheese Sync"
COMPANION_LINE = "Companion to ActionCheese - Multicam Remote"

COPYRIGHT = "Copyright (C) 2026 Gabriel (Focalizat)"
LICENCE = (
    "Free software under the GNU General Public License, version 3 or later. "
    "It comes with ABSOLUTELY NO WARRANTY. You may redistribute it under those "
    "terms; see the LICENSE file, or https://www.gnu.org/licenses/gpl-3.0.html."
)

AUTHOR = "Gabriel"
ORGANISATION = "Focalizat"
#: Public contact for anything about the app.
CONTACT_EMAIL = "contact@actioncheese.com"
#: Bug reports and testing feedback, which go somewhere different on purpose:
#: a crash report with a timeline attached is no use in a general inbox.
BUG_EMAIL = "dev@actioncheese.com"

TAGLINE = "Waveform-based multicam sync for Final Cut Pro XML timelines"

SUMMARY = (
    f"{APP_NAME} aligns multicam footage the way PluralEyes or Syncaila do, by "
    "listening to the audio rather than trusting timecode. Feed it a timeline "
    "exported from Premiere Pro with one camera or recorder per track; it works "
    "out where every clip really belongs and writes the timeline back in sync. "
    "Clips it cannot match are never dropped."
)

# --------------------------------------------------------------------------- #
# The companion app, as advertised in the window's sidebar.
#
# All of it lives here rather than in the widget code so the wording, the price
# and the link can be changed without touching the layout -- and so there is
# one place to look when any of them goes out of date.
# --------------------------------------------------------------------------- #

COMPANION_NAME = "ActionCheese - Multicam Remote"
COMPANION_PITCH = ("Turn your iPhones into a synced multicam crew. "
                   "No Wi-Fi, no laptop, no crew.")
COMPANION_POINTS = (
    "Up to 4 iPhones, switched live from a Mac, iPad or another iPhone",
    'Say "action": every camera rolls, with shared timecode',
    "Shoot folders open here already in sync",
    "Cut and export each take right on the deck phone",
)
COMPANION_PRICING = "Free to start. Pro from $39.99/year."
COMPANION_URL = "https://actioncheese.com/"
COMPANION_BUTTON = "Learn more"
#: Replaced by the App Store link at launch. Kept as one constant so that is a
#: one-line change rather than a hunt through the layout code.
COMPANION_STORE_NOTE = "App Store: coming soon"
#: The icon file, copied from the ActionCheese project into this repo's assets.
COMPANION_ICON = "actioncheese-icon.png"


CREDITS = (
    "Built on ffmpeg for decoding, and NumPy and SciPy for the correlation maths. "
    "The envelope-correlation approach follows the align-videos-by-sound and "
    "audalign projects; GCC-PHAT is standard practice in time-delay estimation."
)


def about_text() -> str:
    return f"""{APP_NAME} {__version__}
{TAGLINE}
{COMPANION_LINE}

{SUMMARY}

Author:  {AUTHOR} ({ORGANISATION})
Bugs:    {BUG_EMAIL}
Contact: {CONTACT_EMAIL}

{COPYRIGHT}
{LICENCE}

{CREDITS}
"""


HELP_TEXT = f"""{APP_NAME} -- {COMPANION_LINE}

GETTING STARTED

Add your material -- any mix of the three -- and press Sync:

  A timeline     In Premiere, put one camera or recorder on each track, then
                 File > Export > Final Cut Pro XML, and add that .xml.
  A camera folder   One folder per camera or recorder. Card structure does not
                 matter; everything under the folder is that one device. Sony
                 cards are read from their own clip sidecars, which know
                 exactly when each take started. Pick several folders at once,
                 and the next dialog opens where the last one left off, so a
                 shoot's cameras are one click apart rather than one walk
                 through the tree.

  One camera, several cards   Select its folders in the list and press Merge.
                 They become a single camera, which matters for more than
                 tidiness: clips from one device are never matched against
                 each other, on the principle that a camera cannot record two
                 things at once, and that has to hold across the card boundary
                 too. Press Split to undo it. On the command line this is
                 --merge, once per camera.
  An ActionCheese shoot folder   The whole day, as the remote app wrote it.
                 Every phone comes in as its own camera, already in sync with
                 the others, and any camera cards you dropped inside the shoot
                 folder are picked up as further cameras.

Then import the result back into Premiere with File > Import.

The output sequence is named after the first input with "_synced" appended, and
carries a new identity, so it appears beside the original in your bin rather
than replacing it.

With a single timeline and nothing else, that timeline's own document is
edited, so everything Premiere put in it survives untouched. With anything
else, a new timeline has to be assembled from what you gave it: the clips keep
their media, their in and out points and their positions, but effects, labels
and speed changes already on them do not come along.


WHAT THE OPTIONS DO

Close empty gaps
    Removes every stretch of timeline where nothing plays on any track. A
    region where the picture is black but a recorder is still rolling is not a
    gap and is left alone. Sync is unaffected: gaps never cut through a clip,
    so everything sharing a moment shifts by the same amount.

Correct recorder drift
    Recorders' clocks diverge -- a field recorder can drift tens of
    milliseconds per hour against a camera. Where the drift is large enough to
    measure reliably, a small speed correction is written. This is applied
    only to standalone audio-recorder clips; picture is never resampled.

Trust already-synced clips
    Clips that arrive with their timing already stated -- from an ActionCheese
    shoot -- are normally treated as strong evidence rather than as fact: where
    the audio has something confident to say, the audio wins. That is not a
    slight on the app. Measured against its own index on two real shoots, the
    stated times were good to about 0.03 s for most phones, but one handset was
    out by up to 0.7 s, and the audio caught it. Turn this on to keep the
    stated timing exactly as given -- worth doing when you know the clocks were
    right, or when the material has no usable audio to check them against.
    Either way, the report lists every clip the audio moved and by how much, so
    you can see for yourself how well the timing held up.

Unmatched clips: inline / append
    "inline" drops each unmatched clip in just after the last clip shot before
    it, so it sits at the right point in the story. "append" puts them all
    after the last synced clip instead. Either way they move to new tracks of
    their own and nothing is deleted.

Unmatched picture / Unmatched audio
    A clip that could not be matched stays on the track it came from, as long
    as nothing else occupies that moment. It is already marked in the label
    colour, and moving it would split one camera's footage across two rows for
    nothing -- on a four-camera shoot that turned four used video tracks into
    seven, with three cameras appearing twice. Only when the moment is already
    taken does the clip need a track of its own.

    Set either to "split" to give it a new track regardless, which is how
    earlier versions always behaved.

    Audio has one extra wrinkle: Premiere exports stereo as two mono clips, and
    they cannot be folded onto a single track, because the format selects a
    channel rather than the pair -- merging them would silently drop the right
    channel.

Mark with
    Label colour for unmatched clips, so they stand out in the timeline.
    Premiere has no label literally called "Red" -- Rose is its red.

Reference track
    The track to hold still, e.g. V1 or A3. Everything else moves around it.
    Left blank, the busiest camera track is used.

Min confidence
    How strong an audio match has to be before it is accepted, from 0 to 1.
    Lower it to catch more clips at the risk of a wrong placement; raise it to
    be stricter. 0.45 is a good default.

Name suffix
    Appended to the sequence name in the output.


MARKERS

Markers you dropped while recording come across with the footage, and they
follow it: each one is tied to the clip that was playing when you made it, and
moves wherever that clip ends up. A marker made between takes, with nothing
rolling, keeps its distance from the takes on either side.

They travel with the clip rather than staying at their frame because a marker
records a moment, not a position. Left at its frame it would end up pointing
at whatever slid into that spot -- worse than losing it, because it would look
right.

Markers from an ActionCheese shoot follow that shoot's own cameras, never a
camera card, since the shoot's clock is the one that timed them. A camera whose
clock was forty seconds out would otherwise take them with it.


THE TIMELINE VIEW

The Timeline tab shows the sequence as imported and as exported, drawn to the
same time scale so the two can be compared directly, with each clip's waveform
drawn inside it. Cyan is picture, amber is sound, rose is anything the sync
could not place. Hover a clip to see its name and timecode. The Log tab has the
full text report.

There is one row per *device*, not per track: a camera's own audio is part of
the same clip, so showing it separately would just repeat the row above, and a
stereo recorder is one recording rather than one row per channel. Tracks with
no clips are not shown -- and are removed from the exported XML too, so you do
not import a column of empty tracks. Use --keep-empty-tracks to keep them.

A typical "before" looks like one solid bar per track -- every clip butted end
to end, which is how footage arrives before syncing. The "after" breaks into
separate blocks at their true positions.


READING THE RESULT

synced      Matched and repositioned.
groups      Internally in sync, but with nothing in the audio connecting them
            to the rest. This is normal on a sequential shoot -- one camera and
            one recorder, take after take -- where no overlapping material
            bridges one take to the next. Each group is correct within itself.
unmatched   No confident match. Colour-marked, and placed at the moment it was
            actually shot whenever that can be worked out: cameras stamp every
            file with its recording time, and once one clip from a device has
            matched, that device's clock offset to the timeline is known, so all
            its other clips can be placed from their timestamps. Without a
            usable timestamp the clip simply follows the last synced clip from
            the same camera or recorder. Usually means the clip
            has no usable audio, or nothing else was recording at that moment.

A device cannot record two things at once, so its clips are kept in shooting
order and never overlap each other. When a match would place a take somewhere
its own device was already recording, that match is dropped and the clip is
reported as unmatched -- the sync was almost certainly wrong. Where recording
timestamps are available the same check goes further: two clips from one camera
must sit as far apart on the timeline as that camera's own clock says they were
shot, which catches a wrong match that is in the right order and overlaps
nothing yet sits minutes from where it belongs. Occasionally the
footage shot between two synced takes is longer than the room the sync left
between them; the clip is then placed after the blockage rather than through
it, which is the one case where the order is broken to keep the tracks honest.


IF SOMETHING LOOKS WRONG

Nothing syncs
    Check the media is where the XML says it is -- the log names any file it
    could not open. Sync is audio-based, so silent clips can never match.

A whole camera is missing
    A folder is one device, so two cameras need two folders. If you added a
    card folder that sits inside an ActionCheese shoot folder you also added,
    it is used once, not twice -- the log says so.

A clip is in the wrong place
    Raise Min confidence and run again. Send the XML and the report to
    {BUG_EMAIL} if it persists.

Premiere warns about a speed effect
    Turn off "Correct recorder drift" and run again, then report it.

The app will not open on another Mac
    Download builds are signed and notarized. A build you made yourself is not:
    right-click it, choose Open, then Open again (on macOS 15 and later use
    System Settings > Privacy & Security > Open Anyway).


Bugs, sample projects and testing feedback: {BUG_EMAIL}
Anything else: {CONTACT_EMAIL}

{COPYRIGHT}
{LICENCE}
"""
