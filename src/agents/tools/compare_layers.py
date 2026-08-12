from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class CompareLayersArgs(BaseModel):
    layer_a_path: str = Field(
        ...,
        description=(
            "Path to the first polygon layer (e.g. old isochrone, flood zone, district boundary). "
            "A filename in outputs/ or user_data/, not a path."
        ),
    )
    layer_b_path: str = Field(
        ...,
        description=(
            "Path to the second polygon layer to compare against. "
            "A filename in outputs/ or user_data/, not a path."
        ),
    )
    layer_a_label: str = Field(
        "Layer A",
        description="Human-readable label for layer A (e.g. '15-min walk', 'Flood Zone', '2020 boundary').",
    )
    layer_b_label: str = Field(
        "Layer B",
        description="Human-readable label for layer B (e.g. '30-min walk', 'Industrial Zone', '2024 boundary').",
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename prefix without extension. Auto-generated if omitted.",
    )


def compare_layers(
    layer_a_path: str,
    layer_b_path: str,
    layer_a_label: str = "Layer A",
    layer_b_label: str = "Layer B",
    output_filename: str = None,
) -> str:
    """
    Spatially compare two polygon layers and compute:
    - Intersection: area covered by BOTH layers
    - Only in A: area in A but not B
    - Only in B: area in B but not A
    - Union: total combined area
    - Overlap percentage between the two layers

    Saves three GPKGs: intersection, only-in-A, only-in-B — ready to display
    as a three-layer map showing agreement and differences.

    Common uses:
    - 'Compare the 15-minute and 30-minute isochrones'
    - 'What area is in the flood zone but not the industrial zone?'
    - 'How much do these two catchment areas overlap?'
    - 'Show what changed between the 2020 and 2024 boundaries'
    - 'Which parts of district A overlap with district B?'

    Call emit_ui_spec after with all three output layers.
    """
    import traceback
    import re

    try:
        import geopandas as gpd
        from shapely.ops import unary_union

        a_full = tool_input_path("layer_a_path", layer_a_path)
        b_full = tool_input_path("layer_b_path", layer_b_path)

        gdf_a = gpd.read_file(a_full)
        gdf_b = gpd.read_file(b_full)

        if gdf_a.empty:
            return f"Layer A is empty: {layer_a_path}"
        if gdf_b.empty:
            return f"Layer B is empty: {layer_b_path}"

        # Ensure WGS84
        if gdf_a.crs is None:
            gdf_a = gdf_a.set_crs("EPSG:4326")
        else:
            gdf_a = gdf_a.to_crs("EPSG:4326")
        if gdf_b.crs is None:
            gdf_b = gdf_b.set_crs("EPSG:4326")
        else:
            gdf_b = gdf_b.to_crs("EPSG:4326")

        # Dissolve each to a single geometry for clean set operations
        poly_a = unary_union(gdf_a.geometry)
        poly_b = unary_union(gdf_b.geometry)

        intersection = poly_a.intersection(poly_b)
        only_a = poly_a.difference(poly_b)
        only_b = poly_b.difference(poly_a)
        union = poly_a.union(poly_b)

        # Project to metric CRS for area calculation
        centroid = union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
        metric_crs = f"EPSG:{utm_epsg}"

        def _area_km2(geom):
            return (
                0.0
                if geom.is_empty
                else round(
                    float(
                        gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
                        .to_crs(metric_crs)
                        .geometry.area.sum()
                    )
                    / 1e6,
                    2,
                )
            )

        area_a = _area_km2(poly_a)
        area_b = _area_km2(poly_b)
        area_inter = _area_km2(intersection)
        area_only_a = _area_km2(only_a)
        area_only_b = _area_km2(only_b)
        area_union = _area_km2(union)

        overlap_pct = round(area_inter / area_union * 100, 1) if area_union > 0 else 0.0
        jaccard = round(area_inter / area_union, 3) if area_union > 0 else 0.0

        # Output filename prefix
        if not output_filename:
            _safe_a = re.sub(r"[^\w]", "_", layer_a_label.lower())[:12].strip("_")
            _safe_b = re.sub(r"[^\w]", "_", layer_b_label.lower())[:12].strip("_")
            output_filename = f"compare_{_safe_a}_{_safe_b}"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        saved = []

        if not intersection.is_empty:
            _gdf_inter = gpd.GeoDataFrame(
                [
                    {
                        "label": f"Intersection ({layer_a_label} ∩ {layer_b_label})",
                        "area_km2": _area_km2(intersection),
                        "geometry": intersection,
                    }
                ],
                crs="EPSG:4326",
            )
            _fname_inter = f"{output_filename}_intersection"
            _gdf_inter.to_file(
                tool_output_path("output_filename", f"{_fname_inter}.gpkg"),
                driver="GPKG",
            )
            saved.append(f"outputs/{_fname_inter}.gpkg")

        if not only_a.is_empty:
            _gdf_only_a = gpd.GeoDataFrame(
                [
                    {
                        "label": f"Only in {layer_a_label}",
                        "area_km2": _area_km2(only_a),
                        "geometry": only_a,
                    }
                ],
                crs="EPSG:4326",
            )
            _fname_only_a = f"{output_filename}_only_a"
            _gdf_only_a.to_file(
                tool_output_path("output_filename", f"{_fname_only_a}.gpkg"),
                driver="GPKG",
            )
            saved.append(f"outputs/{_fname_only_a}.gpkg")

        if not only_b.is_empty:
            _gdf_only_b = gpd.GeoDataFrame(
                [
                    {
                        "label": f"Only in {layer_b_label}",
                        "area_km2": _area_km2(only_b),
                        "geometry": only_b,
                    }
                ],
                crs="EPSG:4326",
            )
            _fname_only_b = f"{output_filename}_only_b"
            _gdf_only_b.to_file(
                tool_output_path("output_filename", f"{_fname_only_b}.gpkg"),
                driver="GPKG",
            )
            saved.append(f"outputs/{_fname_only_b}.gpkg")

        parts = [
            f"Spatial comparison: '{layer_a_label}' vs '{layer_b_label}'",
            "",
            f"{layer_a_label} area:   {area_a} km²",
            f"{layer_b_label} area:   {area_b} km²",
            f"Intersection:         {area_inter} km² ({overlap_pct}% of union)",
            f"Only in {layer_a_label}: {area_only_a} km²",
            f"Only in {layer_b_label}: {area_only_b} km²",
            f"Union (total):        {area_union} km²",
            f"Jaccard similarity:   {jaccard} (0=no overlap, 1=identical)",
            "",
        ]
        parts.append("Saved layers: " + ", ".join(saved))
        return "\n".join(parts)

    except Exception as e:
        return f"compare_layers failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = compare_layers
TOOL_SCHEMA = CompareLayersArgs
