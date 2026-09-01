from pydantic import BaseModel, Field
from src.core.utils import natural_earth_dataset_paths


class GeocodePlaceArgs(BaseModel):
    place_name: str = Field(
        ...,
        description="City, town, address or landmark to look up, e.g. 'Tokyo'.",
    )


def geocode_place(place_name: str) -> str:
    """
    Look up a place name or an address anywhere in the world, returning
    longitude, latitude and country. Tries the platform's geokode service, then
    Natural Earth populated places, then Nominatim for landmarks and anything
    else. Call this for a town, an address or a landmark the user names. A
    feature on the user's map is found with viewer_control run find_feature
    instead, not here.
    """
    import os
    import traceback

    # platform geokode first: authoritative for addresses in the loaded extract
    geokode_url = os.environ.get("GEOKODE_URL")
    if geokode_url:
        try:
            import requests

            resp = requests.get(
                f"{geokode_url.rstrip('/')}/forward",
                params={"q": place_name},
                timeout=10,
            )
            if resp.ok:
                results = resp.json().get("results", [])
                if results:
                    top = results[0]
                    addr = top.get("address", {})
                    label = addr.get("full") or ", ".join(
                        str(p)
                        for p in (
                            addr.get("street"),
                            addr.get("city"),
                            addr.get("state"),
                        )
                        if p
                    ) or place_name
                    lon = round(float(top["lon"]), 5)
                    lat = round(float(top["lat"]), 5)
                    return f"✅ {label} (geokode): lon={lon}, lat={lat}"
        except Exception:
            pass  # geokode unavailable or no match: fall back to Natural Earth

    # Search across available Natural Earth populated places datasets
    search_paths = natural_earth_dataset_paths("populated_places")

    try:
        import geopandas as gpd

        gdf = None
        for path in search_paths:
            if os.path.exists(path):
                gdf = gpd.read_file(path)
                break

        if gdf is None:
            return (
                "❌ No populated places dataset found. "
                "Run download_natural_earth_dataset first."
            )

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
            return _nominatim_fallback(place_name)

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


def _nominatim_fallback(place_name: str) -> str:
    # landmarks ("Eiffel Tower") are in neither geokode's address extract nor
    # Natural Earth's populated places, so ask Nominatim before giving up
    try:
        import requests

        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place_name, "format": "json", "limit": 1},
            headers={"User-Agent": "geolang-gis-agent/1.0"},
            timeout=10,
        )
        data = resp.json()
        if data:
            hit = data[0]
            lon = round(float(hit["lon"]), 5)
            lat = round(float(hit["lat"]), 5)
            label = hit.get("display_name", place_name)
            return f"✅ {label} (nominatim): lon={lon}, lat={lat}"
    except Exception:
        pass
    return (
        f"❌ Place '{place_name}' not found in any geocoding source. "
        "Tell the user geocoding failed. Do not answer with coordinates "
        "from memory."
    )


TOOL_FUNCTION = geocode_place
TOOL_SCHEMA = GeocodePlaceArgs
