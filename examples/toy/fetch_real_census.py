"""
One-time fetch: real Census block geometry + real ACS/decennial demographics
for the Trinity Park (Durham, NC) area the toy network's streets are drawn
from. Unlike make_toy_data.py, this script talks to the network and is NOT
part of the deterministic test suite — run it by hand only when you mean to
refresh the committed extract.

Two real, public-domain sources, both explicitly designed to be redistributed:
  * TIGER/Line block geometry, via the Census Bureau's TIGERweb REST service
    (no API key needed for geometry).
  * Decennial (2020 PL 94-171) + ACS 5-year demographics, via the same
    src/census_api.py the production pipeline uses — needs a free key (see
    that module's docstring), read from .census_api_key / CENSUS_API_KEY.

This is a small AOI-scoped extract (~130 blocks / ~10 block groups for a
single Durham neighborhood), not the statewide/county-wide file the
production `config.yaml` points at — small enough to commit to a public repo,
which real TIGER + Census data always is (it's what "public domain" means).

Writes, next to this script:
    data/census_blocks.gpkg              real TIGER 2020 blocks, AOI-clipped
    data/census/manifest.json            same schema src/census_data.py reads
    data/census/decennial_2020_blocks.csv
    data/census/acs5_<vintage>_blockgroups.csv

Usage:
    python examples/toy/fetch_real_census.py
"""

import json
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
import census_api                                                  # noqa: E402

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
CENSUS_DIR = DATA_DIR / "census"

# Bounding box around the toy network's real street footprint, in WGS84
# (lon_min, lat_min, lon_max, lat_max) — same AOI make_toy_data.py's real
# coordinates were pulled from.
BBOX = (-78.9180, 35.9970, -78.8990, 36.0110)

TIGERWEB_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/tigerWMS_Census2020/MapServer"
)
BLOCKS_LAYER = 10

STATE_FIPS = "37"      # North Carolina
COUNTY_FIPS = "063"    # Durham County

DECENNIAL_VARS = {
    "P1_001N": "total_population", "P1_003N": "white_alone",
    "P1_004N": "black_alone", "P1_005N": "amind_alone",
    "P1_006N": "asian_alone", "P1_007N": "nhpi_alone",
    "P1_008N": "other_alone", "P1_009N": "two_or_more",
    "P2_002N": "hispanic", "P2_003N": "not_hispanic",
}
ACS5_VARS = {
    "B19013_001E": "median_hh_income", "B19013_001M": "median_hh_income_moe",
    "C17002_001E": "poverty_universe", "C17002_002E": "poverty_under050",
    "C17002_003E": "poverty_050_099", "C17002_001M": "poverty_universe_moe",
    "C17002_002M": "poverty_under050_moe", "C17002_003M": "poverty_050_099_moe",
    "B19058_001E": "households_total", "B19058_002E": "households_snap",
    "B19058_001M": "households_total_moe", "B19058_002M": "households_snap_moe",
    "B19001_001E": "hhinc_total", "B19001_002E": "hhinc_lt10k",
    "B19001_003E": "hhinc_10_15k", "B19001_004E": "hhinc_15_20k",
    "B19001_005E": "hhinc_20_25k", "B19001_006E": "hhinc_25_30k",
    "B19001_007E": "hhinc_30_35k", "B19001_008E": "hhinc_35_40k",
    "B19001_009E": "hhinc_40_45k", "B19001_010E": "hhinc_45_50k",
    "B19001_011E": "hhinc_50_60k", "B19001_012E": "hhinc_60_75k",
    "B19001_013E": "hhinc_75_100k", "B19001_014E": "hhinc_100_125k",
    "B19001_015E": "hhinc_125_150k", "B19001_016E": "hhinc_150_200k",
    "B19001_017E": "hhinc_200k_plus",
}


def fetch_blocks() -> gpd.GeoDataFrame:
    """Real TIGER 2020 block polygons intersecting the toy AOI."""
    url = f"{TIGERWEB_BASE}/{BLOCKS_LAYER}/query"
    params = {
        "geometry": ",".join(str(c) for c in BBOX),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
    }
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    gdf = gpd.GeoDataFrame.from_features(r.json()["features"], crs="EPSG:4326")
    gdf = gdf.rename(columns={"GEOID": "GEOID20", "COUNTY": "COUNTYFP20"})
    gdf["GEOID20"] = gdf["GEOID20"].astype(str)
    return gdf[["GEOID20", "COUNTYFP20", "geometry"]]


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CENSUS_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching real TIGER 2020 block geometry (TIGERweb)...")
    blocks = fetch_blocks()
    blocks_path = DATA_DIR / "census_blocks.gpkg"
    blocks.to_file(blocks_path, layer="census_blocks", driver="GPKG")
    geoid20s = set(blocks["GEOID20"])
    geoids_bg = {g[:12] for g in geoid20s}
    print(f"  wrote {blocks_path}  ({len(blocks)} real blocks, "
          f"{len(geoids_bg)} block groups)")

    key = census_api.resolve_api_key()

    print("Fetching real 2020 decennial (PL 94-171) block counts for "
          f"Durham County (state {STATE_FIPS}, county {COUNTY_FIPS})...")
    dec = census_api.fetch_decennial(
        2020, list(DECENNIAL_VARS), STATE_FIPS, COUNTY_FIPS, key)
    dec = dec[dec["GEOID20"].isin(geoid20s)].reset_index(drop=True)
    dec_path = CENSUS_DIR / "decennial_2020_blocks.csv"
    dec.to_csv(dec_path, index=False)
    print(f"  wrote {dec_path}  ({len(dec)} rows, filtered to the toy AOI)")

    acs_vintage = census_api.latest_vintage(["acs", "acs5"])
    print(f"Fetching real ACS {acs_vintage} 5-year block-group estimates...")
    acs = census_api.fetch_acs5(
        acs_vintage, list(ACS5_VARS), STATE_FIPS, COUNTY_FIPS, key)
    acs["GEOID"] = acs["GEOID"].astype(str).str.zfill(12)
    acs = acs[acs["GEOID"].isin(geoids_bg)].reset_index(drop=True)
    acs_path = CENSUS_DIR / f"acs5_{acs_vintage}_blockgroups.csv"
    acs.to_csv(acs_path, index=False)
    print(f"  wrote {acs_path}  ({len(acs)} rows, filtered to the toy AOI)")

    manifest = {
        "decennial": {
            "vintage": 2020, "dataset": ["dec", "pl"], "geography": "block",
            "variables": list(DECENNIAL_VARS), "effective_variables": list(DECENNIAL_VARS),
            "remap": {}, "download_date": date.today().isoformat(),
            "rows": len(dec), "path": str(dec_path),
        },
        "acs5": {
            "vintage": acs_vintage, "dataset": ["acs", "acs5"], "geography": "block group",
            "variables": list(ACS5_VARS), "effective_variables": list(ACS5_VARS),
            "remap": {}, "download_date": date.today().isoformat(),
            "rows": len(acs), "path": str(acs_path),
        },
    }
    manifest_path = CENSUS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  wrote {manifest_path}")

    print(f"\nReal population in the toy AOI: {dec['P1_001N'].sum():,}")


if __name__ == "__main__":
    main()
