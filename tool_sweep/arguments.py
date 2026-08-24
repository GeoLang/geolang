"""Sample arguments for every tool the manifest advertises, one entry per tool.

The nightly sweep (`python -m tool_sweep.runner`) runs every entry through the
real HTTP path against a live platform stack. `tests/test_tool_sweep.py` runs
the offline ones through the in-process app on every push. Both read this table,
so the two cannot drift, and a tool in the manifest with no entry here fails the
sweep instead of shipping unswept.

`external` marks a tool whose code can reach a third-party host on these
arguments: Overpass, Nominatim, opentopodata, WorldPop, the Natural Earth
downloads, the public Valhalla. Several of those try a platform service first,
so the mark means "can leave the network", not "always does". The sweep lists
their failures separately, so a third party being down reads differently from a
broken tool.

`offline` marks a tool that needs no network and no platform service, so the
per-push subset can run it. The staged layers below are not network, the sweep
uploads them itself.

`after` names a tool that must run first. `run_workflow` refuses a manifest
`plan_workflow` did not validate, and both halves must be the same text.

`needs_approval` marks a tool that refuses what nobody approved in the viewer.
The sweep posts the entry's `manifest_toml` to `/workflow/approve` first, which
is the call the approve button makes.

`needs_qgis` marks the tools that need the QGIS bindings, which only the
platform image has. They are offline, so the per-push suite runs them wherever
QGIS starts and skips them where it does not.

`crashes_executor` marks a tool that takes the process down with it. Nothing is
marked today: the QGIS four each built their own QgsApplication, and whichever
ran second segfaulted, until they moved to one session per process. The nightly
runs marked tools anyway, `--skip-crashing` leaves them out of a run that needs
the rest of the table to mean something.

Arguments stay small: a few hundred metres of Monaco, five points, buffers under
a kilometre.
"""

from dataclasses import dataclass, field

POINTS_LAYER = "sweep_points.geojson"
POLYGONS_LAYER = "sweep_polygons.geojson"

# somewhere every platform service has data for: itinera and geokode are built
# from the monaco extract in CI
SWEEP_PLACE = "Monaco"
SWEEP_CENTER_LON = 7.4225
SWEEP_CENTER_LAT = 43.7345


def _point(lon, lat, name, category, population):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"name": name, "category": category, "population": population},
    }


def _box(min_lon, min_lat, max_lon, max_lat, region):
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"region": region},
    }


POINTS_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        _point(7.4210, 43.7345, "Port", "harbour", 120),
        _point(7.4280, 43.7395, "Casino", "leisure", 80),
        _point(7.4160, 43.7370, "Station", "transport", 200),
        _point(7.4255, 43.7310, "Museum", "culture", 60),
        _point(7.4185, 43.7325, "Market", "retail", 140),
    ],
}

POLYGONS_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        _box(7.4130, 43.7290, 7.4230, 43.7400, "west"),
        _box(7.4230, 43.7290, 7.4320, 43.7400, "east"),
    ],
}

STAGED_LAYERS = {POINTS_LAYER: POINTS_GEOJSON, POLYGONS_LAYER: POLYGONS_GEOJSON}

# one source, one transform, one sink: the smallest manifest that exercises
# geodukt's read, operation and write. plan_workflow and run_workflow must post
# the same text or the plan gate refuses the run.
SWEEP_MANIFEST_TOML = f"""[project]
name = "tool-sweep"

[[source]]
name = "points"
format = "geojson"
path = "{POINTS_LAYER}"

[[transform]]
name = "catchment"
input = "points"
operation = "buffer"
distance = 25.0

[[sink]]
name = "out"
input = "catchment"
format = "gpkg"
path = "sweep_workflow.gpkg"
"""


@dataclass(frozen=True)
class ToolSample:
    args: dict = field(default_factory=dict)
    external: bool = False
    offline: bool = False
    needs_qgis: bool = False
    crashes_executor: bool = False
    after: str | None = None
    needs_approval: bool = False


