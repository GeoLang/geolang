from __future__ import annotations

import os

import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union

from src.core.external_pacing import wait_for_turn

GEOKODE_URL_ENV = "GEOKODE_URL"
GEOKODE_TIMEOUT_SECONDS = 15
GEOKODE_BATCH_MAXIMUM_QUERIES = 100
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# overpass-api.de answers 406 to the default python-requests user agent
OVERPASS_USER_AGENT = "geolang-gis-agent/1.0"
OVERPASS_QUERY_TIMEOUT_SECONDS = 90
# longer than the query's own timeout, so overpass reports it instead of the socket
OVERPASS_REQUEST_TIMEOUT_SECONDS = OVERPASS_QUERY_TIMEOUT_SECONDS + 10
OUTLINED_OSM_TYPES = ("way", "relation")
AREA_RELATION_TYPES = ("multipolygon", "boundary")
# a closed way carrying one of these is a loop of line, not an area
LINEAR_WAY_KEYS = ("highway", "barrier", "railway", "waterway")
OUTER_ROLES = ("outer", "")


class GeocoderUnavailable(RuntimeError):
    pass


def geocode(query: str, limit: int = 5) -> list[dict]:
    import requests

    return _geokode_results(
        lambda base_url: requests.get(
            f"{base_url}/forward",
            params={"q": query, "limit": limit},
            timeout=GEOKODE_TIMEOUT_SECONDS,
        )
    )


def geocode_batch(queries: list[str], limit: int = 1) -> list[list[dict]]:
    import requests

    answers = []
    for start in range(0, len(queries), GEOKODE_BATCH_MAXIMUM_QUERIES):
        chunk = queries[start : start + GEOKODE_BATCH_MAXIMUM_QUERIES]
        answers.extend(
            _geokode_results(
                lambda base_url: requests.post(
                    f"{base_url}/batch",
                    json={"queries": chunk, "limit": limit},
                    timeout=GEOKODE_TIMEOUT_SECONDS,
                )
            )
        )
    return answers


def geocode_point(query: str) -> tuple[float, float] | None:
    results = geocode(query, limit=1)
    if not results:
        return None
    return results[0]["lat"], results[0]["lon"]


def place_not_found(query: str) -> str:
    return f"The platform geocoder found no place named '{query}'."


def first_outlined_hit(results: list[dict], admin_level: int | None = None) -> dict | None:
    return next(
        (
            hit
            for hit in results
            if hit["osm_type"] in OUTLINED_OSM_TYPES
            and hit["osm_id"] is not None
            and (admin_level is None or hit["admin_level"] == admin_level)
        ),
        None,
    )


def osm_geometry(osm_type: str, osm_id: int) -> BaseGeometry:
    import requests

    query = (
        f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}];"
        f"{osm_type}({int(osm_id)});out geom;"
    )
    wait_for_turn(OVERPASS_URL)
    response = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers={"User-Agent": OVERPASS_USER_AGENT},
        timeout=OVERPASS_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    elements = response.json()["elements"]
    if not elements:
        raise LookupError(f"Overpass has no {osm_type} {osm_id}.")
    element = elements[0]
    if osm_type == "node":
        return Point(element["lon"], element["lat"])
    if osm_type == "way":
        return _way_geometry(element)
    return _relation_geometry(element)


def _geokode_results(send) -> list:
    base_url = os.environ.get(GEOKODE_URL_ENV, "").rstrip("/")
    if not base_url:
        raise GeocoderUnavailable(
            f"{GEOKODE_URL_ENV} is not set, so there is no geocoder to look places up in."
        )
    try:
        response = send(base_url)
    # requests' own exceptions subclass OSError
    except OSError as error:
        raise GeocoderUnavailable(f"geokode at {base_url} is unreachable: {error}") from error
    if response.status_code >= 500:
        raise GeocoderUnavailable(
            f"geokode at {base_url} answered HTTP {response.status_code}."
        )
    response.raise_for_status()
    return response.json()["results"]


def _line(points: list[dict]) -> LineString:
    return LineString([(point["lon"], point["lat"]) for point in points])


def _way_geometry(way: dict) -> BaseGeometry:
    line = _line(way["geometry"])
    tags = way.get("tags", {})
    linear = any(key in tags for key in LINEAR_WAY_KEYS) and tags.get("area") != "yes"
    if line.is_ring and not linear and tags.get("area") != "no":
        return Polygon(line.coords)
    return line


def _relation_geometry(relation: dict) -> BaseGeometry:
    member_ways = [member for member in relation["members"] if member["type"] == "way"]
    if relation.get("tags", {}).get("type") not in AREA_RELATION_TYPES:
        merged = linemerge([_line(member["geometry"]) for member in member_ways])
        return MultiLineString(list(shapely.get_parts(merged)))

    outers = _rings(member for member in member_ways if member["role"] in OUTER_ROLES)
    inners = _rings(member for member in member_ways if member["role"] == "inner")
    if not outers:
        raise LookupError(f"relation {relation['id']} has no closed outer ring.")
    pieces = []
    for outer in outers:
        holes = unary_union([inner for inner in inners if outer.contains(inner)])
        pieces.extend(shapely.get_parts(outer.difference(holes)))
    return MultiPolygon(pieces)


def _rings(member_ways) -> list[Polygon]:
    lines = [_line(member["geometry"]) for member in member_ways]
    if not lines:
        return []
    # polygonize would cut an island outer ring out of the ring around it as a hole
    return [Polygon(face.exterior) for face in polygonize(linemerge(lines))]
