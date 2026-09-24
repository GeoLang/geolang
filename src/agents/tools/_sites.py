import re

from src.core.place_lookup import GeocoderUnavailable, geocode_batch
from src.core.utils import tool_input_path

COORDINATE_PAIR = re.compile(r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$")

SITE_NAME_COLUMNS = ("name", "NAME", "Name", "site", "label", "address")


def resolve_sites(
    sites: str | None, sites_path: str | None, name_column: str | None = None
) -> list[dict] | str:
    if bool(sites) == bool(sites_path):
        return (
            "Give either sites (semicolon-separated names or 'lat, lon' pairs) or "
            "sites_path (a layer filename), not both and not neither."
        )
    if sites_path:
        return _sites_from_layer(sites_path, name_column)
    return _sites_from_names(sites)


def _sites_from_names(sites: str) -> list[dict] | str:
    names = [part.strip() for part in sites.split(";") if part.strip()]
    coordinates = {name: COORDINATE_PAIR.match(name) for name in names}
    place_names = [name for name in names if not coordinates[name]]
    try:
        answers = dict(zip(place_names, geocode_batch(place_names), strict=True))
    except GeocoderUnavailable as error:
        return f"Could not geocode the sites: {error}"

    resolved = []
    for name in names:
        pair = coordinates[name]
        if pair:
            resolved.append(
                {"name": name, "lat": float(pair.group(1)), "lon": float(pair.group(2))}
            )
            continue
        hits = answers[name]
        if not hits:
            return f"Could not geocode '{name}': the platform geocoder found no match."
        resolved.append({"name": name, "lat": hits[0]["lat"], "lon": hits[0]["lon"]})
    return resolved


def _sites_from_layer(sites_path: str, name_column: str | None) -> list[dict] | str:
    import geopandas as gpd

    frame = gpd.read_file(tool_input_path("sites_path", sites_path))
    if frame.empty:
        return f"Sites layer is empty: {sites_path}"
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    elif frame.crs.to_epsg() != 4326:
        frame = frame.to_crs("EPSG:4326")

    columns = [column for column in frame.columns if column != "geometry"]
    if name_column and name_column not in frame.columns:
        return (
            f"sites_path has no column '{name_column}'. "
            f"It carries: {', '.join(columns) or 'no attribute columns'}."
        )
    chosen = name_column or next(
        (column for column in SITE_NAME_COLUMNS if column in frame.columns), None
    )

    resolved = []
    for position, (_, row) in enumerate(frame.iterrows(), start=1):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        point = geometry if geometry.geom_type == "Point" else geometry.centroid
        name = str(row[chosen]) if chosen else f"site {position}"
        resolved.append({"name": name, "lat": float(point.y), "lon": float(point.x)})
    if not resolved:
        return f"No site has a geometry in {sites_path}."
    return resolved
