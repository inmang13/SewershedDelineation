"""
CLI runner: attach demographics to each sewershed and write per-site statistics.

Usage:
    python run_demographics.py --config config.yaml
    python run_demographics.py --config config.yaml --skip-fetch   # use cached census data
    python run_demographics.py --config config.yaml --force-fetch  # re-pull even if current

Pipeline: ensure the census cache is current (freshness check) -> load the 24-site
boundary layer, parcels (residential mask), decennial blocks and ACS block-groups
-> apportion each census geography into every sewershed by the dasymetric
residential mask -> write a per-site CSV (dashboard-bound) and a shapefile with
the headline fields (lab sharing).

Needs a free Census API key for the pull (see src/census_api.py). Once the data
is cached, --skip-fetch runs the join offline.
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                    # noqa: E402
from population_join import load_population_units, load_census_blocks  # noqa: E402
import census_data                                                # noqa: E402
import census_api                                                 # noqa: E402
import demographics as demo                                       # noqa: E402


# Full column name -> <=10-char DBF name for the shapefile (lab-sharing) copy.
# The CSV keeps full names.
SHORT_NAMES = {
    "SiteID": "SiteID", "tract": "tract",
    "population": "pop", "n_block_groups": "n_bg",
    "pct_white": "pct_white", "pct_black": "pct_black", "pct_asian": "pct_asian",
    "pct_amind": "pct_amind", "pct_nhpi": "pct_nhpi", "pct_other": "pct_other",
    "pct_two_plus": "pct_2plus", "pct_hispanic": "pct_hisp",
    "median_income": "med_inc", "poverty_rate": "pov_rate",
    "poverty_rate_moe": "pov_moe", "snap_rate": "snap_rate",
    "snap_rate_moe": "snap_moe", "prop_val_mean": "pval_mean",
    "prop_val_median": "pval_med", "n_parcels": "n_parcels",
}


def build_spec(cfg: dict) -> dict:
    """Turn the config `demographics.roles` block into the engine spec, coercing
    income-bin bounds (YAML null upper -> None)."""
    roles = cfg["demographics"]["roles"]
    income_bins = [(code, lo, hi) for code, lo, hi in roles["income_bins"]]
    return {
        "total_pop": roles["total_pop"],
        "race": roles["race"],
        "ethnicity": roles.get("ethnicity", {}),
        "poverty": roles["poverty"],
        "snap": roles["snap"],
        "income_bins": income_bins,
        "property_value_col": cfg["demographics"].get("property_value_col", "PARVAL"),
    }


def load_boundaries(cfg: dict) -> gpd.GeoDataFrame:
    dcfg = cfg["demographics"]
    base = Path(cfg["_base_dir"])
    gpkg = base / dcfg["boundary_gpkg"]
    gdf = gpd.read_file(gpkg, layer=dcfg["boundary_layer"])
    gdf = gdf.to_crs(cfg["parameters"]["crs"])
    return gdf


def load_blocks_with_data(cfg: dict) -> gpd.GeoDataFrame:
    """County blocks (working CRS) joined to the cached decennial counts on GEOID20."""
    blocks = load_census_blocks(cfg)[["GEOID20", "geometry"]].copy()
    blocks["GEOID20"] = blocks["GEOID20"].astype(str)
    dec = census_data.load_cached(cfg, "decennial")
    dec["GEOID20"] = dec["GEOID20"].astype(str)
    return blocks.merge(dec, on="GEOID20", how="left")


def load_block_groups_with_data(cfg: dict, blocks: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Derive block-group geometry by dissolving blocks on the 12-digit GEOID
    prefix, then join the cached ACS estimates/MOEs on GEOID."""
    bg = blocks[["GEOID20", "geometry"]].copy()
    bg["GEOID"] = bg["GEOID20"].str[:12]
    bg = bg.dissolve(by="GEOID").reset_index()[["GEOID", "geometry"]]
    acs = census_data.load_cached(cfg, "acs5")
    acs["GEOID"] = acs["GEOID"].astype(str).str.zfill(12)
    return bg.merge(acs, on="GEOID", how="left")


