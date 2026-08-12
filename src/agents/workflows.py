"""Agent output post-processing: progress text and UI spec inference."""
from __future__ import annotations

import json
import os
import re

from src.core.utils import caller_outputs_dir


TOOL_PROGRESS = {
    "geocode_place": lambda a: f"Geocoding {a.get('place_name', 'location')}…",
    "buffer_clip_dissolve": lambda a: f"Clipping to {a.get('buffer_km', '')}km buffer…",
    "list_user_datasets": lambda a: "Checking your datasets…",
    "download_natural_earth_dataset": lambda a: f"Downloading Natural Earth {a.get('scale','')} {a.get('dataset','')}…",
    "geopandas_api": lambda a: f"Running {a.get('function_name', 'analysis')}…",
    "export_to_gpkg": lambda a: "Exporting to GeoPackage…",
    "emit_ui_spec": lambda a: "Preparing visualisation…",
    "run_qgis_algorithm": lambda a: f"Running QGIS: {a.get('algorithm_id','')}…",
    "calculate_isochrones": lambda a: f"Computing {a.get('travel_mode','walking')} isochrones for {a.get('place_name','')}…",
    "clip_layer": lambda a: f"Clipping {a.get('input_path','').split('/')[-1]} to boundary…",
    "download_osm_data": lambda a: f"Downloading OSM {a.get('data_type','')} for {a.get('place_name','')}…",
    "check_qgis_status": lambda a: "Checking QGIS…",
    "query_elevation": lambda a: f"Querying elevation for {a.get('place_name', 'location')}…",
    "download_population_grid": lambda a: f"Fetching population data for {a.get('place_name', 'location')}…",
    "query_zonal_population": lambda a: f"Computing zonal population for {a.get('place_name', 'location')}…",
    "assess_environmental_risk": lambda a: f"Assessing environmental risk for {a.get('place_name', 'location')}…",
    "compute_route": lambda a: f"Computing {a.get('travel_mode', 'driving')} route from {a.get('origin', '')} to {a.get('destination', '')}…",
    "score_sites": lambda a: "Scoring and ranking sites…",
    "spatial_join": lambda a: f"Joining {a.get('points_path','').split('/')[-1]} with {a.get('polygons_path','').split('/')[-1]}…",
    "batch_geocode": lambda a: "Geocoding addresses…",
    "get_admin_boundary": lambda a: f"Fetching boundary for {a.get('place_name', 'location')}…",
    "generate_heatmap": lambda a: f"Generating density heatmap for {a.get('place_name', 'location')}…",
    "find_nearest": lambda a: f"Finding nearest {a.get('targets_path', 'features').split('/')[-1].replace('.gpkg','')} for {a.get('origins_path', 'origins').split('/')[-1].replace('.gpkg','')}…",
    "aggregate_by_region": lambda a: f"Aggregating {a.get('features_path', 'features').split('/')[-1].replace('.gpkg','')} by region ({a.get('agg_func', 'sum')})…",
    "service_gap": lambda a: f"Analysing service gaps for {a.get('service_path', 'service')} in {a.get('place_name', 'area')}…",
    "cluster_points": lambda a: f"Clustering {a.get('input_path','points').split('/')[-1].replace('.gpkg','')} ({a.get('method','dbscan').upper()})…",
    "voronoi": lambda a: f"Generating Voronoi polygons from {a.get('input_path','points').split('/')[-1].replace('.gpkg','')}…",
    "terrain_profile": lambda a: f"Fetching elevation profile from {a.get('start_place','')} to {a.get('end_place','')}…",
}


def get_progress_text(tool_name: str, args_str: str) -> str:
    try:
        args = json.loads(args_str) if args_str else {}
    except Exception:
        args = {}
    fn = TOOL_PROGRESS.get(tool_name)
    if fn:
        try:
            return fn(args)
        except Exception:
            pass
    return f"Running {tool_name}…"


def infer_ui_spec_from_text(text: str):
    """Scan response text for output file references and build a UI spec automatically."""
    clean = re.sub(r"\*+", "", text)

    coord_patterns = [
        r"\[\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\]",
        r"lon[=:\s]+(-?\d+\.?\d*)[,\s]+lat[=:\s]+(-?\d+\.?\d*)",
        r"longitude[=:\s]+(-?\d+\.?\d*)[,\s]+latitude[=:\s]+(-?\d+\.?\d*)",
        r"center[:\s]+(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)",
        r"\((-?\d+\.?\d*)[°\s]*[EW]?,\s*(-?\d+\.?\d*)[°\s]*[NS]?\)",
    ]
    center = None
    for pat in coord_patterns:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            if -180 <= lon <= 180 and -90 <= lat <= 90 and not (lon == 0 and lat == 0):
                center = [lon, lat]
                break

    seen = {}
    for m in re.finditer(
        r'(?:outputs/|/app/geolang/outputs/)([^\s\)\]"\'\*]+\.(?:gpkg|shp|geojson))',
        clean,
    ):
        fname = m.group(1).rstrip(".,;:")
        seen[fname] = {
            "name": fname.replace("_", " ").rsplit(".", 1)[0],
            "file": f"outputs/{fname}",
        }

    for m in re.finditer(
        r"(?:outputs/|/app/geolang/outputs/)([a-zA-Z0-9_\-]+)(?![a-zA-Z0-9_\-\./])",
        clean,
    ):
        fname_base = m.group(1)
        fname = fname_base + ".gpkg"
        if fname not in seen:
            candidate = os.path.join(caller_outputs_dir(), fname)
            if os.path.exists(candidate):
                seen[fname] = {
                    "name": fname_base.replace("_", " "),
                    "file": f"outputs/{fname}",
                }

    if seen:
        layers = list(seen.values())
        spec = {"type": "map", "layers": layers}
        if center:
            spec["center"] = center
            spec["zoom"] = 13
        return spec

    return None
