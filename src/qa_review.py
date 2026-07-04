"""
QA review decisions — the human-in-the-loop feedback file.

After each QA round the reviewer records a decision per flag in a decisions CSV
(inputs.qa_review_decisions, e.g. QC/qa_review_decisions.csv). The pipeline
reads it so review work is never lost between runs:

  decision == "snap"      The flagged endpoint gap is a real connection: the
                          shared snap (node_layer.snap_endpoints pass 3) merges
                          every node within radius_ft of the recorded x/y, so
                          the graph connects there and the flag stops firing.
  decision == "resolved"  Reviewed, no action needed. The flag still fires but
                          carries review_status=resolved — dropped from PDF
                          maps and from the open review_required count.
  decision == "keep"      Reviewed but still open; the comment rides along on
                          the flag so the next reviewer sees the prior finding.

  decision == "flip"      Pipe digitized backwards — reverse its geometry.
  decision == "delete"    Pipe should not be in the network — drop it.
  decision == "extend"    Pipe stops short — move its nearer endpoint to the
                          manhole named in `target` (a FACILITYID or "x,y").
  The three edit decisions are applied by pipe_edits.py, not here.

Matching is exact (flag_type, pipe_id) first, then a proximity fallback (same
flag_type within match_radius_ft of the recorded x/y). The fallback covers ids
that are not stable across runs — disconnected_component numbering depends on
component enumeration order, which can shift when the graph changes.

Columns: flag_type, pipe_id, x, y, decision, radius_ft, target, comment
  - radius_ft applies to snap rows only; blank uses DEFAULT_SNAP_RADIUS_FT.
  - target applies to extend rows only (manhole FACILITYID or "x,y").
  - x/y are required for snap rows (the gap midpoint) and optional for edit
    rows (a locator to disambiguate a duplicate/null FACILITYID).
"""

import csv
import math
from pathlib import Path

# Node-merge + annotation decisions handled in this module; the three edit
# decisions (flip/delete/extend) are validated here but applied by pipe_edits.
EDIT_DECISIONS = {"flip", "delete", "extend"}
VALID_DECISIONS = {"snap", "resolved", "keep"} | EDIT_DECISIONS

# A snap row's merge radius when radius_ft is blank. The recorded x/y is the
# gap midpoint, so the radius must exceed half the gap distance. Tier-2
# snap_gap flags reach 2x the repair radius (20 ft), so a gap wider than 10 ft
# needs an explicit radius_ft — kept small by default to avoid swallowing
# unrelated nearby nodes. A radius that reaches fewer than two node clusters
# raises in snap_endpoints rather than silently merging nothing.
DEFAULT_SNAP_RADIUS_FT = 5.0


def load_review_decisions(path) -> list[dict]:
    """
    Load the decisions CSV. Missing/unset path returns [] (reviewing is
    optional); an unknown decision value is an error — a typo like "reslved"
    silently keeping a flag open would defeat the whole mechanism.
    Rows with a blank decision are skipped (comment-only rows are allowed).
    """
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    decisions = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for lineno, row in enumerate(csv.DictReader(f), start=2):
            dec = (row.get("decision") or "").strip().lower()
            if not dec:
                continue
            if dec not in VALID_DECISIONS:
                raise ValueError(
                    f"{p}, line {lineno}: unknown decision '{dec}' "
                    f"(expected one of {sorted(VALID_DECISIONS)})"
                )
            radius = (row.get("radius_ft") or "").strip()
            x_raw = (row.get("x") or "").strip()
            y_raw = (row.get("y") or "").strip()
            try:
                # x/y optional for edit rows (a locator); required for snap.
                x = float(x_raw) if x_raw else None
                y = float(y_raw) if y_raw else None
                radius_ft = float(radius) if radius else DEFAULT_SNAP_RADIUS_FT
            except (TypeError, ValueError):
                raise ValueError(
                    f"{p}, line {lineno}: x, y (and optional radius_ft) must "
                    f"be numeric — got x={row.get('x')!r} y={row.get('y')!r} "
                    f"radius_ft={row.get('radius_ft')!r}"
                ) from None
            if dec == "snap" and (x is None or y is None):
                raise ValueError(
                    f"{p}, line {lineno}: a snap decision needs x and y "
                    "(the gap midpoint to merge nodes around)"
                )
            decisions.append({
                "flag_type": (row.get("flag_type") or "").strip(),
                "pipe_id":   (row.get("pipe_id") or "").strip(),
                "x":         x,
                "y":         y,
                "decision":  dec,
                "radius_ft": radius_ft,
                "target":    (row.get("target") or "").strip(),
                "comment":   (row.get("comment") or "").strip(),
            })
    return decisions


