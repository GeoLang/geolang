from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import caller_outputs_dir


class DownloadOSMDataArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Place to download data for. Use a neighbourhood or city for best results. "
            "For street addresses a 2km radius search is used automatically. "
            "Examples: 'Marylebone, London', 'Islington, London', 'Manhattan, New York'."
        ),
    )
    data_type: str = Field(
        ...,
        description=(
            "What to download. Common values: buildings, roads, schools, hospitals, "
            "parks, restaurants, shops, supermarkets, pharmacies, amenities, parking, "
            "bus_stops. Or use OSM tag syntax like 'amenity=cafe' or 'shop=bakery'."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def download_osm_data(
    place_name: str,
    data_type: str,
    output_filename: str = None,
) -> str:
    """
    Download OpenStreetMap data for any place and save as GeoPackage.
    Returns the output path and feature count. Use this whenever the user asks
    for real-world data like buildings, roads, amenities, parks, or shops.
    """
    import os
    import traceback

    outputs_dir = caller_outputs_dir()

    try:
        import osmnx as ox

        OSM_TAG_MAP = {
            "buildings": {"building": True},
            "residential": {"building": "residential"},
            "commercial": {"building": "commercial"},
            "industrial": {"building": "industrial"},
            "schools": {"amenity": "school"},
            "hospitals": {"amenity": "hospital"},
            "pharmacies": {"amenity": "pharmacy"},
            "clinics": {"amenity": "clinic"},
            "parks": {"leisure": "park"},
            "green_spaces": {"landuse": "grass"},
            "restaurants": {"amenity": "restaurant"},
            "cafes": {"amenity": "cafe"},
            "bars": {"amenity": "bar"},
            "shops": {"shop": True},
            "supermarkets": {"shop": "supermarket"},
            "offices": {"office": True},
            "parking": {"amenity": "parking"},
            "bus_stops": {"highway": "bus_stop"},
            "transit": {"public_transport": True},
            "amenities": {"amenity": True},
            "water": {"natural": "water"},
            "forests": {"landuse": "forest"},
        }

        # Resolve tags
        dt = data_type.lower().strip()
        if "=" in dt:
            key, val = dt.split("=", 1)
            tags = {key.strip(): val.strip()}
        elif dt in OSM_TAG_MAP:
            tags = OSM_TAG_MAP[dt]
        else:
            # Try as an amenity value (e.g. "cafe", "gym")
            tags = {"amenity": dt}

        # If place_name looks like "lat,lon" coordinates, use point-based download
        import re as _re

        _coord_m = _re.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
        )
        if _coord_m:
            _lat, _lon = float(_coord_m.group(1)), float(_coord_m.group(2))
            place_name = f"{_lat},{_lon}"  # normalise for output filename
            # Skip place-boundary attempt, go straight to point+radius
            if dt == "roads":
                G = ox.graph_from_point((_lat, _lon), dist=2000, network_type="all")
                gdf = ox.graph_to_gdfs(G, nodes=False).reset_index(drop=True)
            else:
                gdf = ox.features_from_point((_lat, _lon), tags=tags, dist=1000)
                if gdf.empty:
                    return (
                        f"No '{data_type}' features found within 1km of {place_name}."
                    )
                gdf = gdf.reset_index(drop=True)
            # Jump to post-processing
            _coord_download = True
        else:
            _coord_download = False

        # Build output path
        if not output_filename:
            safe_place = (
                place_name.lower().replace(" ", "_").replace(",", "")[:20].strip("_")
            )
            safe_type = dt.replace("=", "_").replace(" ", "_")[:15]
            output_filename = f"{safe_place}_{safe_type}"
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")

        # Download — try place boundary first, fall back to point+radius for addresses
        if not _coord_download:
            if dt == "roads":
                try:
                    G = ox.graph_from_place(place_name, network_type="all")
                except Exception:
                    lat, lon = ox.geocode(place_name)
                    G = ox.graph_from_point((lat, lon), dist=2000, network_type="all")
                gdf = ox.graph_to_gdfs(G, nodes=False).reset_index(drop=True)
            else:
                try:
                    gdf = ox.features_from_place(place_name, tags=tags)
                except Exception:
                    lat, lon = ox.geocode(place_name)
                    gdf = ox.features_from_point((lat, lon), tags=tags, dist=2000)
                if gdf.empty:
                    return f"No '{data_type}' features found in {place_name}."
                gdf = gdf.reset_index(drop=True)

        if gdf.empty:
            return f"No '{data_type}' features found in {place_name}."

        # Drop columns with unhashable/complex types that break GPKG serialisation
        drop_cols = []
        for col in gdf.columns:
            if col == "geometry":
                continue
            sample = gdf[col].dropna()
            if len(sample) > 0 and isinstance(sample.iloc[0], (list, dict)):
                drop_cols.append(col)
        if drop_cols:
            gdf = gdf.drop(columns=drop_cols)

        # Coerce remaining object columns to string
        for col in gdf.select_dtypes(include="object").columns:
            if col != "geometry":
                gdf[col] = gdf[col].astype(str)

        # Sanitise column names — GPKG/fiona breaks on special chars, reserved words,
        # very long names, or OSM tags like "FIXME", "addr:street", "name:en"
        import re

        rename_map = {}
        seen = set()
        for col in gdf.columns:
            if col == "geometry":
                continue
            clean = re.sub(r"[^a-zA-Z0-9_]", "_", col)  # replace non-alphanumeric
            clean = re.sub(r"_+", "_", clean).strip("_")  # collapse underscores
            clean = clean[:60]  # GPKG max column name length
            if not clean or clean[0].isdigit():
                clean = "col_" + clean
            # Ensure uniqueness
            base, i = clean, 1
            while clean in seen:
                clean = f"{base}_{i}"
                i += 1
            seen.add(clean)
            if clean != col:
                rename_map[col] = clean
        if rename_map:
            gdf = gdf.rename(columns=rename_map)

        # Ensure WGS84
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # Save — if mixed geometry types, keep only the dominant type
        geom_counts = gdf.geometry.geom_type.value_counts()
        if geom_counts.empty:
            return f"Downloaded data for {place_name} but no valid geometries found."

        dominant = geom_counts.index[0]
        if gdf.geometry.geom_type.nunique() > 1:
            gdf = gdf[gdf.geometry.geom_type == dominant].copy()

        try:
            gdf.to_file(output_path, driver="GPKG")
        except Exception:
            # Last resort: keep only name, geometry and a handful of safe columns
            keep = ["geometry"] + [
                c
                for c in ("name", "amenity", "shop", "building", "leisure")
                if c in gdf.columns
            ]
            gdf[keep].to_file(output_path, driver="GPKG")

        useful_cols = [c for c in gdf.columns if c not in ("geometry",)][:8]
        return (
            f"Downloaded {len(gdf)} {data_type} features ({dominant}) for {place_name}. "
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Key columns: {', '.join(useful_cols)}"
        )

    except Exception as e:
        return f"OSM download failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = download_osm_data
TOOL_SCHEMA = DownloadOSMDataArgs
