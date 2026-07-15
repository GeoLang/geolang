from pydantic import BaseModel, Field
from typing import Optional


class AssessEnvironmentalRiskArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Place or address to assess. "
            "E.g. 'Canary Wharf, London' or 'Leicester city centre'."
        ),
    )
    radius_km: float = Field(
        2.0,
        description="Radius in km around the location to analyse (default 2km).",
    )
    polygon_path: Optional[str] = Field(
        None,
        description=(
            "Optional path to an existing polygon GPKG (e.g. an isochrone) to use "
            "instead of a circular buffer. Relative to outputs/ or absolute."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def assess_environmental_risk(
    place_name: str,
    radius_km: float = 2.0,
    polygon_path: str = None,
    output_filename: str = None,
) -> str:
    """
    Assess environmental risk factors for a location or area. Checks:
    1. Elevation & flood risk (OpenTopoData SRTM 90m grid)
    2. Proximity to water bodies (rivers, coastline from OSM)
    3. Green space / tree cover percentage (OSM landuse)
    4. Industrial site proximity (OSM industrial landuse)
    5. Major road proximity (noise/air pollution proxy)

    Returns a risk summary with scores and saves a GPKG with all layers.
    Use this when the user asks about flood risk, environmental suitability,
    pollution, or green space for a location.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import Point, box

        # Geocode — if place_name looks like "lat,lon" or "lat lon", parse directly
        import re as _re

        _coord_m = _re.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
        )
        if _coord_m:
            lat, lon = float(_coord_m.group(1)), float(_coord_m.group(2))
        else:
            lat, lon = ox.geocode(place_name)
        center = Point(lon, lat)

        # Build analysis area
        if polygon_path:
            # Resolve polygon path
            poly_file = None
            if os.path.isabs(polygon_path) and os.path.exists(polygon_path):
                poly_file = polygon_path
            else:
                for base in (outputs_dir, exec_dir):
                    candidate = os.path.join(base, polygon_path)
                    if os.path.exists(candidate):
                        poly_file = candidate
                        break
            if not poly_file:
                return f"Polygon file not found: '{polygon_path}'."
            area_gdf = gpd.read_file(poly_file)
            if area_gdf.crs and area_gdf.crs.to_epsg() != 4326:
                area_gdf = area_gdf.to_crs("EPSG:4326")
            analysis_poly = area_gdf.union_all()
        else:
            center_gdf = gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326").to_crs(
                "EPSG:3857"
            )
            buffer_geom = center_gdf.geometry.iloc[0].buffer(radius_km * 1000)
            area_gdf = gpd.GeoDataFrame(geometry=[buffer_geom], crs="EPSG:3857").to_crs(
                "EPSG:4326"
            )
            analysis_poly = area_gdf.geometry.iloc[0]

        bounds = analysis_poly.bounds  # minx, miny, maxx, maxy
        warnings = []  # track any data source failures

        # 1. Elevation grid — sample a grid of points
        grid_points = []
        n_samples = 10
        minx, miny, maxx, maxy = bounds
        for xi in np.linspace(minx, maxx, n_samples):
            for yi in np.linspace(miny, maxy, n_samples):
                if analysis_poly.contains(Point(xi, yi)):
                    grid_points.append((yi, xi))  # lat, lon for API

        elevations = []
        if grid_points:
            # Batch query (max 100 per request)
            for i in range(0, len(grid_points), 100):
                batch = grid_points[i : i + 100]
                locs = "|".join(f"{la},{lo}" for la, lo in batch)
                url = f"https://api.opentopodata.org/v1/srtm90m?locations={locs}"
                try:
                    resp = requests.get(url, timeout=20)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "OK":
                            for r in data.get("results", []):
                                if r.get("elevation") is not None:
                                    elevations.append(r["elevation"])
                except Exception:
                    pass

        elev_min = min(elevations) if elevations else None
        elev_max = max(elevations) if elevations else None
        elev_mean = round(np.mean(elevations), 1) if elevations else None

        # Flood risk score (0-10, 10=highest risk)
        if elev_mean is not None:
            if elev_mean < 5:
                flood_score = 9
                flood_label = "VERY HIGH"
            elif elev_mean < 10:
                flood_score = 7
                flood_label = "HIGH"
            elif elev_mean < 20:
                flood_score = 4
                flood_label = "MODERATE"
            elif elev_mean < 50:
                flood_score = 2
                flood_label = "LOW"
            else:
                flood_score = 1
                flood_label = "VERY LOW"
        else:
            flood_score = None
            flood_label = "UNKNOWN"

        # 2. Water body proximity (rivers, lakes, coastline)
        water_count = 0
        water_dist_m = None
        try:
            water = ox.features_from_point(
                (lat, lon),
                tags={"natural": ["water", "coastline"]},
                dist=radius_km * 1000,
            )
            water_count = len(water)
            if water_count > 0:
                water_proj = water.to_crs("EPSG:3857")
                center_proj = (
                    gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
                    .to_crs("EPSG:3857")
                    .geometry.iloc[0]
                )
                water_dist_m = round(water_proj.distance(center_proj).min(), 0)
        except Exception as e:
            warnings.append(f"water bodies (OSM: {e})")

        # 3. Green space coverage
        green_area_pct = 0.0
        green_count = 0
        try:
            greens = ox.features_from_point(
                (lat, lon),
                tags={
                    "landuse": ["grass", "forest", "meadow", "recreation_ground"],
                    "leisure": ["park", "garden", "nature_reserve"],
                },
                dist=radius_km * 1000,
            )
            green_count = len(greens)
            if green_count > 0:
                greens_proj = greens.to_crs("EPSG:3857")
                area_proj = area_gdf.to_crs("EPSG:3857")
                green_total = greens_proj.geometry.area.sum()
                area_total = area_proj.geometry.area.sum()
                if area_total > 0:
                    green_area_pct = round((green_total / area_total) * 100, 1)
        except Exception as e:
            warnings.append(f"green space (OSM: {e})")

        # Green score (0-10, 10=best)
        if green_area_pct >= 30:
            green_score = 9
            green_label = "EXCELLENT"
        elif green_area_pct >= 15:
            green_score = 7
            green_label = "GOOD"
        elif green_area_pct >= 5:
            green_score = 4
            green_label = "MODERATE"
        else:
            green_score = 2
            green_label = "LOW"

        # 4. Industrial proximity
        industrial_count = 0
        industrial_dist_m = None
        try:
            industrial = ox.features_from_point(
                (lat, lon), tags={"landuse": "industrial"}, dist=radius_km * 1000
            )
            industrial_count = len(industrial)
            if industrial_count > 0:
                ind_proj = industrial.to_crs("EPSG:3857")
                center_proj = (
                    gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
                    .to_crs("EPSG:3857")
                    .geometry.iloc[0]
                )
                industrial_dist_m = round(ind_proj.distance(center_proj).min(), 0)
        except Exception as e:
            warnings.append(f"industrial sites (OSM: {e})")

        # Pollution score (0-10, 10=worst)
        if industrial_dist_m is not None and industrial_dist_m < 200:
            pollution_score = 9
            pollution_label = "VERY HIGH"
        elif industrial_dist_m is not None and industrial_dist_m < 500:
            pollution_score = 7
            pollution_label = "HIGH"
        elif industrial_dist_m is not None and industrial_dist_m < 1000:
            pollution_score = 4
            pollution_label = "MODERATE"
        else:
            pollution_score = 2
            pollution_label = "LOW"

        # 5. Major road proximity (noise proxy)
        road_dist_m = None
        try:
            roads = ox.features_from_point(
                (lat, lon),
                tags={"highway": ["motorway", "trunk", "primary"]},
                dist=radius_km * 1000,
            )
            if len(roads) > 0:
                roads_proj = roads.to_crs("EPSG:3857")
                center_proj = (
                    gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
                    .to_crs("EPSG:3857")
                    .geometry.iloc[0]
                )
                road_dist_m = round(roads_proj.distance(center_proj).min(), 0)
        except Exception as e:
            warnings.append(f"major roads (OSM: {e})")

        # Noise score (0-10, 10=worst)
        if road_dist_m is not None and road_dist_m < 50:
            noise_score = 9
            noise_label = "VERY HIGH"
        elif road_dist_m is not None and road_dist_m < 150:
            noise_score = 7
            noise_label = "HIGH"
        elif road_dist_m is not None and road_dist_m < 400:
            noise_score = 4
            noise_label = "MODERATE"
        else:
            noise_score = 2
            noise_label = "LOW"

        # Overall risk (weighted average)
        scores = []
        if flood_score is not None:
            scores.append(("flood", flood_score, 0.35))
        scores.append(("pollution", pollution_score, 0.25))
        scores.append(("noise", noise_score, 0.20))
        scores.append(("green_deficit", 10 - green_score, 0.20))

        total_weight = sum(w for _, _, w in scores)
        overall = (
            round(sum(s * w for _, s, w in scores) / total_weight, 1)
            if total_weight > 0
            else None
        )

        if overall is not None:
            if overall >= 7:
                overall_label = "HIGH RISK"
            elif overall >= 4:
                overall_label = "MODERATE RISK"
            else:
                overall_label = "LOW RISK"
        else:
            overall_label = "UNKNOWN"

        # Save summary point GPKG
        if not output_filename:
            safe = place_name.lower().replace(" ", "_").replace(",", "")[:20].strip("_")
            output_filename = f"{safe}_env_risk"

        summary_gdf = gpd.GeoDataFrame(
            [
                {
                    "place": place_name,
                    "overall_risk": overall,
                    "overall_label": overall_label,
                    "flood_score": flood_score,
                    "flood_label": flood_label,
                    "elev_min_m": elev_min,
                    "elev_max_m": elev_max,
                    "elev_mean_m": elev_mean,
                    "green_pct": green_area_pct,
                    "green_score": green_score,
                    "green_label": green_label,
                    "pollution_score": pollution_score,
                    "pollution_label": pollution_label,
                    "industrial_count": industrial_count,
                    "industrial_dist_m": industrial_dist_m,
                    "noise_score": noise_score,
                    "noise_label": noise_label,
                    "road_dist_m": road_dist_m,
                    "water_bodies": water_count,
                    "water_dist_m": water_dist_m,
                }
            ],
            geometry=[center],
            crs="EPSG:4326",
        )
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        summary_gdf.to_file(output_path, driver="GPKG")

        # Build response
        parts = [
            f"Environmental risk assessment for {place_name} ({radius_km}km radius):",
            f"",
            f"OVERALL: {overall}/10 — {overall_label}",
            f"",
            f"Flood risk:   {flood_score}/10 ({flood_label}) — mean elevation {elev_mean}m"
            + (
                f", range {elev_min:.0f}–{elev_max:.0f}m"
                if elev_min is not None
                else ""
            ),
            f"Pollution:    {pollution_score}/10 ({pollution_label})"
            + (
                f" — nearest industrial site {industrial_dist_m:.0f}m away"
                if industrial_dist_m
                else ""
            )
            + f", {industrial_count} industrial zones nearby",
            f"Noise:        {noise_score}/10 ({noise_label})"
            + (f" — nearest major road {road_dist_m:.0f}m away" if road_dist_m else ""),
            f"Green space:  {green_score}/10 ({green_label}) — {green_area_pct}% green coverage, {green_count} green areas",
            f"Water bodies: {water_count} nearby"
            + (f", nearest {water_dist_m:.0f}m away" if water_dist_m else ""),
        ]

        if warnings:
            parts.append("")
            parts.append(f"Note: Some data sources unavailable: {'; '.join(warnings)}")

        parts.append("")
        parts.append(
            f"Saved to outputs/{output_filename}.gpkg. Center: lon={lon:.4f}, lat={lat:.4f}"
        )
        return "\n".join(parts)

    except Exception as e:
        return (
            f"Environmental risk assessment failed: {str(e)}\n{traceback.format_exc()}"
        )


TOOL_FUNCTION = assess_environmental_risk
TOOL_SCHEMA = AssessEnvironmentalRiskArgs