def manual_snaps(decisions: list[dict]) -> list[dict]:
    """The snap rows, as {x, y, radius_ft} dicts for snap_endpoints pass 3."""
    return [{"x": d["x"], "y": d["y"], "radius_ft": d["radius_ft"]}
            for d in decisions if d["decision"] == "snap"]


def manual_snaps_from_config(cfg: dict) -> list[dict]:
    """Convenience for graph builders: snap list straight from the config."""
    return manual_snaps(
        load_review_decisions(cfg["inputs"].get("qa_review_decisions")))


def pipe_edits(decisions: list[dict]) -> list[dict]:
    """The flip/delete/extend rows, in file order (applied by pipe_edits.py)."""
    return [d for d in decisions if d["decision"] in EDIT_DECISIONS]


def apply_review(flags: list[dict], decisions: list[dict],
                 match_radius_ft: float = 50.0) -> list[dict]:
    """
    Annotate flags in place with review_status / review_comment.

    review_status: "resolved" | "open" (reviewed, kept) | "" (never reviewed).
    Snap decisions are not matched — an applied snap removes its flag, so a
    surviving snap row simply has nothing to annotate.
    """
    for f in flags:
        f["review_status"] = ""
        f["review_comment"] = ""

    # Only resolved/keep annotate flags. snap removes its own flag; the edit
    # decisions (flip/delete/extend) change geometry and are surfaced as their
    # own manual_edit flags, not matched against existing ones.
    reviews = [d for d in decisions if d["decision"] in {"resolved", "keep"}]
    if not reviews:
        return flags

    by_key = {(d["flag_type"], d["pipe_id"]): d for d in reviews}
    matched_keys = set()
    for f in flags:
        d = by_key.get((f["flag_type"], f["pipe_id"]))
        if d is not None:
            _annotate(f, d)
            matched_keys.add((d["flag_type"], d["pipe_id"]))

    # Proximity fallback for decisions whose pipe_id no longer matches.
    # Nearest-first, and each decision is consumed once — dense flag clusters
    # (e.g. the 63489/63491 tangle has 8 gaps within ~25 ft) would otherwise
    # let one decision annotate several flags, or a flag grab whichever
    # decision happened to come first in the CSV.
    leftovers = [d for d in reviews
                 if (d["flag_type"], d["pipe_id"]) not in matched_keys]
    for f in flags:
        if not leftovers:
            break
        if f["review_status"]:
            continue
        geom = f.get("geometry")
        if geom is None or geom.is_empty:
            continue
        best, best_dist = None, match_radius_ft
        for d in leftovers:
            if d["flag_type"] != f["flag_type"]:
                continue
            dist = math.hypot(geom.x - d["x"], geom.y - d["y"])
            if dist <= best_dist:
                best, best_dist = d, dist
        if best is not None:
            _annotate(f, best)
            leftovers.remove(best)
    return flags


def _annotate(flag: dict, decision: dict) -> None:
    flag["review_status"] = ("resolved" if decision["decision"] == "resolved"
                             else "open")
    flag["review_comment"] = decision["comment"]
