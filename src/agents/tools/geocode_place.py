from pydantic import BaseModel, Field
from src.core.place_lookup import GeocoderUnavailable, geocode
from src.core.utils import natural_earth_dataset_paths


class GeocodePlaceArgs(BaseModel):
    place_name: str = Field(
        ...,
        description="City, town, address or landmark to look up, e.g. 'Tokyo'.",
    )


def geocode_place(place_name: str) -> str:
    """
    Look up a place name or an address anywhere in the world, returning
    longitude, latitude and country. Tries the platform geocoder, then Natural
    Earth populated places. Call this for a town, an address or a landmark the
    user names. A feature on the user's map is found with viewer_control run
    find_feature instead, not here.
    """
    import os
    import traceback

    try:
        try:
            results = geocode(place_name)
        except GeocoderUnavailable:
            results = []
        hit = _geokode_answer(results, place_name)
        if hit is not None:
            return _format_geokode(hit, place_name)
        # held back so the place sources answer a query with no house number first
        geokode_street = _format_geokode(results[0], place_name) if results else None

        import geopandas as gpd

        search_paths = natural_earth_dataset_paths("populated_places")

        gdf = None
        for path in search_paths:
            if os.path.exists(path):
                gdf = gpd.read_file(path)
                break

        if gdf is None:
            return _not_found(place_name, geokode_street)

        # Try exact match on NAME, then case-insensitive, then partial
        name_col = next(
            (c for c in gdf.columns if c.upper() in ("NAME", "NAME_EN")), None
        )
        if name_col is None:
            return f"❌ Could not find name column. Columns: {list(gdf.columns)}"

        query = place_name.strip()
        match = gdf[gdf[name_col].str.upper() == query.upper()]
        if match.empty:
            match = gdf[gdf[name_col].str.contains(query, case=False, na=False)]

        if match.empty:
            return _not_found(place_name, geokode_street)

        # Use the most populous match if there are multiple
        pop_col = next((c for c in match.columns if "POP" in c.upper()), None)
        if pop_col and len(match) > 1:
            match = match.nlargest(1, pop_col)
        else:
            match = match.iloc[[0]]

        row = match.iloc[0]
        lon = round(row.geometry.x, 4)
        lat = round(row.geometry.y, 4)
        country = row.get("SOV0NAME", row.get("ADM0NAME", "Unknown"))
        name = row[name_col]

        return f"✅ {name}, {country}: lon={lon}, lat={lat}"

    except Exception as e:
        return f"❌ Geocoding failed: {str(e)}\n{traceback.format_exc()}"


def _geokode_answer(results: list, place_name: str) -> dict | None:
    if not results:
        return None
    if place_name.strip()[:1].isdigit():
        return results[0]
    return next(
        (
            r
            for r in results
            if r.get("kind") == "place" or r.get("match_type") == "exact"
        ),
        None,
    )


def _format_geokode(hit: dict, place_name: str) -> str:
    addr = hit.get("address", {})
    label = (
        addr.get("full")
        or ", ".join(
            str(p)
            for p in (addr.get("street"), addr.get("city"), addr.get("state"))
            if p
        )
        or place_name
    )
    lon = round(float(hit["lon"]), 5)
    lat = round(float(hit["lat"]), 5)
    return f"✅ {label} (geokode): lon={lon}, lat={lat}"


def _not_found(place_name: str, geokode_street: str | None) -> str:
    return geokode_street or (
        f"❌ Place '{place_name}' not found in any geocoding source. "
        "Tell the user geocoding failed. Do not answer with coordinates "
        "from memory."
    )


TOOL_FUNCTION = geocode_place
TOOL_SCHEMA = GeocodePlaceArgs
