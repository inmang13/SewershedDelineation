"""
Census data freshness + local cache management.

Owns the "check the date of the current data and pull newer if available"
feature. Vintage discovery is deterministic — the machine-readable Census
catalog (data.json) already tells us the newest release, so no LLM is needed to
find it. The AI earns its keep only on *schema drift*: when a newer vintage is
pulled, requested variable codes can be renamed or discontinued between vintages,
and a resolver (optionally LLM-backed) maps the old codes to new ones before the
pull commits.

Reproducibility note: an auto-updating pull is non-reproducible BY DESIGN. The
demo/paper path should PIN vintages (config `acs5_vintage` / `decennial_vintage`
set to explicit years); the freshness check is for live/dashboard use. When a
vintage is pinned, `check_and_update` only pulls if the local cache is missing or
its variable set changed — it never chases a newer release.

Local cache layout (all under `data/census/`):
    manifest.json                          one entry per dataset
    decennial_<vintage>_blocks.csv         GEOID20 + count columns
    acs5_<vintage>_blockgroups.csv         GEOID + estimate/MOE columns
"""

from datetime import date
from pathlib import Path
import json

import pandas as pd

import census_api


DECENNIAL_DATASET = ["dec", "pl"]
ACS5_DATASET = ["acs", "acs5"]


def _resolve_vintage(configured, dataset) -> int:
    """A configured vintage is either an explicit year (pinned, reproducible) or
    the string 'latest' (live — asks the catalog)."""
    if configured is None or str(configured).lower() == "latest":
        return census_api.latest_vintage(dataset)
    return int(configured)


def load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {}


def _needs_pull(entry: dict | None, vintage: int, variables: list[str],
                data_path: Path) -> bool:
    """A pull is needed if we have no record, the vintage moved, the requested
    variable set changed, or the cached file is gone."""
    if entry is None:
        return True
    if entry.get("vintage") != vintage:
        return True
    if set(entry.get("variables", [])) != set(variables):
        return True
    if not data_path.exists():
        return True
    return False


def check_drift(vintage: int, dataset_path: str, requested: list[str],
                drift_resolver=None) -> dict:
    """
    Deterministically detect whether every requested variable code still resolves
    in the target vintage. Returns {"ok": bool, "missing": [...], "remap": {...}}.

    `dataset_path` is e.g. "acs/acs5" or "dec/pl". If any code is missing and a
    `drift_resolver(missing, available, vintage)` callable is supplied (this is
    the optional AI seam), it is asked to map the missing codes to replacements;
    a remap only counts if the replacement actually exists in the vintage.
    """
    available = census_api.available_variables(vintage, dataset_path)
    # ACS margin-of-error codes (xxx_M) are queryable companions of every estimate
    # (xxx_E) but are NOT listed as separate keys in variables.json — add them so
    # the drift-check doesn't false-flag legitimate MOE variables as discontinued.
    # Restrict to real table codes (contain "_") so context fields like NAME don't
    # mint junk (NAME -> NAMM).
    available = available | {v[:-1] + "M" for v in available
                             if v.endswith("E") and "_" in v}
    missing = [c for c in requested if c not in available]
    result = {"ok": not missing, "missing": missing, "remap": {}}
    if missing and drift_resolver is not None:
        proposed = drift_resolver(missing, available, vintage) or {}
        valid = {old: new for old, new in proposed.items()
                 if new in available}
        result["remap"] = valid
        result["ok"] = all(c in valid for c in missing)
    return result


