from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class SpatialJoinArgs(BaseModel):
    points_path: str = Field(
        ...,
        description=(
            "Layer whose features are tagged or filtered, usually points but any "
            "geometry works. A filename in outputs/, not a path."
        ),
    )
    polygons_path: str = Field(
        ...,
        description=(
            "Polygon layer joined against, e.g. an isochrone or admin boundary. "
            "A filename in outputs/, not a path."
        ),
    )
    how: str = Field(
        "inner",
        description=(
            "'inner' keeps only points inside a polygon; 'left' keeps every point "
            "and adds the polygon attributes, NaN where nothing matched."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def spatial_join(
    points_path: str,
    polygons_path: str,
    how: str = "inner",
    output_filename: str = None,
) -> str:
    """
    Join two layers by location: attach a polygon's attributes to the points
    inside it, or filter points to those inside a boundary. Answers questions
    like which schools fall inside the flood zone.
    """
    import os
    import traceback

    try:
        import geopandas as gpd

        pts_full = tool_input_path("points_path", points_path)
        poly_full = tool_input_path("polygons_path", polygons_path)

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

        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
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
