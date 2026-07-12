# Questions for the study area (GIS / collection-system staff)

Data ambiguities the delineation tool surfaced that only the study area can resolve
authoritatively. Each is a specific pipe/topology question with the evidence
that raised it. Resolve → record the answer here and apply the pipe edit in
`QC/qa_review_decisions.csv`.

---

## 1. Pipe 33218 — does it connect the trunk interceptor to the local main, or is it an artifact? (raised 2026-07-08)

**Question:** At manhole **21319** (junction of the trunk interceptor),
pipe **33218** runs from the interceptor manhole (21319) *out* to node **29963**
on the local **332xx** main that our Tract 1.02 sampling point (manhole 29962,
node 27094) sits on. Is 33218 a real gravity connection, is its direction
correct, or is it a digitizing artifact?

**Why we're asking / what it changes:**
- 33218 is the **sole** connection carrying the interceptor's upstream shed
  (including all of Tract **3.02**, 703 pipes) into Tract 1.02's basin. Cut it
  and 3.02 fully detaches from 1.02.
- Its direction looks **hydraulically backwards**: at manhole 21319 the
  interceptor flows *through* (in via **11032**, invert 298.84 → out via
  **11031**, invert 298.82 — consistent through-flow to the plant). 33218
  branches flow *out of* the interceptor manhole back into the local main —
  interceptors collect local flow, they don't discharge into local gravity
  mains. 33218 also carries **no invert data** (the interceptor pipes do), a
  common artifact signature.
- Pipe **11038** (which drains the area toward 3.02) discharges into the
  interceptor at 21319, **not** into the local main 33261 (which ends at a
  separate node 29963, ~16 ft away, unsnapped). So 11038's flow follows the
  interceptor — away toward the plant — unless 33218 routes it back.
- **Impact if 33218 is removed:** Tract 1.02 drops from 3,161 → 2,403 traced
  pipes and 5,933 → 5,016 ac (truth 4,623 ac), **IoU 0.728 → 0.856**. 3.02 is
  unchanged (it was already correct; it just stops being double-claimed).

**Our hypothesis (needs the study area confirmation):** 33218 is either mis-digitized
(true flow 29963 → 21319, i.e. the local main feeding the interceptor) or a
spurious connection. Either way **Tract 3.02 does not drain to Tract 1.02.**

**Related pipes:** 11032 (trunk interceptor), 11031 (interceptor
continuation), 11038, 33261, 33263, 33216/33217 (1.02's local main).
**Map:** `output/preview_png/trunk_interceptor_junction.png`

**Contrast with confirmed cases (for the study area's context):**
- **33250** — an overflow pipe, already confirmed spurious and deleted
  ("pretend it doesn't exist").
- **34374** — the analogous connection for Tract **3.01**; Grace confirmed 3.01
  *does* drain into 1.02, so 34374 is correct and was kept. 22 likewise drains
  into 1.02. 3.02 is the open one.

**Status:** OPEN — awaiting the study area. Do **not** edit 33218 until confirmed.
