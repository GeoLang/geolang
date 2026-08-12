from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import caller_outputs_dir


class CalculateIsochronesArgs(BaseModel):
    place_name: str = Field(
        ...,
        description="Location to calculate isochrones from. E.g. '10 Downing Street, London' or 'Canary Wharf, London'.",
    )
    travel_mode: str = Field(
        "walking",
        description="Travel mode: 'walking', 'cycling', or 'driving'.",
    )
    time_minutes: str = Field(
        "5,10,15",
        description="Comma-separated travel time thresholds in minutes. E.g. '5,10,15' or '10,20,30'.",
    )
    road_detail: str = Field(
        "auto",
        description=(
            "Road network detail level — controls download size and accuracy. "
            "Choose based on travel mode and time:\n"
            "  'full'         — all roads including residential. "
            "Use for walking/cycling, or short drives (≤15 min).\n"
            "  'major'        — motorways, trunk, primary, secondary only. "
            "Use for driving 15–60 min. Good for logistics/HGV routing.\n"
            "  'motorway'     — motorways and trunk roads only. "
            "Use for long drives (>60 min) or inter-city coverage analysis.\n"
            "  'auto'         — let the tool decide based on travel_mode and time_minutes "
            "(default, safe choice when unsure)."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


# OSM highway filter strings for each detail level
_ROAD_FILTERS = {
    "full": None,  # use network_type, no custom filter
    "major": '["highway"~"motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link"]',
    "motorway": '["highway"~"motorway|motorway_link|trunk|trunk_link"]',
}


def calculate_isochrones(
    place_name: str,
    travel_mode: str = "walking",
    time_minutes: str = "5,10,15",
    road_detail: str = "auto",
    output_filename: str = None,
) -> str:
    """
    Calculate walk, cycle, or drive time isochrones (catchment areas) around a
    location using OpenStreetMap road networks. Returns polygons showing how far
    you can travel in each time threshold.

    road_detail controls the network download size:
      'full'     — all roads (walking/cycling or short drives ≤15 min)
      'major'    — motorway/trunk/primary/secondary (driving 15–60 min, logistics)
      'motorway' — motorway/trunk only (long drives >60 min, inter-city)
      'auto'     — tool decides based on mode and time (default)
    """
    import os
    import traceback

    outputs_dir = caller_outputs_dir()

    try:
        import osmnx as ox
        import geopandas as gpd
        from shapely.geometry import Point

        # Parse times
        times = sorted(set(int(t.strip()) for t in time_minutes.split(",")))
        if not times:
            return "No valid time values provided."
        if max(times) > 60 and travel_mode.lower() == "walking":
            return "Walking isochrones over 60 minutes cover too large an area. Use driving or reduce the time."

        mode_cfg = {
            "walking": ("walk", 5.0),
            "cycling": ("bike", 15.0),
            "driving": ("drive", 30.0),
        }
        network_type, fallback_kph = mode_cfg.get(travel_mode.lower(), ("walk", 5.0))

        location = ox.geocode(place_name)
        lat, lon = location

        if not output_filename:
            safe_place = (
                place_name.lower().replace(" ", "_").replace(",", "")[:18].strip("_")
            )
            output_filename = f"{safe_place}_{travel_mode[:4]}_isochrones"

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")

        # --- DRIVING: use Valhalla public API (fast, server-side, no timeout risk) ---
        if network_type == "drive":
            import requests

            valhalla_url = "https://valhalla1.openstreetmap.de/isochrone"
            features = []

            for t_min in sorted(times, reverse=True):
                payload = {
                    "locations": [{"lon": lon, "lat": lat}],
                    "costing": "auto",
                    "contours": [{"time": t_min}],
                    "polygons": True,
                    "denoise": 0.5,
                    "generalize": 150,
                }
                resp = requests.post(valhalla_url, json=payload, timeout=30)
                resp.raise_for_status()
                fc = resp.json()

                # Extract the polygon from the FeatureCollection
                poly = None
                if fc.get("type") == "FeatureCollection" and fc.get("features"):
                    geom = fc["features"][0].get("geometry", {})
                    from shapely.geometry import shape

                    poly = shape(geom)

                if poly is not None:
                    features.append(
                        {
                            "geometry": poly,
                            "minutes": t_min,
                            "mode": travel_mode,
                            "road_detail": "valhalla",
                            "place": place_name,
                        }
                    )

            if not features:
                return f"Could not compute driving isochrones for {place_name} via Valhalla API."

            gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
            gdf.to_file(output_path, driver="GPKG")

            time_str = ", ".join(str(t) for t in sorted(times))
            return (
                f"Computed driving isochrones ({time_str} min) around {place_name} "
                f"using Valhalla routing. "
                f"{len(gdf)} zones saved to outputs/{output_filename}.gpkg. "
                f"Center: lon={lon:.4f}, lat={lat:.4f}"
            )

        # --- WALKING / CYCLING: use OSMnx (fast enough for these modes) ---
        import networkx as nx
        from shapely.ops import unary_union

        # Resolve 'auto' → concrete level based on mode + time
        detail = road_detail.lower().strip()
        if detail == "auto":
            detail = "full"  # always full for walk/cycle

        if detail not in _ROAD_FILTERS:
            return f"Invalid road_detail '{road_detail}'. Choose: full, major, motorway, auto."

        # Edge buffer size in metres
        buf_m = {"walk": 80, "bike": 100}[network_type]

        # Network download radius
        max_dist = int(max(times) * 60 * (fallback_kph / 3.6) * 1.25)

        custom_filter = _ROAD_FILTERS[detail]
        if custom_filter:
            G = ox.graph_from_point(
                (lat, lon),
                dist=max_dist,
                custom_filter=custom_filter,
                retain_all=False,
            )
        else:
            G = ox.graph_from_point(
                (lat, lon), dist=max_dist, network_type=network_type
            )

        # Travel times
        for u, v, data in G.edges(data=True):
            data["travel_time"] = data.get("length", 50) / (fallback_kph / 3.6)

        center_node = ox.nearest_nodes(G, lon, lat)

        # Build isochrone polygon for each threshold
        features = []
        for t_min in sorted(times, reverse=True):
            t_sec = t_min * 60
            subgraph = nx.ego_graph(
                G, center_node, radius=t_sec, distance="travel_time"
            )

            if subgraph.number_of_nodes() < 3:
                continue

            try:
                from shapely import concave_hull
                from shapely.geometry import MultiPoint

                node_coords = [(d["x"], d["y"]) for _, d in subgraph.nodes(data=True)]

                node_gdf = gpd.GeoDataFrame(
                    geometry=[MultiPoint(node_coords)], crs="EPSG:4326"
                ).to_crs("EPSG:3857")
                nodes_proj = node_gdf.geometry.iloc[0]

                hull = concave_hull(nodes_proj, ratio=0.3)
                poly_proj = hull.buffer(buf_m)
                if poly_proj.geom_type == "MultiPolygon":
                    poly_proj = max(poly_proj.geoms, key=lambda g: g.area)

                poly = (
                    gpd.GeoDataFrame(geometry=[poly_proj], crs="EPSG:3857")
                    .to_crs("EPSG:4326")
                    .geometry.iloc[0]
                )

            except Exception:
                node_pts = [Point(d["x"], d["y"]) for _, d in subgraph.nodes(data=True)]
                poly = unary_union([p.buffer(0.002) for p in node_pts])

            features.append(
                {
                    "geometry": poly,
                    "minutes": t_min,
                    "mode": travel_mode,
                    "road_detail": detail,
                    "place": place_name,
                }
            )

        if not features:
            return f"Could not compute isochrones for {place_name}."

        gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
        gdf.to_file(output_path, driver="GPKG")

        time_str = ", ".join(str(t) for t in sorted(times))
        return (
            f"Computed {travel_mode} isochrones ({time_str} min) around {place_name} "
            f"using road_detail='{detail}'. "
            f"{len(gdf)} zones saved to outputs/{output_filename}.gpkg. "
            f"Center: lon={lon:.4f}, lat={lat:.4f}"
        )

    except Exception as e:
        return f"Isochrone calculation failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = calculate_isochrones
TOOL_SCHEMA = CalculateIsochronesArgs