def _pull_one(kind: str, vintage: int, variables: list[str], state_fips: str,
              county_fips: str, key: str, data_path: Path, base: Path,
              drift_resolver=None) -> dict:
    """Drift-check, fetch, write CSV, return the manifest entry for one dataset."""
    dataset_path = "dec/pl" if kind == "decennial" else "acs/acs5"
    drift = check_drift(vintage, dataset_path, variables, drift_resolver)
    if not drift["ok"]:
        raise census_api.CensusAPIError(
            f"{kind} vintage {vintage}: variable codes no longer resolve and could "
            f"not be remapped: {drift['missing']}. Update the config variable list "
            f"(see {census_api.API_ROOT}/{vintage}/{dataset_path}/variables.html)."
        )
    effective = [drift["remap"].get(c, c) for c in variables]

    if kind == "decennial":
        df = census_api.fetch_decennial(vintage, effective, state_fips, county_fips, key)
    else:
        df = census_api.fetch_acs5(vintage, effective, state_fips, county_fips, key)

    data_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(data_path, index=False)
    return {
        "vintage": vintage,
        "dataset": DECENNIAL_DATASET if kind == "decennial" else ACS5_DATASET,
        "geography": "block" if kind == "decennial" else "block group",
        "variables": variables,          # record the REQUESTED codes
        "effective_variables": effective, # what was actually pulled (post-remap)
        "remap": drift["remap"],
        "download_date": date.today().isoformat(),
        "rows": len(df),
        "path": str(data_path.relative_to(base)),  # machine-independent — see load_cached
    }


def check_and_update(cfg: dict, force: bool = False, drift_resolver=None) -> dict:
    """
    Ensure the local census cache is current, pulling only what's stale/missing.

    Reads variable lists + vintages from config `census`. Returns the manifest.
    Pulls decennial (block: pop/race/ethnicity) and ACS5 (block group:
    income/poverty/SNAP) for the configured state/county.
    """
    ccfg = cfg.get("census", {})
    base = Path(cfg["_base_dir"])
    data_dir = base / ccfg.get("data_dir", "data/census")
    manifest_path = base / ccfg.get("manifest_path", "data/census/manifest.json")
    state = str(ccfg["state_fips"])
    county = str(ccfg["county_fips"])
    key = census_api.resolve_api_key(cfg)

    dec_vintage = _resolve_vintage(ccfg.get("decennial_vintage", 2020), DECENNIAL_DATASET)
    acs_vintage = _resolve_vintage(ccfg.get("acs5_vintage", "latest"), ACS5_DATASET)

    dec_vars = list(ccfg["decennial_variables"].keys())
    acs_vars = list(ccfg["acs5_variables"].keys())

    manifest = load_manifest(manifest_path)
    jobs = [
        ("decennial", dec_vintage, dec_vars,
         data_dir / f"decennial_{dec_vintage}_blocks.csv"),
        ("acs5", acs_vintage, acs_vars,
         data_dir / f"acs5_{acs_vintage}_blockgroups.csv"),
    ]

    for kind, vintage, variables, data_path in jobs:
        if force or _needs_pull(manifest.get(kind), vintage, variables, data_path):
            print(f"  [{kind}] pulling vintage {vintage} "
                  f"({len(variables)} vars) -> {data_path.name}")
            manifest[kind] = _pull_one(kind, vintage, variables, state, county,
                                       key, data_path, base, drift_resolver)
            print(f"  [{kind}] {manifest[kind]['rows']:,} rows cached")
        else:
            print(f"  [{kind}] up to date (vintage {vintage}, "
                  f"{manifest[kind]['rows']:,} rows)")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_cached(cfg: dict, kind: str) -> pd.DataFrame:
    """Read a cached census table (kind = 'decennial' or 'acs5'). The runner joins
    these to the local TIGER block/block-group geometry."""
    base = Path(cfg["_base_dir"])
    manifest_path = base / cfg.get("census", {}).get(
        "manifest_path", "data/census/manifest.json")
    manifest = load_manifest(manifest_path)
    if kind not in manifest:
        raise FileNotFoundError(
            f"No cached {kind} census data — run check_and_update first.")
    path = base / manifest[kind]["path"]  # stored relative to base — portable across machines
    df = pd.read_csv(path, dtype={"GEOID20": str, "GEOID": str})
    return df
