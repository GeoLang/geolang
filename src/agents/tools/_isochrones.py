# travel mode to the osmnx network type and the speed used when an edge has none
MODE_NETWORKS = {
    "walking": ("walk", 5.0),
    "cycling": ("bike", 15.0),
    "driving": ("drive", 30.0),
}

# OSM highway filter strings for each detail level
ROAD_FILTERS = {
    "full": None,  # use network_type, no custom filter
    "major": '["highway"~"motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link"]',
    "motorway": '["highway"~"motorway|motorway_link|trunk|trunk_link"]',
}

# valhalla rejects a request with more contours than service_limits.isochrone.max_contours
VALHALLA_MAX_CONTOURS = 4

VALHALLA_URL = "https://valhalla1.openstreetmap.de/isochrone"

# metres each road edge is widened by before the bands are drawn
EDGE_BUFFER_M = {"walk": 80, "bike": 100}

# how far past the straight-line reach of the slowest band the network is downloaded
NETWORK_RADIUS_MARGIN = 1.25

CONCAVE_HULL_RATIO = 0.3

# degrees each node is buffered by when the concave hull fails
NODE_BUFFER_DEGREES = 0.002


def isochrone_polygons(
    lat: float, lon: float, travel_mode: str, times: list[int], road_detail: str
) -> list[dict] | str:
    network_type, fallback_kph = MODE_NETWORKS.get(travel_mode.lower(), ("walk", 5.0))
    if network_type == "drive":
        return _valhalla_polygons(lat, lon, travel_mode, times)
    return _network_polygons(
        lat, lon, travel_mode, times, road_detail, network_type, fallback_kph
    )


def _valhalla_polygons(
    lat: float, lon: float, travel_mode: str, times: list[int]
) -> list[dict]:
    import requests
    from shapely.geometry import shape

    features = []
    descending_times = sorted(times, reverse=True)
    for start in range(0, len(descending_times), VALHALLA_MAX_CONTOURS):
        chunk = descending_times[start : start + VALHALLA_MAX_CONTOURS]
        payload = {
            "locations": [{"lon": lon, "lat": lat}],
            "costing": "auto",
            "contours": [{"time": t_min} for t_min in chunk],
            "polygons": True,
            "denoise": 0.5,
            "generalize": 150,
        }
        resp = requests.post(VALHALLA_URL, json=payload, timeout=30)
        resp.raise_for_status()
        fc = resp.json()

        # label from properties.contour, a dropped contour would shift positions
        if fc.get("type") != "FeatureCollection":
            continue
        for feature in fc.get("features") or []:
            geom = feature.get("geometry")
            contour = (feature.get("properties") or {}).get("contour")
            if geom is None or contour is None:
                continue
            features.append(
                {
                    "geometry": shape(geom),
                    "minutes": int(contour),
                    "mode": travel_mode,
                    "road_detail": "valhalla",
                }
            )
    return features


def _network_polygons(
    lat: float,
    lon: float,
    travel_mode: str,
    times: list[int],
    road_detail: str,
    network_type: str,
    fallback_kph: float,
) -> list[dict] | str:
    import geopandas as gpd
    import networkx as nx
    import osmnx as ox
    from shapely.geometry import Point
    from shapely.ops import unary_union

    detail = road_detail.lower().strip()
    if detail == "auto":
        detail = "full"  # always full for walk/cycle

    if detail not in ROAD_FILTERS:
        return f"Invalid road_detail '{road_detail}'. Choose: full, major, motorway, auto."

    buf_m = EDGE_BUFFER_M[network_type]
    max_dist = int(max(times) * 60 * (fallback_kph / 3.6) * NETWORK_RADIUS_MARGIN)

    custom_filter = ROAD_FILTERS[detail]
    if custom_filter:
        G = ox.graph_from_point(
            (lat, lon),
            dist=max_dist,
            custom_filter=custom_filter,
            retain_all=False,
        )
    else:
        G = ox.graph_from_point((lat, lon), dist=max_dist, network_type=network_type)

    for u, v, data in G.edges(data=True):
        data["travel_time"] = data.get("length", 50) / (fallback_kph / 3.6)

    center_node = ox.nearest_nodes(G, lon, lat)

    features = []
    for t_min in sorted(times, reverse=True):
        t_sec = t_min * 60
        subgraph = nx.ego_graph(G, center_node, radius=t_sec, distance="travel_time")

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

            hull = concave_hull(nodes_proj, ratio=CONCAVE_HULL_RATIO)
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
            poly = unary_union([p.buffer(NODE_BUFFER_DEGREES) for p in node_pts])

        features.append(
            {
                "geometry": poly,
                "minutes": t_min,
                "mode": travel_mode,
                "road_detail": detail,
            }
        )
    return features
