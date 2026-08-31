from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class BatchGeocodeArgs(BaseModel):
    addresses: Optional[str] = Field(
        None,
        description=(
            "Addresses or place names separated by semicolons, e.g. "
            "'10 Downing Street, London; Tower Bridge, London'. Use this when the "
            "user pastes a list."
        ),
    )
    input_csv_path: Optional[str] = Field(
        None,
        description=(
            "CSV holding the addresses, a filename in user_data/ or outputs/, not a "
            "path. Columns named address, location, place, name or site are found "
            "on their own."
        ),
    )
    address_column: Optional[str] = Field(
        None,
        description="Column holding the addresses, when it is not found on its own.",
    )
    label_column: Optional[str] = Field(
        None,
        description="Column labelling each output point. Defaults to the address.",
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def batch_geocode(
    addresses: str = None,
    input_csv_path: str = None,
    address_column: str = None,
    label_column: str = None,
    output_filename: str = None,
) -> str:
    """
    Geocode a list of addresses or place names and save them as a point GPKG,
    ready to map or to feed a spatial tool. Takes either a semicolon-separated
    list or a CSV. Geocodes through Nominatim at one address per second, so a
    long list is slow.
    """
    import time
    import traceback

    try:
        import requests
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point

        records = []  # [{label, address}]

        if addresses:
            for addr in addresses.split(";"):
                addr = addr.strip()
                if addr:
                    records.append({"label": addr, "address": addr})

        elif input_csv_path:
            csv_full = tool_input_path("input_csv_path", input_csv_path)
            df = pd.read_csv(csv_full)
            # Auto-detect address column
            addr_col = address_column
            if not addr_col:
                for candidate in (
                    "address",
                    "location",
                    "place",
                    "name",
                    "site",
                    "Address",
                    "Location",
                ):
                    if candidate in df.columns:
                        addr_col = candidate
                        break
            if not addr_col:
                return (
                    f"Could not find an address column in the CSV. "
                    f"Columns found: {list(df.columns)}. "
                    f"Provide address_column explicitly."
                )
            lbl_col = (
                label_column
                if label_column and label_column in df.columns
                else addr_col
            )
            for _, row in df.iterrows():
                addr = str(row[addr_col]).strip()
                label = str(row[lbl_col]).strip()
                if addr and addr.lower() not in ("nan", "none", ""):
                    records.append({"label": label, "address": addr})
                    # Carry over all other columns
                    extra = {
                        c: row[c] for c in df.columns if c not in (addr_col, lbl_col)
                    }
                    records[-1].update(extra)

        else:
            return (
                "Provide either 'addresses' (semicolon-separated) or 'input_csv_path'."
            )

        if not records:
            return "No addresses to geocode."

        # Geocode via Nominatim
        NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
        HEADERS = {"User-Agent": "geolang-gis-agent/1.0"}

        results = []
        failed = []

        for rec in records:
            addr = rec["address"]
            try:
                resp = requests.get(
                    NOMINATIM_URL,
                    params={"q": addr, "format": "json", "limit": 1},
                    headers=HEADERS,
                    timeout=10,
                )
                data = resp.json()
                if data:
                    hit = data[0]
                    lat = float(hit["lat"])
                    lon = float(hit["lon"])
                    display = hit.get("display_name", addr)
                    row = dict(rec)
                    row["geocoded_name"] = display
                    row["geometry"] = Point(lon, lat)
                    results.append(row)
                else:
                    failed.append(addr)
            except Exception:
                failed.append(addr)
            time.sleep(1.1)  # Nominatim fair-use: max 1 req/sec

        if not results:
            return (
                f"Geocoding failed for all {len(records)} addresses. "
                f"Check that addresses are valid and include enough context (city, country)."
            )

        gdf = gpd.GeoDataFrame(results, crs="EPSG:4326")
        # Drop address column to avoid duplication (already in label)
        gdf = gdf.drop(columns=["address"], errors="ignore")

        # Sanitise column names
        import re

        gdf.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in gdf.columns
        ]

        if not output_filename:
            output_filename = "batch_geocoded"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
        gdf.to_file(output_path, driver="GPKG")

        fail_note = (
            f" Failed to geocode {len(failed)}: {'; '.join(failed[:5])}{'...' if len(failed) > 5 else ''}."
            if failed
            else ""
        )
        return (
            f"Geocoded {len(results)} of {len(records)} addresses successfully.{fail_note} "
            f"Saved to outputs/{output_filename}.gpkg."
        )

    except Exception as e:
        return f"Batch geocoding failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = batch_geocode
TOOL_SCHEMA = BatchGeocodeArgs
