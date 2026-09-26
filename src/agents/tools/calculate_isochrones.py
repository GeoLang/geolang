from pydantic import BaseModel, Field
from typing import Optional
from src.core.place_lookup import geocode_point, place_not_found
from src.core.utils import tool_output_path

from ._isochrones import isochrone_polygons


class CalculateIsochronesArgs(BaseModel):
    place_name: str = Field(
        ...,
        description="Place or address to start from, e.g. 'Canary Wharf, London'.",
    )
    travel_mode: str = Field(
        "walking",
        description="'walking', 'cycling', or 'driving'.",
    )
    time_minutes: str = Field(
        "5,10,15",
        description="Comma-separated time thresholds in minutes, e.g. '5,10,15'.",
    )
    road_detail: str = Field(
        "auto",
        description=(
            "How much of the road network to download, by mode and time:\n"
            "  'full'      all roads including residential, for walking, cycling "
            "or drives up to 15 min\n"
            "  'major'     motorway, trunk, primary, secondary, for drives of 15 to "
            "60 min and for logistics\n"
            "  'motorway'  motorway and trunk only, for drives over 60 min\n"
            "  'auto'      decided from travel_mode and time_minutes, safe when unsure"
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def calculate_isochrones(
    place_name: str,
    travel_mode: str = "walking",
    time_minutes: str = "5,10,15",
    road_detail: str = "auto",
    output_filename: str = None,
) -> str:
    """
    Walk, cycle, or drive time isochrones (catchment areas) around a location,
    over the platform's itinera engine where the loaded OSM extract covers the
    point, and OpenStreetMap otherwise: Valhalla for driving, a downloaded road
    network for walking and cycling. Returns one polygon per time threshold.
    """
    import traceback


    try:
        import geopandas as gpd

        # Parse times
        times = sorted(set(int(t.strip()) for t in time_minutes.split(",")))
        if not times:
            return "No valid time values provided."
        if max(times) > 60 and travel_mode.lower() == "walking":
            return "Walking isochrones over 60 minutes cover too large an area. Use driving or reduce the time."

        driving = travel_mode.lower() == "driving"

        location = geocode_point(place_name)
        if location is None:
            return place_not_found(place_name)
        lat, lon = location

        if not output_filename:
            safe_place = (
                place_name.lower().replace(" ", "_").replace(",", "")[:18].strip("_")
            )
            output_filename = f"{safe_place}_{travel_mode[:4]}_isochrones"

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")

        features = isochrone_polygons(lat, lon, travel_mode, times, road_detail)
        if isinstance(features, str):
            return features
        if not features:
            if driving:
                return f"Could not compute driving isochrones for {place_name} via Valhalla API."
            return f"Could not compute isochrones for {place_name}."

        for feature in features:
            feature["place"] = place_name

        gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
        gdf.to_file(output_path, driver="GPKG")

        time_str = ", ".join(str(t) for t in sorted(times))
        saved = (
            f"{len(gdf)} zones saved to outputs/{output_filename}.gpkg. "
            f"Center: lon={lon:.4f}, lat={lat:.4f}"
        )
        detail = features[0]["road_detail"]
        if detail == "itinera":
            return (
                f"Computed {travel_mode} isochrones ({time_str} min) around {place_name} "
                f"using itinera. {saved}"
            )
        if driving:
            return (
                f"Computed driving isochrones ({time_str} min) around {place_name} "
                f"using Valhalla routing. {saved}"
            )
        return (
            f"Computed {travel_mode} isochrones ({time_str} min) around {place_name} "
            f"using road_detail='{detail}'. {saved}"
        )

    except Exception as e:
        return f"Isochrone calculation failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = calculate_isochrones
TOOL_SCHEMA = CalculateIsochronesArgs
TOOL_SUPERSEDED_BY = ("analysis.travel_time",)
