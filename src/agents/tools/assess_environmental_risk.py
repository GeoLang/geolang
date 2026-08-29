from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


LOW_BAND_M = 5.0  # same bands query_elevation uses: below 5m high, below 10m moderate
MID_BAND_M = 10.0

OVERPASS_TIMEOUT_S = 90

# every OSM layer the score reads, fetched in one query and split by tag after
OSM_LAYERS = {
    "water": {"natural": ["water", "coastline"]},
    "green": {
        "landuse": ["grass", "forest", "meadow", "recreation_ground"],
        "leisure": ["park", "garden", "nature_reserve"],
    },
    "industrial": {"landuse": ["industrial"]},
    "roads": {"highway": ["motorway", "trunk", "primary"]},
}


def pinned_geocode_hit(hits):
    """The hit Nominatim ranked first, or its equal-importance twin with the lowest id.

    Nominatim's own order weighs how well the name matched, so it stays: sorting
    by importance alone put New York (old name New Amsterdam) above Amsterdam.
    It can still swap equally ranked hits between calls, so those tie-break on
    osm_type and osm_id to keep the elevation sample grid anchored.
    """
    first = hits[0]
    importance = float(first.get("importance") or 0.0)
    tied = [hit for hit in hits if float(hit.get("importance") or 0.0) == importance]
    return min(tied, key=lambda hit: (str(hit.get("osm_type") or ""), int(hit.get("osm_id") or 0)))


def merged_tags(layers):
    merged = {}
    for tags in layers.values():
        for key, values in tags.items():
            known = merged.setdefault(key, [])
            known.extend(value for value in values if value not in known)
    return merged


OSM_TAGS = merged_tags(OSM_LAYERS)


def tagged_rows(features, tags):
    """The rows carrying any of these tag values, an absent column matches none."""
    mask = None
    for key, values in tags.items():
        if key not in features.columns:
            continue
        hit = features[key].isin(values)
        mask = hit if mask is None else (mask | hit)
    if mask is None:
        return features.iloc[0:0]
    return features[mask]


