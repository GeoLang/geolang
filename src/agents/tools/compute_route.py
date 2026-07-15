from pydantic import BaseModel, Field
from typing import Optional


class ComputeRouteArgs(BaseModel):
    origin: str = Field(
        ...,
        description="Starting location name or address. E.g. 'King's Cross, London'.",
    )
    destination: str = Field(
        ...,
        description="Destination location name or address. E.g. 'Heathrow Airport'.",
    )
    travel_mode: str = Field(
        "driving",
        description="Travel mode: 'driving', 'cycling', or 'walking'.",
    )
    alternatives: bool = Field(
        False,
        description="If true, request up to 3 alternative routes for comparison.",
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def compute_route(
    origin: str,
    destination: str,
    travel_mode: str = "driving",
    alternatives: bool = False,
    output_filename: str = None,
) -> str:
    """
    Compute a route between two locations. Tries the platform's itinera routing
    engine first (routes on the loaded OSM extract), falling back to the public
    Valhalla engine for coverage outside it or when alternatives are requested.
    Returns travel time, distance, turn-by-turn summary, and saves the route
    geometry as a GPKG linestring. Optionally returns up to 3 alternative routes.

    Use this when the user asks for directions, travel time between places,
    route comparison, or optimal path. Works for driving, cycling, and walking.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import requests
        import json
        import osmnx as ox
        import geopandas as gpd
        from shapely.geometry import LineString

        # Geocode origin and destination
        orig_lat, orig_lon = ox.geocode(origin)
        dest_lat, dest_lon = ox.geocode(destination)

        # platform itinera first (no alternates support, so skip when asked for them)
        itinera_url = os.environ.get("ITINERA_URL")
        if itinera_url and not alternatives:
            profile_map = {
                "driving": "car",
                "cycling": "bicycle",
                "walking": "pedestrian",
            }
            try:
                resp = requests.get(
                    f"{itinera_url.rstrip('/')}/route",
                    params={
                        "from": f"{orig_lat},{orig_lon}",
                        "to": f"{dest_lat},{dest_lon}",
                        "profile": profile_map.get(travel_mode.lower(), "car"),
                    },
                    timeout=15,
                )
                if resp.ok:
                    data = resp.json()
                    geometry = data.get("geometry", [])
                    if len(geometry) >= 2:
                        coords = [(lon, lat) for lat, lon in geometry]
                        distance_km = round(data.get("distance_m", 0) / 1000, 1)
                        time_minutes = round(data.get("duration_s", 0) / 60, 1)
                        gdf = gpd.GeoDataFrame(
                            [
                                {
                                    "route": "primary",
                                    "distance_km": distance_km,
                                    "time_minutes": time_minutes,
                                    "origin": origin,
                                    "destination": destination,
                                    "mode": travel_mode,
                                    "geometry": LineString(coords),
                                }
                            ],
                            geometry="geometry",
                            crs="EPSG:4326",
                        )
                        if not output_filename:
                            safe_o = (
                                origin.lower().replace(" ", "_").replace(",", "")[:12].strip("_")
                            )
                            safe_d = (
                                destination.lower()
                                .replace(" ", "_")
                                .replace(",", "")[:12]
                                .strip("_")
                            )
                            output_filename = f"route_{safe_o}_to_{safe_d}"
                        if output_filename.lower().endswith(".gpkg"):
                            output_filename = output_filename[:-5]
                        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
                        gdf.to_file(output_path, driver="GPKG")

                        steps = data.get("steps", [])
                        turn_summary = [
                            f"  • {s['maneuver']} onto {s['name']} ({s.get('distance_m', 0) / 1000:.1f}km)"
                            for s in steps[:6]
                            if s.get("maneuver") and s.get("name")
                        ]
                        parts = [
                            f"Route: {origin} → {destination} ({travel_mode}, itinera)",
                            f"Distance: {distance_km}km",
                            f"Travel time: {time_minutes} minutes",
                        ]
                        if turn_summary:
                            parts.append("")
                            parts.append("Key directions:")
                            parts.extend(turn_summary)
                        parts.append("")
                        parts.append(
                            f"Saved to outputs/{output_filename}.gpkg. "
                            f"Center: lon={(orig_lon + dest_lon) / 2:.4f}, "
                            f"lat={(orig_lat + dest_lat) / 2:.4f}"
                        )
                        return "\n".join(parts)
            except Exception:
                pass  # itinera unavailable or outside its extract: fall back to Valhalla

        costing_map = {"driving": "auto", "cycling": "bicycle", "walking": "pedestrian"}
        costing = costing_map.get(travel_mode.lower(), "auto")

        # Valhalla route request
        valhalla_url = "https://valhalla1.openstreetmap.de/route"
        payload = {
            "locations": [
                {"lon": orig_lon, "lat": orig_lat},
                {"lon": dest_lon, "lat": dest_lat},
            ],
            "costing": costing,
            "directions_options": {"units": "kilometers"},
            "alternates": 2 if alternatives else 0,
        }

        resp = requests.post(valhalla_url, json=payload, timeout=30)
        resp.raise_for_status()
        valhalla_result = resp.json()

        trip = valhalla_result.get("trip", {})
        legs_list = trip.get("legs", [])

        if not legs_list:
            return f"No route found between {origin} and {destination}."

        # Parse all routes (primary + alternates)
        routes = []

        # Primary route
        primary_leg = legs_list[0]
        primary_shape = primary_leg.get("shape", "")
        primary_summary = primary_leg.get("summary", {})

        if primary_shape:
            coords = []
            enc = primary_shape
            idx = 0
            lat_a = 0
            lng_a = 0
            while idx < len(enc):
                shift, result = 0, 0
                while True:
                    b = ord(enc[idx]) - 63
                    idx += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                lat_a += ~(result >> 1) if (result & 1) else (result >> 1)
                shift, result = 0, 0
                while True:
                    b = ord(enc[idx]) - 63
                    idx += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                lng_a += ~(result >> 1) if (result & 1) else (result >> 1)
                coords.append((lng_a / 1e6, lat_a / 1e6))
            if coords:
                routes.append(
                    {
                        "route": "primary",
                        "distance_km": round(primary_summary.get("length", 0), 1),
                        "time_minutes": round(primary_summary.get("time", 0) / 60, 1),
                        "geometry": LineString(coords),
                    }
                )

        # Check for alternates in the response
        alternates = valhalla_result.get("alternates", [])
        for i, alt in enumerate(alternates[:2]):
            alt_trip = alt.get("trip", {})
            alt_legs = alt_trip.get("legs", [])
            if alt_legs:
                alt_leg = alt_legs[0]
                alt_shape = alt_leg.get("shape", "")
                alt_summary = alt_leg.get("summary", {})
                if alt_shape:
                    coords = []
                    enc = alt_shape
                    idx = 0
                    lat_a = 0
                    lng_a = 0
                    while idx < len(enc):
                        shift, result = 0, 0
                        while True:
                            b = ord(enc[idx]) - 63
                            idx += 1
                            result |= (b & 0x1F) << shift
                            shift += 5
                            if b < 0x20:
                                break
                        lat_a += ~(result >> 1) if (result & 1) else (result >> 1)
                        shift, result = 0, 0
                        while True:
                            b = ord(enc[idx]) - 63
                            idx += 1
                            result |= (b & 0x1F) << shift
                            shift += 5
                            if b < 0x20:
                                break
                        lng_a += ~(result >> 1) if (result & 1) else (result >> 1)
                        coords.append((lng_a / 1e6, lat_a / 1e6))
                    if coords:
                        routes.append(
                            {
                                "route": f"alternative_{i + 1}",
                                "distance_km": round(alt_summary.get("length", 0), 1),
                                "time_minutes": round(
                                    alt_summary.get("time", 0) / 60, 1
                                ),
                                "geometry": LineString(coords),
                            }
                        )

        if not routes:
            return f"Route computed but could not decode geometry for {origin} → {destination}."

        # Build GeoDataFrame
        for r in routes:
            r["origin"] = origin
            r["destination"] = destination
            r["mode"] = travel_mode

        gdf = gpd.GeoDataFrame(routes, geometry="geometry", crs="EPSG:4326")

        if not output_filename:
            safe_o = origin.lower().replace(" ", "_").replace(",", "")[:12].strip("_")
            safe_d = (
                destination.lower().replace(" ", "_").replace(",", "")[:12].strip("_")
            )
            output_filename = f"route_{safe_o}_to_{safe_d}"

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        gdf.to_file(output_path, driver="GPKG")

        # Build manoeuvre summary for primary route
        manoeuvres = primary_leg.get("maneuvers", [])
        turn_summary = []
        for m in manoeuvres[:8]:
            instruction = m.get("instruction", "")
            dist = m.get("length", 0)
            if instruction:
                turn_summary.append(f"  • {instruction} ({dist:.1f}km)")

        # Build response
        primary = routes[0]
        parts = [
            f"Route: {origin} → {destination} ({travel_mode})",
            f"Distance: {primary['distance_km']}km",
            f"Travel time: {primary['time_minutes']} minutes",
        ]

        if len(routes) > 1:
            parts.append("")
            parts.append("Alternatives:")
            for r in routes[1:]:
                parts.append(
                    f"  {r['route']}: {r['distance_km']}km, " f"{r['time_minutes']} min"
                )

        if turn_summary:
            parts.append("")
            parts.append("Key directions:")
            parts.extend(turn_summary[:6])

        # Centre point for map display
        mid_lat = (orig_lat + dest_lat) / 2
        mid_lon = (orig_lon + dest_lon) / 2

        parts.append("")
        parts.append(
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Center: lon={mid_lon:.4f}, lat={mid_lat:.4f}"
        )

        return "\n".join(parts)

    except Exception as e:
        return f"Route computation failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = compute_route
TOOL_SCHEMA = ComputeRouteArgs
