from pydantic import BaseModel, Field
from typing import Optional


class FindNearestArgs(BaseModel):
    origins_path: str = Field(
        ...,
        description=(
            "Path to the layer whose features you want to find neighbours FOR — "
            "e.g. your sites, hospitals, or user-uploaded points. "
            "Relative to outputs/ or user_data/ or absolute."
        ),
    )
    targets_path: str = Field(
        ...,
        description=(
            "Path to the layer to search for nearest features IN — "
            "e.g. bus stops, schools, restaurants. "
            "Relative to outputs/ or user_data/ or absolute."
        ),
    )
    k: int = Field(
        1,
        description="Number of nearest targets to find per origin feature (default 1, max 10).",
    )
    max_distance_km: Optional[float] = Field(
        None,
        description=(
            "Optional maximum search radius in km. Origins with no target within this "
            "distance are excluded from results (or flagged if keep_unmatched=True)."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def find_nearest(
    origins_path: str,
    targets_path: str,
    k: int = 1,
    max_distance_km: float = None,
    output_filename: str = None,
) -> str:
    """
    For each feature in the origins layer, find the k nearest features in the
    targets layer and report the distance. Adds distance_m and target attributes
    to the origins and saves as a GPKG.

    Common uses:
    - 'Find the nearest hospital to each of my sites'
    - 'How far is each school from the nearest bus stop?'
    - 'Find the 3 nearest restaurants to each hotel'
    - 'Which GP surgery is closest to each care home?'

    Distances are straight-line (Euclidean in metres after projection).
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    user_data_dir = os.path.join(exec_dir, "user_data")
    os.makedirs(outputs_dir, exist_ok=True)

    def _res(p):
        return (
            None
            if not p
            else (
                p
                if os.path.isabs(p) and os.path.exists(p)
                else next(
                    (
                        c
                        for _b in (outputs_dir, user_data_dir, exec_dir)
                        for _n in (
                            [p] + ([] if p.lower().endswith(".gpkg") else [p + ".gpkg"])
                        )
                        for c in [os.path.join(_b, _n)]
                        if os.path.exists(c)
                    ),
                    None,
                )
            )
        )

    try:
        import geopandas as gpd

        orig_full = _res(origins_path)
        if not orig_full:
            return f"Origins file not found: '{origins_path}'."

        targ_full = _res(targets_path)
        if not targ_full:
            return f"Targets file not found: '{targets_path}'."

        gdf_orig = gpd.read_file(orig_full)
        gdf_targ = gpd.read_file(targ_full)

        if gdf_orig.empty:
            return f"Origins layer is empty: {origins_path}"
        if gdf_targ.empty:
            return f"Targets layer is empty: {targets_path}"

        if gdf_orig.crs is None:
            gdf_orig = gdf_orig.set_crs("EPSG:4326")
        if gdf_targ.crs is None:
            gdf_targ = gdf_targ.set_crs("EPSG:4326")

        # Project both to a metric CRS for accurate distance measurement
        # Use UTM zone based on centroid of origins
        centroid = gdf_orig.to_crs("EPSG:4326").geometry.unary_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        hemisphere = "north" if centroid.y >= 0 else "south"
        utm_epsg = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone

        gdf_orig_m = gdf_orig.to_crs(f"EPSG:{utm_epsg}")
        gdf_targ_m = gdf_targ.to_crs(f"EPSG:{utm_epsg}")

        # Use centroids for non-point geometries
        if not gdf_orig_m.geometry.geom_type.str.contains("Point").all():
            gdf_orig_m = gdf_orig_m.copy()
            gdf_orig_m["geometry"] = gdf_orig_m.geometry.centroid
        if not gdf_targ_m.geometry.geom_type.str.contains("Point").all():
            gdf_targ_m = gdf_targ_m.copy()
            gdf_targ_m["geometry"] = gdf_targ_m.geometry.centroid

        k = max(1, min(k, 10))
        max_dist_m = max_distance_km * 1000 if max_distance_km else None

        # Prefix target columns to avoid collisions
        targ_rename = {c: f"nearest_{c}" for c in gdf_targ_m.columns if c != "geometry"}
        gdf_targ_m = gdf_targ_m.rename(columns=targ_rename)

        joined = gpd.sjoin_nearest(
            gdf_orig_m,
            gdf_targ_m,
            how="left",
            max_distance=max_dist_m,
            distance_col="distance_m",
            k=k,
        )

        # Drop sjoin index columns
        joined = joined.drop(
            columns=[c for c in joined.columns if c.startswith("index_")],
            errors="ignore",
        )

        # Round distance
        if "distance_m" in joined.columns:
            joined["distance_m"] = joined["distance_m"].round(1)
            joined["distance_km"] = (joined["distance_m"] / 1000).round(3)

        # Restore WGS84 geometry from original (not centroid)
        joined = joined.set_geometry(
            gdf_orig.to_crs(f"EPSG:{utm_epsg}").geometry.values
        ).to_crs("EPSG:4326")

        # Sanitise column names
        import re

        joined.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in joined.columns
        ]

        if not output_filename:
            orig_stem = os.path.splitext(os.path.basename(orig_full))[0][:14]
            targ_stem = os.path.splitext(os.path.basename(targ_full))[0][:14]
            output_filename = f"{orig_stem}_near_{targ_stem}"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        joined.to_file(output_path, driver="GPKG")

        # Summary stats
        valid = joined["distance_m"].dropna() if "distance_m" in joined.columns else []
        if len(valid):
            avg_km = round(float(valid.mean()) / 1000, 2)
            max_km = round(float(valid.max()) / 1000, 2)
            min_km = round(float(valid.min()) / 1000, 2)
            dist_summary = (
                f"Distances — min {min_km}km, avg {avg_km}km, max {max_km}km."
            )
        else:
            dist_summary = "No matches found within the search radius."

        unmatched = (
            int(joined["distance_m"].isna().sum())
            if "distance_m" in joined.columns
            else 0
        )
        unmatched_note = (
            f" {unmatched} origins had no target within {max_distance_km}km."
            if unmatched
            else ""
        )

        return (
            f"Found nearest {os.path.basename(targ_full).replace('.gpkg','')} for "
            f"{len(gdf_orig)} origins (k={k}). {dist_summary}{unmatched_note} "
            f"Saved to outputs/{output_filename}.gpkg."
        )

    except Exception as e:
        return f"find_nearest failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = find_nearest
TOOL_SCHEMA = FindNearestArgs
