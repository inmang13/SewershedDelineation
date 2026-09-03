# Guide for AI coding agents

This file is for an AI agent (Claude Code, Copilot, Cursor, etc.) picking up
this repository cold — as a contributor, a fork maintainer, or someone asked
to extend it. Read this before making changes.

## What this is

A gravity-sewer + optional force-main tracer: given a manhole and a city's
pipe network, it traces upstream and builds a service-area polygon. Pure
Python (GeoPandas/NetworkX/Shapely), config-driven, no ArcPy/QGIS. See
`README.md` for the full picture and `docs/decision_log.md` for *why* things
are built the way they are — many design choices here look arbitrary until
you read the incident or measurement that produced them.

## Hard rules — do not violate these

- **Never modify a source shapefile.** All human corrections (snap/flip/
  delete/extend a gravity pipe, confirm a force-main junction, override a
  direction call) go through a reviewer-edited CSV under `QC/`, applied
  **in memory** on every graph load. The committed `data/` layers (where
  they exist — most are gitignored, not redistributable) are never rewritten.
  If you find yourself wanting to "just fix the geometry," find the QC file
  that fixes it declaratively instead.
- **One module per phase, one file per module** (`src/graph_builder.py`,
  `src/traversal.py`, `src/force_main_classify.py`, …). A file that grows to
  do two unrelated phases' work should be split, not extended — this was a
  real code-review finding (`force_mains.py` split into four files, see
  `docs/decision_log.md`).
- **No hardcoded paths or magic numbers.** Everything that varies by city or
  by run lives in `config.yaml`. If a new parameter needs a default, add it
  to the module's own default dict (see `force_mains.DEFAULT_INCLUDE` for the
  pattern), not a bare literal in the function body.
- **Fail loud on ambiguous data, never guess.** This codebase repeatedly
  chooses "raise with a specific, actionable message" over "silently pick the
  nearest plausible answer" — see `force_main_wiring.add_force_main_edges`'s
  snap-tolerance check, or `apply_direction_overrides`'s no-match error. Match
  that standard in new code: a silent wrong answer is worse than a loud stop.
- **Don't touch `docs/decision_log.md`'s past entries.** It's append-only. Add
  a new dated entry for anything you change that isn't obvious from the diff
  alone — the next reader (human or agent) needs the *why*, not just the
  *what*.

## Before changing anything

1. Read the relevant module's docstring — it usually explains a prior wrong
   approach and why the current one replaced it. Reverting to the "obvious"
   simpler version is often re-introducing a bug someone already found.
2. Check `docs/roadmap.md` for open items in the area you're touching — a
   half-finished caveat may already be tracked there.
3. Run the test suite (`pytest tests/`) before AND after your change. 186
   tests as of the last force-main work; a shrinking count with no comment
   explaining why is a red flag, not a cleanup.

## Testing

```bash
pytest tests/                          # full suite
python examples/toy/make_toy_data.py   # regenerate the synthetic network (rarely needed)
python run.py --config examples/toy/config.yaml --sites MH01,MH04,MH08
```

The toy network (`examples/toy/`) is fully synthetic and committed — use it
for any change you want to sanity-check without needing real municipal data,
which is never available in this repo.

## Adding a new pipeline phase

Follow the existing shape: a new `src/<phase>.py` module, a `run_<phase>.py`
CLI runner if it's independently invokable, a `tests/test_<phase>.py`, and an
entry in `docs/decision_log.md` explaining what problem it solves. Look at
`force_main_topology.py` / `force_main_classify.py` / `force_main_review.py`
as a recent, deliberately-split example of the pattern.

## What NOT to do

- Don't add a dependency to fix something 20 lines of NumPy/Shapely already
  does — the dependency list is deliberately short (see `requirements.txt`'s
  comments on why each one is there).
- Don't relax a validation check (a tolerance, a required column, a raised
  error) to make your change pass without understanding why the check exists
  — read the surrounding comment first; nearly every guard here was added
  after a specific real failure.
- Don't commit unrelated work in the same commit as a focused fix — a past
  commit here bundled an unrelated 738-line module into a force-main fix and
  it's now a known, regretted wart in the history (`docs/roadmap.md`).
