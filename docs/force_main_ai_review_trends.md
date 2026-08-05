# Trends from the AI map review, 2026-08-02/03

63 of 63 open review questions gone through visually (satellite imagery + flow
arrows). 40 high confidence, 18 medium, 5 low — those 5 need your eyes or a
second Opus pass regardless of how the rest goes. Full call-by-call detail in
`QC/force_main_ai_suggestions.csv`.

## The recurring bug: a station right next to a junction reads as a discharge

This showed up **4 separate times** (East End, Geer St, and the tiny 2029 stub
twice) and is the single biggest thing worth fixing in the rule itself, not
just patching row by row.

The pattern: some stations aren't a single manhole, they're a wet well manhole
connected by a few feet of gravity pipe to a second manhole that continues
downstream. The out-degree rule sees "flow continues past this point" and calls
it a discharge — when actually both ends of the force main are still inside the
pump station, and there's no real discharge here at all.

**Every case had the same tell**: the force main segment at that terminus was
very short (under 20 ft) AND a lift station facility point sat within a few
feet. Suggested rule: if a terminus classifies as "discharge" but a confirmed
station facility is within the station tolerance (200 ft) AND the connected
force-main run at that end is short, downgrade the classification to
"needs review — possible station-adjacent misread" rather than auto-treating it
as a real junction. This would have caught 4 of my 63 calls automatically.

## Second pattern: force main laid parallel to gravity, not crossing it

3 cases (q28, q29, q35 — all "uncertain/low confidence"): the force main runs
directly on top of or immediately alongside a gravity main for its whole
visible length, rather than crossing and terminating at one clear point. Reads
as a valid discharge/wetwell by distance alone, but there's no visual "this is
where it connects" moment — just two lines sharing a trench.

This is worth a geometric check, not just a distance one: if the force-main
terminus sits within tolerance of a gravity node, but ALSO the two lines run
near-parallel for some distance beforehand (not perpendicular/converging), flag
it as lower confidence automatically. Could compute the angle between the two
lines' final segments — a real junction usually has the force main coming in at
an angle, not creeping in alongside.

## Third: some "no_discharge"/"no_wetwell" gaps are real data gaps, not review items

9 cases where the far end of a pair genuinely had no infrastructure nearby —
deep woods, new subdivisions, golf-course construction. Two subsystems
(Falls Village LS #1 and #2) sit 1800-4700 ft from any mapped gravity main —
their wet well identity is confirmed by your facility layer, but there's no
gravity network drawn anywhere near them. That's not an error to snap, it's
either an area where gravity mains aren't digitized yet, or these stations feed
a force main that runs a long way before reaching mapped gravity (plausible for
new development). Worth flagging as its own category rather than mixing into
the general "no snap, too far" pile — these specific ones might resolve
themselves once a newer gravity layer is available, not because anything here
was wrong.

## Fourth: duplicate/redundant junction candidates

2 cases (system 26, system 31) had two separate discharge candidates a short
distance apart, both visually plausible — almost certainly twin/redundant force
mains from the same station rather than two real junctions. Same shape as your
earlier "snap ok, don't need all three" comment from the first review round.
Suggested rule: when two junction candidates in the same system are within
~50 ft of each other AND both read the same role (both discharge or both
wetwell), group them as one decision in the review file instead of two rows.

## What I could NOT assess from the map alone

- **q34/q39 (pipe 2015)**: no force main geometry rendered in either image
  despite valid coordinates — the pipe may be too short to show at this scale,
  or there's a bug in how it's being drawn. Worth a direct look in GIS.
- **q53, q54, q59, q51, q35**: genuine dead ends with no visible pump structure
  and no facility match — real data gaps on the wet-well side that imagery alone
  can't resolve (no aboveground structure, or structure not visible/obscured).

## High-value confirmations worth flagging on their own

- **q30 (Garrett Rd / 4232 Garrett Rd Lift Station)** — this is your **30804**
  validation site. Confirmed as the wet well with high confidence.
- **q13/q41 (Geer St, 00243)** — this is the connection you flagged in the
  earlier conversation (08374 should snap to 00243). q13 is the real discharge;
  q41 is the misclassified wet well (see the recurring bug above).