def served_parcels_for(boundary_geom, parcels, sindex):
    """Parcels intersecting the sewershed boundary + each parcel's clip fraction
    (area inside / total area), used to area-weight property value."""
    cand_idx = list(sindex.query(boundary_geom, predicate="intersects"))
    if not cand_idx:
        return parcels.iloc[[]].copy(), pd.Series(dtype=float)
    cand = parcels.iloc[cand_idx].copy()
    clipped = cand.geometry.intersection(boundary_geom)
    frac = (clipped.area / cand.geometry.area).clip(0, 1)
    return cand, frac


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="use the cached census data, do not contact the API")
    parser.add_argument("--force-fetch", action="store_true",
                        help="re-pull census data even if the cache looks current")
    args = parser.parse_args()

    cfg = load_config(args.config)
    base = Path(cfg["_base_dir"])
    params = cfg["parameters"]

    # --- 1. Freshness: ensure the census cache is current ---
    if not args.skip_fetch:
        print("Census data freshness check:")
        try:
            census_data.check_and_update(cfg, force=args.force_fetch)
        except census_api.CensusAPIError as e:
            print(f"\nCensus pull failed:\n  {e}\n")
            print("If the data is already cached, re-run with --skip-fetch.")
            sys.exit(1)
    else:
        print("Skipping census fetch (--skip-fetch); using cached data.")

    # --- 2. Load inputs ---
    print("\nLoading boundaries, parcels, and census geographies...")
    boundaries = load_boundaries(cfg)
    dcfg = cfg["demographics"]
    site_field, tract_field = dcfg["site_id_field"], dcfg["tract_field"]
    print(f"  boundaries : {len(boundaries)} sites")

    parcels = load_population_units(cfg)
    res = demo.build_residential_mask(parcels, cfg)
    print(f"  parcels    : {len(parcels):,} total, {len(res):,} residential (mask)")
    res_geoms = list(res.geometry.values)
    res_tree = STRtree(res_geoms)
    parcels_sindex = parcels.sindex

    blocks = load_blocks_with_data(cfg)
    bgs = load_block_groups_with_data(cfg, blocks)
    print(f"  blocks     : {len(blocks):,} | block-groups: {len(bgs):,}")

    spec = build_spec(cfg)

    # --- 3. Per-site demographics ---
    blocks_sindex = blocks.sindex
    bgs_sindex = bgs.sindex
    records = []
    for _, site in boundaries.iterrows():
        geom = site.geometry
        sid = site[site_field]
        b_sub = blocks.iloc[list(blocks_sindex.query(geom, predicate="intersects"))]
        bg_sub = bgs.iloc[list(bgs_sindex.query(geom, predicate="intersects"))]
        served, frac = served_parcels_for(geom, parcels, parcels_sindex)

        rec = {site_field: sid, tract_field: site.get(tract_field)}
        rec.update(demo.demographics_for_site(
            geom, b_sub, bg_sub, res_geoms, res_tree, served, spec,
            served_area_weight=frac))
        records.append(rec)
        print(f"    {sid} ({site.get(tract_field)}): pop {rec['population']:,} | "
              f"{rec['n_block_groups']} BGs | "
              f"med inc {rec['median_income']}")

    out = pd.DataFrame(records)

    # --- 4. Write CSV (dashboard) + shapefile (lab) ---
    csv_path = base / cfg["outputs"]["demographics_csv"]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    out.round(2).to_csv(csv_path, index=False)
    print(f"\nWrote {csv_path}  ({len(out)} sites, {len(out.columns)} columns)")

    shp_cols = [c for c in SHORT_NAMES if c in out.columns]
    shp = boundaries[[site_field, "geometry"]].merge(
        out[shp_cols], on=site_field, how="left")
    shp = shp.rename(columns={c: SHORT_NAMES[c] for c in shp_cols})
    shp_path = base / cfg["outputs"]["demographics_shp"]
    shp.round(2).to_file(shp_path)
    print(f"Wrote {shp_path}  (headline fields, 10-char names)")


if __name__ == "__main__":
    main()
