import yaml
from pathlib import Path


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        cfg = yaml.safe_load(f)

    errors = []

    # Validate input files exist
    base = path.parent
    for key in ["gravity_main_shapefile", "manholes_shapefile", "population_units_shapefile"]:
        fp = base / cfg["inputs"][key]
        if not fp.exists():
            errors.append(f"inputs.{key}: file not found — {fp}")

    # Validate manhole target: need at least one of manhole_id or manhole_coordinate
    mh_id = cfg["inputs"].get("manhole_id")
    mh_coord = cfg["inputs"].get("manhole_coordinate")
    if not mh_id and not mh_coord:
        errors.append("inputs: must provide either manhole_id or manhole_coordinate")
    if mh_id and mh_coord:
        errors.append("inputs: provide manhole_id OR manhole_coordinate, not both")
    if mh_coord is not None:
        if not isinstance(mh_coord, list) or len(mh_coord) != 2:
            errors.append("inputs.manhole_coordinate: must be a list of [x, y]")

    # Validate required parameter keys
    required_params = [
        "crs", "node_snap_tolerance_ft", "manhole_snap_distance_ft",
        "pipe_buffer_distance_ft", "selection_radius_ft", "large_catchment_threshold_acres",
        "flag_map_context_buffer_ft", "pdf_flag_types", "max_mappable_cycle_nodes",
        "boundary_method", "close_radius_ft", "basemap_style",
    ]
    for key in required_params:
        if key not in cfg.get("parameters", {}):
            errors.append(f"parameters.{key}: missing")

    # Validate enumerated parameters (single source of truth: boundary.VALID_METHODS)
    from boundary import VALID_METHODS
    valid_methods = set(VALID_METHODS)
    method = cfg.get("parameters", {}).get("boundary_method")
    if method is not None and method not in valid_methods:
        errors.append(f"parameters.boundary_method: '{method}' not in {sorted(valid_methods)}")
    valid_basemaps = {"satellite", "street"}
    style = cfg.get("parameters", {}).get("basemap_style")
    if style is not None and style not in valid_basemaps:
        errors.append(f"parameters.basemap_style: '{style}' not in {sorted(valid_basemaps)}")

    # Validate required output keys
    required_outputs = ["output_polygon", "flags_report", "flag_maps_pdf"]
    for key in required_outputs:
        if key not in cfg.get("outputs", {}):
            errors.append(f"outputs.{key}: missing")

    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    # Resolve all file paths relative to config location
    cfg["_base_dir"] = base
    for key in ["gravity_main_shapefile", "manholes_shapefile", "population_units_shapefile"]:
        cfg["inputs"][key] = str(base / cfg["inputs"][key])
    # Optional inputs — resolved if present, but NOT required to exist (census
    # blocks are only needed for block/hybrid methods; validation paths point at a
    # sibling project used only by --sweep). The consuming module checks existence.
    for key in ["census_blocks_shapefile", "validation_truth_polygons",
                "validation_points", "qa_review_decisions"]:
        if cfg["inputs"].get(key):
            cfg["inputs"][key] = str((base / cfg["inputs"][key]).resolve())
    for key in ["output_polygon", "flags_report", "flag_maps_pdf",
                "large_cycle_suspects", "debug_upstream_pipes",
                "sewershed_parcels", "sewershed_boundary", "validation_overlay",
                "qc_flags_gpkg"]:
        if key in cfg["outputs"]:
            cfg["outputs"][key] = str(base / cfg["outputs"][key])

    # Ensure output directory exists
    out_dir = base / "output"
    out_dir.mkdir(exist_ok=True)

    return cfg