SWEEP_ARGUMENTS: dict[str, ToolSample] = {
    "list_outputs": ToolSample(offline=True),
    "list_user_datasets": ToolSample(offline=True),
    "emit_ui_spec": ToolSample(
        args={
            "ui_type": "map",
            "center_lon": SWEEP_CENTER_LON,
            "center_lat": SWEEP_CENTER_LAT,
            "zoom": 14,
        },
        offline=True,
    ),
    "viewer_control": ToolSample(
        args={
            "action": "fly_to",
            "lon": SWEEP_CENTER_LON,
            "lat": SWEEP_CENTER_LAT,
            "height": 800,
        },
        offline=True,
    ),
    "sql_query": ToolSample(
        args={"sql": "SELECT 1 AS one", "show_on_map": False, "fit": False},
        offline=True,
    ),
    "geopandas_api": ToolSample(
        args={"function_name": "read_file", "dataset_path": POINTS_LAYER},
        offline=True,
    ),
    "buffer_clip_dissolve": ToolSample(
        args={
            "input_path": POINTS_LAYER,
            "center_lon": SWEEP_CENTER_LON,
            "center_lat": SWEEP_CENTER_LAT,
            "buffer_km": 0.8,
            "output_filename": "sweep_buffer.gpkg",
        },
        offline=True,
    ),
    "clip_layer": ToolSample(
        args={
            "input_path": POINTS_LAYER,
            "clip_path": POLYGONS_LAYER,
            "output_filename": "sweep_clip",
        },
        offline=True,
    ),
    "spatial_join": ToolSample(
        args={
            "points_path": POINTS_LAYER,
            "polygons_path": POLYGONS_LAYER,
            "how": "inner",
            "output_filename": "sweep_join",
        },
        offline=True,
    ),
    "aggregate_by_region": ToolSample(
        args={
            "regions_path": POLYGONS_LAYER,
            "features_path": POINTS_LAYER,
            "agg_columns": "population",
            "agg_func": "sum",
            "region_label_col": "region",
            "output_filename": "sweep_aggregate",
        },
        offline=True,
    ),
    "cluster_points": ToolSample(
        args={
            "input_path": POINTS_LAYER,
            "method": "dbscan",
            "eps_km": 0.5,
            "min_samples": 2,
            "output_filename": "sweep_clusters",
        },
        offline=True,
    ),
    "voronoi": ToolSample(
        args={
            "input_path": POINTS_LAYER,
            "boundary_path": POLYGONS_LAYER,
            "label_col": "name",
            "output_filename": "sweep_voronoi",
        },
        offline=True,
    ),
    "find_nearest": ToolSample(
        args={
            "origins_path": POINTS_LAYER,
            "targets_path": POLYGONS_LAYER,
            "k": 1,
            "output_filename": "sweep_nearest",
        },
        offline=True,
    ),
    "compare_layers": ToolSample(
        args={
            "layer_a_path": POLYGONS_LAYER,
            "layer_b_path": POLYGONS_LAYER,
            "layer_a_label": "west and east",
            "layer_b_label": "the same two",
            "output_filename": "sweep_compare",
        },
        offline=True,
    ),
    "generate_heatmap": ToolSample(
        args={
            "input_path": POINTS_LAYER,
            "place_name": SWEEP_PLACE,
            "value_column": "population",
            "bandwidth_km": 0.5,
            "output_filename": "sweep_heatmap",
        },
        offline=True,
    ),
    "export_to_gpkg": ToolSample(
        args={
            "dataset_path": POINTS_LAYER,
            "output_filename": "sweep_export.gpkg",
            "layer_name": "points",
        },
        offline=True,
    ),
    "list_tilesets": ToolSample(args={"category": "terrain"}),
    "ptolemy_query": ToolSample(args={"action": "list_datasets"}),
    "list_workflow_operations": ToolSample(),
    "plan_workflow": ToolSample(args={"manifest_toml": SWEEP_MANIFEST_TOML}),
    "run_workflow": ToolSample(
        args={"manifest_toml": SWEEP_MANIFEST_TOML},
        after="plan_workflow",
        needs_approval=True,
    ),
    "geocode_place": ToolSample(args={"place_name": SWEEP_PLACE}, external=True),
    "batch_geocode": ToolSample(
        args={
            "addresses": "Monaco;Monte Carlo",
            "output_filename": "sweep_geocoded",
        },
        external=True,
    ),
    "compute_route": ToolSample(
        args={
            "origin": "Monaco",
            "destination": "Monte Carlo",
            "travel_mode": "driving",
            "output_filename": "sweep_route",
        },
        external=True,
    ),
    "calculate_isochrones": ToolSample(
        args={
            "place_name": SWEEP_PLACE,
            "travel_mode": "walking",
            "time_minutes": "5",
            "output_filename": "sweep_isochrones",
        },
        external=True,
    ),
    "get_admin_boundary": ToolSample(
        args={"place_name": SWEEP_PLACE, "output_filename": "sweep_boundary"},
        external=True,
    ),
    "download_natural_earth_dataset": ToolSample(
        args={"scale": "110m", "dataset": "populated_places"}, external=True
    ),
    "download_osm_data": ToolSample(
        args={
            "place_name": SWEEP_PLACE,
            "data_type": "pharmacies",
            "output_filename": "sweep_osm",
        },
        external=True,
    ),
    "download_population_grid": ToolSample(
        args={"place_name": SWEEP_PLACE, "radius_km": 1.0}, external=True
    ),
    "query_elevation": ToolSample(args={"place_name": SWEEP_PLACE}, external=True),
    "terrain_profile": ToolSample(
        args={
            "start_place": "Monaco",
            "end_place": "Monte Carlo",
            "n_samples": 10,
            "output_filename": "sweep_profile",
        },
        external=True,
    ),
    "query_zonal_population": ToolSample(
        args={"polygon_path": POLYGONS_LAYER, "place_name": SWEEP_PLACE},
        external=True,
    ),
    "assess_environmental_risk": ToolSample(
        args={"place_name": SWEEP_PLACE, "radius_km": 0.5}, external=True
    ),
    "score_sites": ToolSample(
        args={
            "sites": "Monaco;Monte Carlo",
            "criteria": "population",
            "output_filename": "sweep_scores",
        },
        external=True,
    ),
    "service_gap": ToolSample(
        args={
            "place_name": SWEEP_PLACE,
            "service_path": POINTS_LAYER,
            "service_radius_km": 0.5,
            "grid_resolution_m": 500,
            "output_filename": "sweep_gaps",
        },
        external=True,
    ),
    "check_qgis_status": ToolSample(offline=True, needs_qgis=True),
    "list_qgis_algorithms": ToolSample(offline=True, needs_qgis=True),
    "run_qgis_algorithm": ToolSample(
        args={
            "algorithm_id": "native:buffer",
            "parameters": f'{{"INPUT": "{POINTS_LAYER}", "DISTANCE": 0.002}}',
            "output_filename": "sweep_qgis_buffer.gpkg",
        },
        offline=True,
        needs_qgis=True,
    ),
    "pyqgis_api": ToolSample(
        args={"function_name": "QgsVectorLayer", "uri": POINTS_LAYER},
        offline=True,
        needs_qgis=True,
    ),
}
