from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import caller_outputs_dir


class VoronoiArgs(BaseModel):
    input_path: str = Field(
        ...,
        description=(
            "Path to the point layer to generate Voronoi polygons from — "
            "e.g. hospital locations, depot sites, store locations. "
            "Relative to outputs/ or user_data/ or absolute."
        ),
    )
    boundary_path: Optional[str] = Field(
        None,
        description=(
            "Optional polygon layer to clip the Voronoi diagram to — "
            "e.g. a city boundary, region, or isochrone. "
            "Relative to outputs/ or absolute. "
            "If omitted, a convex hull of the input points is used."
        ),
    )
    label_col: Optional[str] = Field(
        None,
        description=(
            "Column from input_path to use as the label for each Voronoi cell "
            "(e.g. 'name', 'hospital_name'). Auto-detected if omitted."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def voronoi(
    input_path: str,
    boundary_path: str = None,
    label_col: str = None,
    output_filename: str = None,
) -> str:
    """
    Generate Voronoi (Thiessen) polygons from a point layer. Each polygon
    defines the area closest to one input point — ideal for defining service
    catchment zones, trade areas, or nearest-facility boundaries.

    Common uses:
    - 'Draw catchment zones for each hospital'
    - 'Which areas are closest to each of my stores?'
    - 'Create Voronoi regions for weather stations'
    - 'Define trade areas for each retail location'
    - 'Show nearest-depot zones for logistics'

    Returns a polygon GPKG with one cell per input point, labelled with
    the point's name/attributes. Call emit_ui_spec after with ui_type='map'.
    """
    import os
    import traceback
    import re

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = caller_outputs_dir()
    user_data_dir = os.path.join(exec_dir, "user_data")

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
        import numpy as np
        from shapely.geometry import box

        full_path = _res(input_path)
        if not full_path:
            return f"Input file not found: '{input_path}'."

        gdf = gpd.read_file(full_path)
        if gdf.empty:
            return f"Layer is empty: {input_path}"
        if len(gdf) < 3:
            return f"Need at least 3 points for Voronoi tessellation (got {len(gdf)})."

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        gdf = gdf.to_crs("EPSG:4326")

        # Use centroids for non-point geometries
        if not gdf.geometry.geom_type.str.contains("Point").all():
            gdf = gdf.copy()
            gdf["geometry"] = gdf.geometry.centroid

        # Auto-detect label column
        if not label_col:
            for candidate in ("name", "NAME", "Name", "label", "title", "id"):
                if candidate in gdf.columns:
                    label_col = candidate
                    break
        if not label_col:
            str_cols = gdf.select_dtypes(include="object").columns.tolist()
            label_col = str_cols[0] if str_cols else None

        # Project to metric CRS
        centroid = gdf.geometry.unary_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
        metric_crs = f"EPSG:{utm_epsg}"

        gdf_m = gdf.to_crs(metric_crs)

        # Load or build clip boundary
        if boundary_path:
            bnd_full = _res(boundary_path)
            if not bnd_full:
                return f"Boundary file not found: '{boundary_path}'."
            bnd_gdf = gpd.read_file(bnd_full).to_crs(metric_crs)
            clip_poly = bnd_gdf.union_all()
        else:
            # Convex hull + 10% padding
            hull = gdf_m.geometry.unary_union.convex_hull
            minx, miny, maxx, maxy = hull.bounds
            pad_x = (maxx - minx) * 0.1 or 1000
            pad_y = (maxy - miny) * 0.1 or 1000
            clip_poly = box(minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)

        # Voronoi tessellation using scipy
        from scipy.spatial import Voronoi as ScipyVoronoi

        coords = np.array([(g.x, g.y) for g in gdf_m.geometry])

        # Add far-away mirror points to ensure finite regions for edge points
        cx, cy = coords[:, 0].mean(), coords[:, 1].mean()
        radius = (
            max(
                clip_poly.bounds[2] - clip_poly.bounds[0],
                clip_poly.bounds[3] - clip_poly.bounds[1],
            )
            * 3
        )
        mirrors = np.array(
            [
                [cx + radius, cy],
                [cx - radius, cy],
                [cx, cy + radius],
                [cx, cy - radius],
            ]
        )
        all_coords = np.vstack([coords, mirrors])

        vor = ScipyVoronoi(all_coords)

        # Build one polygon per original (non-mirror) point
        from shapely.geometry import Polygon as ShapelyPolygon

        n_orig = len(coords)
        polygons = []
        for i in range(n_orig):
            region_idx = vor.point_region[i]
            region = vor.regions[region_idx]
            if not region or -1 in region:
                # Infinite region — use clip_poly as fallback
                polygons.append(clip_poly)
                continue
            verts = vor.vertices[region]
            poly = ShapelyPolygon(verts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            clipped = poly.intersection(clip_poly)
            polygons.append(clipped if not clipped.is_empty else clip_poly)

        # Build output GDF in metric CRS, then reproject to WGS84
        out_rows = []
        for i, poly in enumerate(polygons):
            row = {"geometry": poly}
            if label_col and label_col in gdf.columns:
                row["name"] = gdf.iloc[i][label_col]
            # Copy all non-geometry columns
            for col in gdf.columns:
                if col != "geometry" and col != label_col:
                    row[col] = gdf.iloc[i][col]
            out_rows.append(row)

        out_gdf = gpd.GeoDataFrame(out_rows, crs=metric_crs).to_crs("EPSG:4326")

        # Sanitise column names
        out_gdf.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in out_gdf.columns
        ]

        stem = os.path.splitext(os.path.basename(full_path))[0][:20]
        if not output_filename:
            output_filename = f"{stem}_voronoi"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        out_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        out_gdf.to_file(out_path, driver="GPKG")

        bnd_note = (
            f" Clipped to '{os.path.basename(boundary_path)}'."
            if boundary_path
            else " Clipped to convex hull of points."
        )
        lbl_note = f" Labelled by '{label_col}'." if label_col else ""

        return (
            f"Voronoi tessellation complete: {len(out_gdf)} cells from {len(gdf)} input points.{bnd_note}{lbl_note} "
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Each polygon shows the area closest to one input point."
        )

    except ImportError:
        return "voronoi requires scipy. Install with: pip install scipy"
    except Exception as e:
        return f"voronoi failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = voronoi
TOOL_SCHEMA = VoronoiArgs
