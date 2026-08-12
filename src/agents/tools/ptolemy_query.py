"""
Ptolemy geodatabase tool.

Reads datasets, branches, and features from the platform's Ptolemy service
(versioned PostGIS geodatabase) and saves feature results as GPKG in outputs/
so downstream tools and emit_ui_spec can use them.
"""
from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import caller_outputs_dir


class PtolemyQueryArgs(BaseModel):
    action: str = Field(
        ...,
        description=(
            "One of: 'list_datasets' (all datasets in the geodatabase), "
            "'list_branches' (branches of one dataset, requires dataset_id), "
            "'export' (save a branch's features as GPKG, requires branch_id), "
            "'query_bbox' (features in a bounding box, requires branch_id and "
            "min_lon, min_lat, max_lon, max_lat)."
        ),
    )
    dataset_id: Optional[str] = Field(None, description="Dataset UUID for list_branches.")
    branch_id: Optional[str] = Field(None, description="Branch UUID for export and query_bbox.")
    min_lon: Optional[float] = Field(None, description="Bounding box west edge.")
    min_lat: Optional[float] = Field(None, description="Bounding box south edge.")
    max_lon: Optional[float] = Field(None, description="Bounding box east edge.")
    max_lat: Optional[float] = Field(None, description="Bounding box north edge.")
    limit: int = Field(1000, description="Maximum number of features to fetch.")
    output_filename: Optional[str] = Field(
        None, description="Output filename without extension. Auto-generated if omitted."
    )


def ptolemy_query(
    action: str,
    dataset_id: str = None,
    branch_id: str = None,
    min_lon: float = None,
    min_lat: float = None,
    max_lon: float = None,
    max_lat: float = None,
    limit: int = 1000,
    output_filename: str = None,
) -> str:
    """
    Query the Ptolemy geodatabase: list its datasets and branches, export a
    branch's features to GPKG, or fetch features in a bounding box.
    Use this when the user refers to data stored in the platform geodatabase
    (shared/versioned layers), not for ad-hoc downloads (use download_osm_data
    or download_natural_earth_dataset for those).
    """
    import os
    import traceback

    base_url = os.environ.get("PTOLEMY_URL", "http://ptolemy:3000").rstrip("/")
    api = f"{base_url}/api/v1"
    outputs_dir = caller_outputs_dir()

    from src.core.user_token import service_headers

    # the person who asked wins; PTOLEMY_API_TOKEN is the service account a
    # headless run falls back on
    headers = service_headers("PTOLEMY_API_TOKEN")

    try:
        import requests

        if action == "list_datasets":
            resp = requests.get(f"{api}/datasets", headers=headers, timeout=15)
            resp.raise_for_status()
            datasets = resp.json()
            if not datasets:
                return "No datasets in the Ptolemy geodatabase yet."
            lines = ["Ptolemy datasets:"]
            for d in datasets:
                lines.append(
                    f"  • {d.get('name')} (id={d.get('id')}, "
                    f"type={d.get('geometry_type')}, srid={d.get('srid')})"
                )
            lines.append("Use list_branches with a dataset_id to see its branches.")
            return "\n".join(lines)

        if action == "list_branches":
            if not dataset_id:
                return "ERROR: list_branches requires dataset_id."
            resp = requests.get(
                f"{api}/datasets/{dataset_id}/branches", headers=headers, timeout=15
            )
            resp.raise_for_status()
            branches = resp.json()
            if not branches:
                return f"Dataset {dataset_id} has no branches."
            lines = [f"Branches of dataset {dataset_id}:"]
            for b in branches:
                lines.append(f"  • {b.get('name')} (id={b.get('id')})")
            lines.append("Use export or query_bbox with a branch_id to fetch features.")
            return "\n".join(lines)

        if action == "export":
            if not branch_id:
                return "ERROR: export requires branch_id."
            import geopandas as gpd

            resp = requests.get(
                f"{api}/branches/{branch_id}/export/geojson",
                params={"limit": limit},
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            fc = resp.json()
            features = fc.get("features", [])
            if not features:
                return f"Branch {branch_id} has no features."
            gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
            name = output_filename or f"ptolemy_{branch_id[:8]}"
            if name.lower().endswith(".gpkg"):
                name = name[:-5]
            out_path = os.path.join(outputs_dir, f"{name}.gpkg")
            gdf.to_file(out_path, driver="GPKG")
            bounds = gdf.total_bounds
            return (
                f"Exported {len(gdf)} features from Ptolemy branch {branch_id} "
                f"to outputs/{name}.gpkg. "
                f"Center: lon={(bounds[0] + bounds[2]) / 2:.4f}, "
                f"lat={(bounds[1] + bounds[3]) / 2:.4f}"
            )

        if action == "query_bbox":
            if not branch_id:
                return "ERROR: query_bbox requires branch_id."
            if None in (min_lon, min_lat, max_lon, max_lat):
                return "ERROR: query_bbox requires min_lon, min_lat, max_lon, max_lat."
            import geopandas as gpd
            from shapely import wkb as shapely_wkb

            resp = requests.get(
                f"{api}/branches/{branch_id}/features/bbox",
                params={
                    "min_x": min_lon,
                    "min_y": min_lat,
                    "max_x": max_lon,
                    "max_y": max_lat,
                    "limit": limit,
                },
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            feats = resp.json()
            if not feats:
                return "No features found in that bounding box."
            records = []
            for f in feats:
                props = dict(f.get("properties") or {})
                props["ptolemy_id"] = f.get("id")
                props["geometry"] = shapely_wkb.loads(bytes(f["geometry_wkb"]))
                records.append(props)
            gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
            name = output_filename or f"ptolemy_bbox_{branch_id[:8]}"
            if name.lower().endswith(".gpkg"):
                name = name[:-5]
            out_path = os.path.join(outputs_dir, f"{name}.gpkg")
            gdf.to_file(out_path, driver="GPKG")
            return (
                f"Fetched {len(gdf)} features from Ptolemy branch {branch_id} "
                f"in bbox ({min_lon},{min_lat},{max_lon},{max_lat}), "
                f"saved to outputs/{name}.gpkg."
            )

        return (
            f"ERROR: Unknown action '{action}'. "
            "Use list_datasets, list_branches, export, or query_bbox."
        )

    except Exception as e:
        return f"Ptolemy query failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = ptolemy_query
TOOL_SCHEMA = PtolemyQueryArgs