def flood_score_from(elevations, water_dist_m=None):
    """
    Flood score (0-10, 10=worst) and label from the elevation samples.

    Mean elevation hides hilly coastal cities: San Francisco means 22m yet its
    0-5m waterfront floods. So score the low-lying share of samples, saturating
    at a quarter of the area (a quarter below a band already floods, more of it
    isn't worse), take the worse band, then amplify by water proximity. The
    amplifier multiplies, so water near high ground stays low risk and far water
    never dampens the terrain signal.
    """
    if not elevations:
        return None, "UNKNOWN"

    n = len(elevations)
    share_low = sum(1 for e in elevations if e < LOW_BAND_M) / n
    share_mid = sum(1 for e in elevations if e < MID_BAND_M) / n
    exposure_low = min(1.0, share_low / 0.25)
    exposure_mid = min(1.0, share_mid / 0.25)
    score = max(9.0 * exposure_low, 7.0 * exposure_mid)

    if water_dist_m is not None:
        if water_dist_m < 250:
            score *= 1.3
        elif water_dist_m < 1000:
            score *= 1.15

    score = max(1, min(10, int(round(score))))
    if score >= 8:
        label = "VERY HIGH"
    elif score >= 6:
        label = "HIGH"
    elif score >= 3:
        label = "MODERATE"
    elif score >= 2:
        label = "LOW"
    else:
        label = "VERY LOW"
    return score, label


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
            "instead of a circular buffer. A filename in outputs/, not a path."
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
    1. Flood risk: share of the sampled elevation grid below 5m/10m, amplified by
       water proximity (OpenTopoData SRTM 90m grid)
    2. Proximity to water bodies (rivers, coastline from OSM)
    3. Green space / tree cover percentage (OSM landuse)
    4. Industrial site proximity (OSM industrial landuse)
    5. Major road proximity (noise/air pollution proxy)

    Returns a risk summary with scores and saves a polygon GPKG of the assessment
    area (the radius_km buffer, or the supplied polygon) attributed with every
    score, readable per feature with the Feature Picker.
    Use this when the user asks about flood risk, environmental suitability,
    pollution, or green space for a location.
    """
    import time
    import traceback

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import Point

        # Geocode — if place_name looks like "lat,lon" or "lat lon", parse directly
        import re as _re

        _coord_m = _re.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
        )
        if _coord_m:
            lat, lon = float(_coord_m.group(1)), float(_coord_m.group(2))
        else:
            lat, lon = None, None
            try:
                _geo_resp = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": place_name,
                        "format": "json",
                        "limit": 10,
                        "dedupe": 0,
                    },
                    headers={"User-Agent": "geolang-gis-agent/1.0"},
                    timeout=20,
                )
                _hits = _geo_resp.json() if _geo_resp.status_code == 200 else []
            except Exception:
                _hits = []
            if isinstance(_hits, list) and _hits:
                _best = pinned_geocode_hit(_hits)
                lat, lon = float(_best["lat"]), float(_best["lon"])
            if lat is None:
                lat, lon = ox.geocode(place_name)
        # quantise the anchor so geocoder jitter below ~10m cannot shift the grid
        lat, lon = round(lat, 4), round(lon, 4)
        center = Point(lon, lat)

        # Build analysis area
        if polygon_path:
            poly_file = tool_input_path("polygon_path", polygon_path)
            area_gdf = gpd.read_file(poly_file)
            if area_gdf.crs and area_gdf.crs.to_epsg() != 4326:
                area_gdf = area_gdf.to_crs("EPSG:4326")
            analysis_poly = area_gdf.union_all()
        else:
            # buffer in local UTM so radius_km is true metres on the ground, not
            # 3857 metres (which are stretched by 1/cos(lat))
            center_gdf = gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
            utm = center_gdf.estimate_utm_crs()
            buffer_geom = (
                center_gdf.to_crs(utm).geometry.iloc[0].buffer(radius_km * 1000)
            )
            area_gdf = gpd.GeoDataFrame(geometry=[buffer_geom], crs=utm).to_crs(
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
                    # round so identical inputs build a byte-identical query URL
                    grid_points.append((round(yi, 5), round(xi, 5)))  # lat, lon for API

        elevations = []
        if grid_points:
            # Batch query (max 100 per request)
            for i in range(0, len(grid_points), 100):
                batch = grid_points[i : i + 100]
                locs = "|".join(f"{la},{lo}" for la, lo in batch)
                url = f"https://api.opentopodata.org/v1/srtm90m?locations={locs}"
                batch_elevs = None
                for attempt in range(3):
                    try:
                        resp = requests.get(url, timeout=20)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("status") == "OK":
                                batch_elevs = [
                                    r["elevation"]
                                    for r in data.get("results", [])
                                    if r.get("elevation") is not None
                                ]
                                break
                    except Exception:
                        pass
                    time.sleep(1.1)  # opentopodata fair-use: max 1 req/sec
                # a dropped batch would silently change the elevation stats, so say so
                if batch_elevs is None:
                    warnings.append("elevation samples (OpenTopoData batch failed)")
                else:
                    elevations.extend(batch_elevs)

        elev_min = min(elevations) if elevations else None
        elev_max = max(elevations) if elevations else None
        elev_mean = round(np.mean(elevations), 1) if elevations else None
        low_lying_pct = (
            round(
                100.0 * sum(1 for e in elevations if e < LOW_BAND_M) / len(elevations),
                1,
            )
            if elevations
            else None
        )

        # 2 to 5. one Overpass round trip for every layer: four queries on a
        # dense city took minutes and tripped the rate limit between them
        osm_layers = {}
        try:
            ox.settings.requests_timeout = OVERPASS_TIMEOUT_S
            features = ox.features_from_point(
                (lat, lon), tags=OSM_TAGS, dist=radius_km * 1000
            ).to_crs("EPSG:3857")
            osm_layers = {
                name: tagged_rows(features, tags) for name, tags in OSM_LAYERS.items()
            }
        except Exception as e:
            warnings.append(f"OSM layers (Overpass: {e})")
        center_proj = (
            gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
            .to_crs("EPSG:3857")
            .geometry.iloc[0]
        )

        def layer_count(name):
            layer = osm_layers.get(name)
            return 0 if layer is None else len(layer)

        def nearest_m(name):
            if layer_count(name) == 0:
                return None
            return round(osm_layers[name].distance(center_proj).min(), 0)

        water_count = layer_count("water")
        water_dist_m = nearest_m("water")

        # scored here, not with the samples, because it needs the water distance
        flood_score, flood_label = flood_score_from(elevations, water_dist_m)

        # 3. Green space coverage
        green_area_pct = 0.0
        green_count = layer_count("green")
        if green_count > 0:
            green_total = osm_layers["green"].geometry.area.sum()
            area_total = area_gdf.to_crs("EPSG:3857").geometry.area.sum()
            if area_total > 0:
                green_area_pct = round((green_total / area_total) * 100, 1)

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
        industrial_count = layer_count("industrial")
        industrial_dist_m = nearest_m("industrial")

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
        road_dist_m = nearest_m("roads")

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

        # Save the assessment AREA as the rendered geometry, attributed with every
        # score. The centroid is kept as center_lon/center_lat properties.
        if not output_filename:
            safe = place_name.lower().replace(" ", "_").replace(",", "")[:20].strip("_")
            output_filename = f"{safe}_env_risk"

        area_series = gpd.GeoSeries([analysis_poly], crs="EPSG:4326")
        area_km2 = round(
            area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0] / 1e6, 2
        )

        summary_gdf = gpd.GeoDataFrame(
            [
                {
                    "place": place_name,
                    "area_km2": area_km2,
                    "radius_km": radius_km,
                    "center_lon": round(lon, 6),
                    "center_lat": round(lat, 6),
                    "overall_risk": overall,
                    "overall_label": overall_label,
                    "flood_score": flood_score,
                    "flood_label": flood_label,
                    "elev_min_m": elev_min,
                    "elev_max_m": elev_max,
                    "elev_mean_m": elev_mean,
                    "low_lying_pct": low_lying_pct,
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
            geometry=[analysis_poly],
            crs="EPSG:4326",
        )
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
        summary_gdf.to_file(output_path, driver="GPKG")

        # Build response
        parts = [
            f"Environmental risk assessment for {place_name} ({radius_km}km radius):",
            "",
            f"OVERALL: {overall}/10 — {overall_label}",
            "",
            f"Flood risk:   {flood_score}/10 ({flood_label}) — mean elevation {elev_mean}m"
            + (
                f", range {elev_min:.0f}–{elev_max:.0f}m"
                if elev_min is not None
                else ""
            )
            + (
                f", {low_lying_pct}% of samples below {LOW_BAND_M:.0f}m"
                if low_lying_pct is not None
                else ""
            )
            + (
                f", nearest water {water_dist_m:.0f}m"
                if water_dist_m is not None
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
            f"Saved to outputs/{output_filename}.gpkg. "
            f"That layer is the {area_km2} km2 assessment area polygon attributed "
            f"with every score (overall_risk is 0-10), which the Feature Picker "
            f"panel shows when the user clicks it. "
            f"Center: lon={lon:.4f}, lat={lat:.4f}"
        )
        return "\n".join(parts)

    except Exception as e:
        return (
            f"Environmental risk assessment failed: {str(e)}\n{traceback.format_exc()}"
        )


TOOL_FUNCTION = assess_environmental_risk
TOOL_SCHEMA = AssessEnvironmentalRiskArgs
