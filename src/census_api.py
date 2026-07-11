"""
Census API client for the demographic-join phase.

Pulls the socioeconomic variables that characterize each sewershed's population:
race + ethnicity from the 2020 Decennial (PL 94-171, block level) and income /
poverty / SNAP from the ACS 5-year (block-group level). Everything comes from the
public-domain Census API so the data is legally committable to the repo (unlike
the no-redistribution NHGIS extract this replaces).

The Census API now requires a key on EVERY request — keyless access was
deprecated. Get a free key instantly at
    https://api.census.gov/data/key_signup.html
and expose it one of three ways (checked in this order):
    1. environment variable  CENSUS_API_KEY
    2. a file  .census_api_key  in the project root (one line, the key)
    3. config  inputs.census_api_key

Only the data queries need the key; the dataset catalog (data.json) used by
`latest_vintage` is keyless.

Geography note: the the city target is state FIPS 37, county FIPS 063. Decennial
block IDs are the 15-digit GEOID (state+county+tract+block) matching the local
TIGER `GEOID20`; ACS block-group IDs are the 12-digit prefix of that.
"""

from pathlib import Path
import os

import pandas as pd
import requests

CATALOG_URL = "https://api.census.gov/data.json"
API_ROOT = "https://api.census.gov/data"
MISSING_KEY_MARKER = "missing_key"


class CensusAPIError(RuntimeError):
    """A Census API request failed (bad key, unknown variable, HTTP error)."""


def resolve_api_key(cfg: dict | None = None) -> str:
    """
    Find the Census API key from env var, project-root file, or config.

    Raises CensusAPIError with signup instructions if none is found — the pull
    cannot proceed without one.
    """
    key = os.environ.get("CENSUS_API_KEY")
    if key:
        return key.strip()

    # Project-root file. Prefer the config's base dir; fall back to this file's
    # grandparent (src/ -> project root).
    base = None
    if cfg is not None:
        base = cfg.get("_base_dir")
    base = Path(base) if base else Path(__file__).resolve().parent.parent
    key_file = base / ".census_api_key"
    if key_file.exists():
        text = key_file.read_text(encoding="utf-8").strip()
        if text:
            return text

    if cfg is not None:
        cfg_key = cfg.get("inputs", {}).get("census_api_key")
        if cfg_key:
            return str(cfg_key).strip()

    raise CensusAPIError(
        "No Census API key found. Get a free one at "
        "https://api.census.gov/data/key_signup.html then set the CENSUS_API_KEY "
        "environment variable, write it to a `.census_api_key` file in the project "
        "root, or put it in config inputs.census_api_key."
    )


def latest_vintage(dataset: list[str]) -> int:
    """
    Return the newest available vintage year for a dataset family (keyless).

    `dataset` is the Census `c_dataset` list, e.g. ["acs", "acs5"] or
    ["dec", "pl"]. Queries the machine-readable catalog so freshness discovery is
    deterministic (no LLM needed).
    """
    r = requests.get(CATALOG_URL, timeout=60)
    if r.status_code != 200:
        raise CensusAPIError(
            f"Could not fetch the dataset catalog {CATALOG_URL}: HTTP {r.status_code}")
    cat = r.json()
    vintages = {
        d.get("c_vintage")
        for d in cat["dataset"]
        if d.get("c_dataset") == dataset and d.get("c_vintage")
    }
    if not vintages:
        raise CensusAPIError(f"No vintages found in catalog for dataset {dataset}")
    return max(vintages)


def available_variables(vintage: int, dataset_path: str) -> set[str]:
    """
    Return the set of variable codes a given vintage/dataset exposes (keyless).

    `dataset_path` is the API path segment, e.g. "acs/acs5" or "dec/pl". Used by
    the freshness drift-check to confirm requested codes still resolve before a
    pull.
    """
    url = f"{API_ROOT}/{vintage}/{dataset_path}/variables.json"
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise CensusAPIError(
            f"Could not fetch variable catalog {url}: HTTP {r.status_code}"
        )
    return set(r.json().get("variables", {}).keys())


def _query(vintage: int, dataset_path: str, variables: list[str],
           geo_for: str, geo_in: list[str], key: str) -> pd.DataFrame:
    """
    Low-level Census API GET. Returns a DataFrame with one column per requested
    variable (kept as strings — caller coerces) plus the geography id columns the
    API appends (state, county, tract, block / block group).
    """
    params = [("get", ",".join(variables)), ("for", geo_for)]
    params += [("in", clause) for clause in geo_in]
    params.append(("key", key))
    url = f"{API_ROOT}/{vintage}/{dataset_path}"
    r = requests.get(url, params=params, timeout=180)

    if MISSING_KEY_MARKER in r.url:
        raise CensusAPIError(
            "Census API rejected the request as missing/invalid key. Verify the "
            "key from https://api.census.gov/data/key_signup.html is correct."
        )
    if r.status_code != 200:
        raise CensusAPIError(
            f"Census API HTTP {r.status_code} for {url}\n  params={params[:-1]}"
            f"\n  body: {r.text[:300]}"
        )
    try:
        rows = r.json()
    except ValueError as e:
        raise CensusAPIError(
            f"Census API returned non-JSON (likely a bad variable code or "
            f"geography) for {url}: {r.text[:200]}"
        ) from e

    header, *data = rows
    return pd.DataFrame(data, columns=header)


def fetch_decennial(vintage: int, variables: list[str], state_fips: str,
                    county_fips: str, key: str) -> pd.DataFrame:
    """
    Pull decennial (PL 94-171 by default vintage=2020) variables by BLOCK for one
    county. Returns a DataFrame keyed by the 15-digit `GEOID20` (matching the
    local TIGER block file) with each variable coerced to a nullable integer.
    """
    df = _query(
        vintage, "dec/pl", variables,
        geo_for="block:*",
        geo_in=[f"state:{state_fips}", f"county:{county_fips}", "tract:*"],
        key=key,
    )
    df["GEOID20"] = (df["state"] + df["county"] + df["tract"] + df["block"])
    for v in variables:
        df[v] = pd.to_numeric(df[v], errors="coerce").astype("Int64")
    return df[["GEOID20", *variables]]


def fetch_acs5(vintage: int, variables: list[str], state_fips: str,
               county_fips: str, key: str) -> pd.DataFrame:
    """
    Pull ACS 5-year variables by BLOCK GROUP for one county. Returns a DataFrame
    keyed by the 12-digit block-group `GEOID` with each variable coerced to a
    nullable numeric (estimates `_E` and margins of error `_M` alike). ACS uses
    -666666666-style sentinels for suppressed cells; those become NA.
    """
    df = _query(
        vintage, "acs/acs5", variables,
        geo_for="block group:*",
        geo_in=[f"state:{state_fips}", f"county:{county_fips}", "tract:*"],
        key=key,
    )
    df["GEOID"] = (df["state"] + df["county"] + df["tract"] + df["block group"])
    for v in variables:
        col = pd.to_numeric(df[v], errors="coerce")
        # ACS jam/suppression sentinels are large-magnitude negatives.
        df[v] = col.where(col > -100000000)
    return df[["GEOID", *variables]]
