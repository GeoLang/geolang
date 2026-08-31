from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import (
    population_raster_path,
    tool_input_path,
    tool_input_path_or_none,
    tool_output_path,
)


class ServiceGapArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Place to analyse, e.g. 'Leeds city centre'. The study area unless "
            "boundary_path is given."
        ),
    )
    service_path: str = Field(
        ...,
        description=(
            "Service facility layer (points or polygons), a filename in outputs/ or "
            "user_data/, not a path. Also accepts an OSM keyword like 'hospitals' or "
            "'schools' to download it instead."
        ),
    )
    service_radius_km: float = Field(
        1.0,
        description="Catchment radius in km: a cell is served within this distance of a facility.",
    )
    boundary_path: Optional[str] = Field(
        None,
        description=(
            "Boundary polygon (GPKG) to clip to, a filename in outputs/, not a path. "
            "Defaults to a bounding box around place_name."
        ),
    )
    grid_resolution_m: int = Field(
        500,
        description="Grid cell size in metres, 250 to 1000. Smaller is finer and slower.",
    )
    population_weight: bool = Field(
        False,
        description=(
            "Weight gap cells by population from ghsl_pop.tif in user_data/ or the "
            "project root. Needs that file."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def service_gap(
    place_name: str,
    service_path: str,
    service_radius_km: float = 1.0,
    boundary_path: str = None,
    grid_resolution_m: int = 500,
    population_weight: bool = False,
    output_filename: str = None,
) -> str:
    """
    Find areas under-served by a facility type (hospitals, schools, parks).

    Grids the study area and classifies each cell: SERVED within
    service_radius_km of a facility, UNDERSERVED 1 to 3x the radius, GAP
    beyond 3x. Returns a polygon GPKG, one row per cell.
    Call emit_ui_spec after with ui_type='map'.
    """
    import traceback

    try:
        import geopandas as gpd
        import numpy as np
        import re
        from shapely.geometry import Point, box
        import osmnx as ox

        # ── Study area ──────────────────────────────────────────────────────────
        if boundary_path:
            bnd_full = tool_input_path("boundary_path", boundary_path)
            bnd_gdf = gpd.read_file(bnd_full).to_crs("EPSG:4326")
            study_poly = bnd_gdf.union_all()
            centroid = study_poly.centroid
            lat, lon = centroid.y, centroid.x
        else:
            # Geocode place to get a bounding area
            _coord_m = re.match(
                r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
            )
            if _coord_m:
                lat, lon = float(_coord_m.group(1)), float(_coord_m.group(2))
            else:
                lat, lon = ox.geocode(place_name)
            # 10km square around centroid as default study area
            center_gdf = gpd.GeoDataFrame(
                geometry=[Point(lon, lat)], crs="EPSG:4326"
            ).to_crs("EPSG:3857")
            buf = center_gdf.geometry.iloc[0].buffer(10000)
            study_poly = (
                gpd.GeoDataFrame(geometry=[buf], crs="EPSG:3857")
                .to_crs("EPSG:4326")
                .geometry.iloc[0]
            )

        # ── Service facilities ──────────────────────────────────────────────────
        OSM_SERVICE_TAGS = {
            "hospitals": {"amenity": "hospital"},
            "hospital": {"amenity": "hospital"},
            "schools": {"amenity": ["school", "college"]},
            "school": {"amenity": ["school", "college"]},
            "pharmacies": {"amenity": "pharmacy"},
            "pharmacy": {"amenity": "pharmacy"},
            "gp": {"amenity": "doctors"},
            "doctors": {"amenity": "doctors"},
            "parks": {"leisure": "park"},
            "park": {"leisure": "park"},
            "supermarkets": {"shop": "supermarket"},
            "supermarket": {"shop": "supermarket"},
            "libraries": {"amenity": "library"},
            "library": {"amenity": "library"},
            "fire_stations": {"amenity": "fire_station"},
            "police": {"amenity": "police"},
            "bus_stops": {"highway": "bus_stop"},
            "transit": {"public_transport": True},
            "nurseries": {"amenity": "kindergarten"},
            "clinics": {"amenity": ["clinic", "doctors", "dentist"]},
        }

        svc_full = tool_input_path_or_none("service_path", service_path)
        if svc_full:
            gdf_svc = gpd.read_file(svc_full)
        else:
            # Try OSM download
            key = service_path.lower().strip()
            tags = OSM_SERVICE_TAGS.get(key)
            if not tags:
                # Try key=value format
                if "=" in key:
                    k, v = key.split("=", 1)
                    tags = {k.strip(): v.strip()}
                else:
                    return (
                        f"Service file '{service_path}' not found and not a recognised OSM type. "
                        f"Known types: {', '.join(OSM_SERVICE_TAGS.keys())}. "
                        f"Or use key=value syntax like 'amenity=hospital'."
                    )
            try:
                gdf_svc = ox.features_from_point((lat, lon), tags=tags, dist=15000)
                if gdf_svc.empty:
                    return f"No '{service_path}' features found within 15km of {place_name}."
                # Keep only geometry + name
                keep = [c for c in ["name", "geometry"] if c in gdf_svc.columns]
                gdf_svc = gdf_svc[keep].reset_index(drop=True)
            except Exception as e:
                return f"Could not download OSM data for '{service_path}': {e}"

        if gdf_svc.empty:
            return f"Service layer is empty: {service_path}"

        if gdf_svc.crs is None:
            gdf_svc = gdf_svc.set_crs("EPSG:4326")
        gdf_svc = gdf_svc.to_crs("EPSG:4326")

        # ── Project to metric CRS ───────────────────────────────────────────────
        utm_zone = int((lon + 180) / 6) + 1
        utm_epsg = 32600 + utm_zone if lat >= 0 else 32700 + utm_zone
        metric_crs = f"EPSG:{utm_epsg}"

        study_gdf = gpd.GeoDataFrame(geometry=[study_poly], crs="EPSG:4326").to_crs(
            metric_crs
        )
        study_bounds = study_gdf.total_bounds  # minx, miny, maxx, maxy
        minx, miny, maxx, maxy = study_bounds

        # Use centroids for polygon services
        gdf_svc_m = gdf_svc.to_crs(metric_crs)
        if not gdf_svc_m.geometry.geom_type.str.contains("Point").all():
            gdf_svc_m = gdf_svc_m.copy()
            gdf_svc_m["geometry"] = gdf_svc_m.geometry.centroid

        svc_pts = np.array(
            [
                (geom.x, geom.y)
                for geom in gdf_svc_m.geometry
                if geom and not geom.is_empty
            ]
        )
        if len(svc_pts) == 0:
            return "Service layer has no valid geometries."

        # ── Build grid ──────────────────────────────────────────────────────────
        res = max(100, int(grid_resolution_m))
        xs = np.arange(minx + res / 2, maxx, res)
        ys = np.arange(miny + res / 2, maxy, res)

        if len(xs) * len(ys) > 50000:
            # Auto-coarsen if grid would be too large
            res = int(np.sqrt((maxx - minx) * (maxy - miny) / 50000))
            xs = np.arange(minx + res / 2, maxx, res)
            ys = np.arange(miny + res / 2, maxy, res)

        # Build vectorised distance calculation using broadcasting
        # For each grid cell centre, compute minimum distance to any facility
        gx, gy = np.meshgrid(xs, ys)
        gx_flat = gx.ravel()
        gy_flat = gy.ravel()

        # Distance in batches to avoid memory issues
        batch = 5000
        min_dists = np.zeros(len(gx_flat))
        for i in range(0, len(gx_flat), batch):
            gxb = gx_flat[i : i + batch]
            gyb = gy_flat[i : i + batch]
            # Shape: (n_cells, n_facilities)
            dx = gxb[:, None] - svc_pts[:, 0][None, :]
            dy = gyb[:, None] - svc_pts[:, 1][None, :]
            dists = np.sqrt(dx**2 + dy**2)
            min_dists[i : i + batch] = dists.min(axis=1)

        radius_m = service_radius_km * 1000
        served_thresh = radius_m
        underserved_thresh = radius_m * 3

        # ── Optional population weighting ───────────────────────────────────────
        pop_vals = None
        if population_weight:
            pop_tif = population_raster_path()
            if pop_tif:
                try:
                    import rasterio
                    from rasterio.transform import rowcol
                    from rasterio.warp import transform as rio_transform

                    with rasterio.open(pop_tif) as src:
                        # Transform grid centres from UTM to raster CRS
                        lons, lats = rio_transform(
                            metric_crs,
                            src.crs.to_string(),
                            gx_flat.tolist(),
                            gy_flat.tolist(),
                        )
                        rows, cols = rowcol(src.transform, lons, lats)
                        rows = np.clip(np.array(rows), 0, src.height - 1)
                        cols = np.clip(np.array(cols), 0, src.width - 1)
                        data = src.read(1)
                        nodata = src.nodata
                        pop_vals = data[rows, cols].astype(float)
                        if nodata is not None:
                            pop_vals[pop_vals == nodata] = 0
                        pop_vals = np.maximum(pop_vals, 0)
                except Exception:
                    pop_vals = None

        # ── Build grid cell polygons ─────────────────────────────────────────────
        study_union_m = study_gdf.union_all()
        half = res / 2

        rows_data = []
        for idx in range(len(gx_flat)):
            cx, cy = gx_flat[idx], gy_flat[idx]
            cell = box(cx - half, cy - half, cx + half, cy + half)
            # Clip to study area
            if not study_union_m.intersects(cell):
                continue
            cell_clipped = study_union_m.intersection(cell)
            if cell_clipped.is_empty:
                continue

            d = float(min_dists[idx])
            if d <= served_thresh:
                gap_class = "SERVED"
                gap_score = 0
            elif d <= underserved_thresh:
                gap_class = "UNDERSERVED"
                gap_score = 1
            else:
                gap_class = "GAP"
                gap_score = 2

            row = {
                "geometry": cell_clipped,
                "min_dist_m": round(d, 0),
                "gap_class": gap_class,
                "gap_score": gap_score,
            }
            if pop_vals is not None:
                row["population"] = round(float(pop_vals[idx]), 1)
            rows_data.append(row)

        if not rows_data:
            return "No grid cells intersected with the study area."

        out_gdf = gpd.GeoDataFrame(rows_data, crs=metric_crs).to_crs("EPSG:4326")

        # ── Output filename ─────────────────────────────────────────────────────
        if not output_filename:
            safe = re.sub(r"[^\w]", "_", place_name.lower())[:16].strip("_")
            svc_name = re.sub(r"[^\w]", "_", str(service_path).lower())[:12].strip("_")
            output_filename = f"{safe}_gap_{svc_name}"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
        out_gdf.to_file(output_path, driver="GPKG")

        # ── Summary ─────────────────────────────────────────────────────────────
        total_cells = len(out_gdf)
        n_served = int((out_gdf["gap_score"] == 0).sum())
        n_under = int((out_gdf["gap_score"] == 1).sum())
        n_gap = int((out_gdf["gap_score"] == 2).sum())
        pct_gap = round(n_gap / total_cells * 100, 1) if total_cells else 0
        pct_under = round(n_under / total_cells * 100, 1) if total_cells else 0

        pop_note = ""
        if pop_vals is not None and "population" in out_gdf.columns:
            pop_gap = int(out_gdf[out_gdf["gap_score"] == 2]["population"].sum())
            pop_under = int(out_gdf[out_gdf["gap_score"] == 1]["population"].sum())
            pop_note = f" Estimated population in gaps: {pop_gap:,}, underserved: {pop_under:,}."

        return (
            f"Service gap analysis for '{place_name}' — {service_path} within {service_radius_km}km. "
            f"{len(svc_pts)} facilities found. "
            f"Grid: {total_cells} cells at {res}m resolution. "
            f"Served: {n_served} ({round(n_served/total_cells*100,1) if total_cells else 0}%), "
            f"Underserved: {n_under} ({pct_under}%), "
            f"Gap: {n_gap} ({pct_gap}%).{pop_note} "
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Each cell carries 'gap_score' and 'gap_class' (0=served, 1=underserved, 2=gap). "
            f"Shade it by passing 'gap_score' as the fourth part of the emit_ui_spec layer "
            f"entry: 'Service gaps|outputs/{output_filename}.gpkg|#ff6b35|gap_score'."
        )

    except Exception as e:
        return f"service_gap failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = service_gap
TOOL_SCHEMA = ServiceGapArgs
