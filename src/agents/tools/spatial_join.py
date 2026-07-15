from pydantic import BaseModel, Field
from typing import Optional


class SpatialJoinArgs(BaseModel):
    points_path: str = Field(
        ...,
        description=(
            "Path to the layer whose features you want to tag/filter — typically points "
            "but can be any geometry. Relative to outputs/ or absolute. "
            "E.g. 'restaurants.gpkg', 'outputs/schools.gpkg'."
        ),
    )
    polygons_path: str = Field(
        ...,
        description=(
            "Path to the polygon layer to join against — e.g. an isochrone, "
            "flood zone, or admin boundary. Relative to outputs/ or absolute."
        ),
    )
    how: str = Field(
        "inner",
        description=(
            "Join type: 'inner' keeps only points that fall inside a polygon (default); "
            "'left' keeps ALL points and adds polygon attributes (NaN if no match)."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def spatial_join(
    points_path: str,
    polygons_path: str,
    how: str = "inner",
    output_filename: str = None,
) -> str:
    """
    Spatially join two layers — attach polygon attributes to point features that fall
    inside the polygon, or filter points to only those inside a boundary.

    Common uses:
    - 'Which schools fall inside the flood zone?'
    - 'Which restaurants are within the 15-min isochrone?'
    - 'Tag each hospital with the district it belongs to.'

    Use how='inner' to keep only matching features (default).
    Use how='left' to keep all features and add polygon attributes where they match.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    user_data_dir = os.path.join(exec_dir, "user_data")
    os.makedirs(outputs_dir, exist_ok=True)

    _res = lambda p: (
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

        pts_full = _res(points_path)
        if not pts_full:
            return f"Points/features file not found: '{points_path}'. Check the filename in outputs/ or user_data/."

        poly_full = _res(polygons_path)
        if not poly_full:
            return f"Polygons file not found: '{polygons_path}'. Check the filename in outputs/."

        gdf_pts = gpd.read_file(pts_full)
        gdf_poly = gpd.read_file(poly_full)

        if gdf_pts.empty:
            return f"Features layer is empty: {points_path}"
        if gdf_poly.empty:
            return f"Polygons layer is empty: {polygons_path}"

        # Align CRS — reproject polygons to match points
        if gdf_pts.crs is None:
            gdf_pts = gdf_pts.set_crs("EPSG:4326")
        if gdf_poly.crs is None:
            gdf_poly = gdf_poly.set_crs("EPSG:4326")
        if gdf_poly.crs != gdf_pts.crs:
            gdf_poly = gdf_poly.to_crs(gdf_pts.crs)

        how = how.lower().strip()
        if how not in ("inner", "left"):
            how = "inner"

        joined = gpd.sjoin(gdf_pts, gdf_poly, how=how, predicate="within")

        # Drop the index_right column added by sjoin
        joined = joined.drop(
            columns=[c for c in joined.columns if c.startswith("index_")],
            errors="ignore",
        )

        if joined.empty:
            return (
                f"No features from '{points_path}' fall within any polygon in '{polygons_path}'. "
                f"Check that both layers overlap geographically."
            )

        # Auto-generate output filename
        if not output_filename:
            pts_stem = os.path.splitext(os.path.basename(pts_full))[0][:12]
            poly_stem = os.path.splitext(os.path.basename(poly_full))[0][:12]
            output_filename = f"{pts_stem}_in_{poly_stem}"

        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        # Sanitise column names for GPKG (max 60 chars, no special chars)
        import re

        joined.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in joined.columns
        ]

        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        joined.to_file(output_path, driver="GPKG")

        n_in = len(joined)
        n_total = len(gdf_pts)
        poly_cols = [c for c in gdf_poly.columns if c != "geometry"][:5]

        return (
            f"Spatial join complete: {n_in} of {n_total} features fall within the polygon(s). "
            f"Polygon attributes added: {', '.join(poly_cols) if poly_cols else 'none'}. "
            f"Saved to outputs/{output_filename}.gpkg."
        )

    except Exception as e:
        return f"Spatial join failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = spatial_join
TOOL_SCHEMA = SpatialJoinArgs
