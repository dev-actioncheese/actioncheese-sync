# Development tree

Where ActionCheese Sync could go next, and why. Items marked **[gap]** came
out of comparing against HB Sync (horiabodeanu.ro/hb-sync), a commercial
Windows+Mac competitor; the rest are our own backlog.

Effort is **S** (a day or less), **M** (a few days), **L** (a week or more).
"Needs" says what has to exist before the work can even be verified — several
of these are blocked on evidence rather than on effort.

```
ActionCheese Sync
│
├── 1. REACH — who can run it at all
│   │   The largest gap by far. Everything else is refinement; this is
│   │   audience. Premiere's biggest user base is on Windows.
│   │
│   ├── 1.1 Windows build                              [gap] M  PARTLY DONE
│   │   ├── cross-platform code ............. DONE, tested (288 tests)
│   │   ├── packaging spec + build script ... WRITTEN, never executed
│   │   ├── verify pathurl shape ............ NEEDS a Windows Premiere export
│   │   └── unsigned, SmartScreen warns ..... decided: no certificate
│   │       (HB Sync is unsigned on Windows too, so this is parity)
│   │
│   ├── 1.2 Linux / AppImage                           [gap] M
│   │   └── audience is DaVinci Resolve only; Premiere has no Linux build
│   │
│   └── 1.3 Intel Mac (universal2)                     [gap] S
│       └── cheap once Windows forces the build matrix open; shrinking market
│
├── 2. FORMATS — what it can read and write
│   │
│   ├── 2.1 FCPXML output                              [gap] M
│   │   ├── reaches Final Cut Pro and gives Resolve a second route
│   │   ├── we already REFUSE .fcpxml input with a clear message
│   │   └── ActionCheese already writes FCPXML, so there is a reference
│   │
│   ├── 2.2 Mixed-folder device separation             [gap] M
│   │   │   HB Sync splits one dumped folder into devices "by file names
│   │   │   and the camera model stored in each file's metadata". We need
│   │   │   one folder per camera, or an explicit Merge.
│   │   ├── we ALREADY read camera model from Sony sidecars
│   │   ├── ffprobe exposes make/model for most cameras
│   │   └── risk: guessing wrong splits a camera in two, which is the
│   │       failure that produced the "tracks are doubled" report
│   │
│   ├── 2.3 CSV report alongside the JSON one          [gap] S
│   │   └── editors read spreadsheets; our JSON is for scripts
│   │
│   └── 2.4 AAF (Avid)                                 [gap] L
│       └── a different format family; only if an Avid user ever asks
│
├── 3. MATCHING — the part that decides whether it works
│   │
│   ├── 3.1 Why six clips on the nera shoot never matched    M  OPEN
│   │   │   Our own backlog, and the highest-value unknown here. They are
│   │   │   placed correctly from the camera clock, so the output is right,
│   │   │   but we do not know whether the audio was genuinely unmatchable
│   │   │   or the matcher is too strict.
│   │   ├── evidence exists: a hand-synced timeline to check against
│   │   └── the rescue rule came out of exactly this kind of look
│   │
│   ├── 3.2 Scale: matching is O(n²)                          M
│   │   │   Measured: 205 clips took 1m49s (~21k pairs, ~5ms each).
│   │   │   Extrapolated: 500 clips ≈ 10 min, 1000 clips ≈ 42 min.
│   │   │   A full wedding with four cameras reaches those numbers.
│   │   ├── a coarse prefilter would cut the pair count before correlation
│   │   ├── HB Sync's "fingerprint" language suggests they solved this by
│   │   │   using hashing as the first stage
│   │   └── cheaper first: skip pairs whose clock timestamps cannot overlap
│   │       once a device offset is known — no new algorithm needed
│   │
│   ├── 3.3 Short clips below the confidence floor            S
│   │   │   Recorded in testCalloutConfidenceSitsBelowTheDefaultFloor: a
│   │   │   shared spoken "action" aligns to within 1 ms but scores 0.245
│   │   │   to 0.294, under the 0.45 floor.
│   │   └── matters for any "sync by callout" workflow
│   │
│   └── 3.4 Per-band weighting                               M  SPECULATIVE
│       └── we standardise 24 bands equally; weighting by which bands
│           actually carry transients might help across mismatched mics.
│           No evidence it is needed. Do not start without a failing case.
│
├── 4. DRIFT
│   │
│   ├── 4.1 Optionally write a corrected WAV           [gap] M
│   │   │   HB Sync writes corrected media next to the original. We write
│   │   │   a speed filter into the XML.
│   │   ├── ours: nothing touched on disk, but depends on the NLE honouring
│   │   │   the filter
│   │   ├── theirs: guaranteed to play right anywhere, at the cost of a new
│   │   │   file and the time to write it
│   │   └── offer both; keep the filter as the default
│   │
│   └── 4.2 Drift on shorter clips                            M
│       └── needs 300 s of probe baseline today, so a shoot of short takes
│           gets no measurement at all. Same shape as the overlap problem
│           that stopped short takes matching before it was fixed.
│
├── 5. OUTPUT AND REPORTING
│   │
│   ├── 5.1 Confidence visible in the NLE                     S
│   │   └── we colour unmatched clips; a confidence note per clip would let
│   │       an editor sort by least-certain
│   │
│   └── 5.2 Remember window size and last options             S
│       └── prefs already exist for the sidebar and last folder
│
└── 6. COMPLIANCE AND HOUSEKEEPING — before any wider release
    │
    ├── 6.1 LGPL decode-only ffmpeg                           M  AGREED
    │   │   The app only ever decodes audio and provably never reaches
    │   │   x264/x265, which are 8.3 MB of the bundle and the sole reason
    │   │   --enable-gpl is set.
    │   ├── removes a forced GPL obligation, making the licence a choice
    │   └── NEEDS testing against real footage before anyone relies on it
    │
    ├── 6.2 THIRD-PARTY-NOTICES.txt + a Licences menu item    S
    │
    └── 6.3 Copyright assignment sweep                        S
        └── 5 documents and 57 SPDX headers; NEEDS the legal name and year
```

## If only three things get done

1. **1.1 Windows.** Nothing else changes who can use this.
2. **3.1 the six unmatched clips.** The one place we might be leaving real
   accuracy on the table, and there is ground truth to check against.
3. **2.1 FCPXML out.** One format, two more NLEs.

## Deliberately not planned

- Code signing on Windows. A yearly cost a free app does not justify, and
  the competitor ships unsigned too.
- Our own fingerprint/hashing matcher as a *replacement*. The continuous
  correlation surface is what gives us the peak-to-runner-up ratio, and that
  ratio is what rescued a camera that scored 0.194 and still landed one frame
  from a hand sync. Hashing is worth exploring as a prefilter (3.2), not as a
  substitute.
