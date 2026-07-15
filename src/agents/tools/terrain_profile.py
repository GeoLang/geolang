from pydantic import BaseModel, Field
from typing import Optional


class TerrainProfileArgs(BaseModel):
    start_place: str = Field(
        ...,
        description=(
            "Start point of the profile — place name or 'lat,lon' coordinates. "
            "E.g. 'Fort William, Scotland' or '56.82,-5.11'."
        ),
    )
    end_place: str = Field(
        ...,
        description=(
            "End point of the profile — place name or 'lat,lon' coordinates. "
            "E.g. 'Ben Nevis summit' or '56.797,-5.003'."
        ),
    )
    n_samples: int = Field(
        50,
        description=(
            "Number of elevation sample points along the transect. "
            "More samples = smoother profile but slower. Range 10–100. Default 50."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension for the PNG chart. Auto-generated if omitted.",
    )


def terrain_profile(
    start_place: str,
    end_place: str,
    n_samples: int = 50,
    output_filename: str = None,
) -> str:
    """
    Generate an elevation cross-section / terrain profile between two locations.
    Samples the SRTM 90m elevation grid at N points along the transect,
    then plots a filled area chart showing the terrain profile.

    Common uses:
    - 'Show the elevation profile from Fort William to Ben Nevis'
    - 'What is the terrain like between these two points?'
    - 'Draw a cross-section through the Alps'
    - 'Show me the terrain profile along this route'

    Returns a PNG chart image. Call emit_ui_spec after with ui_type='image'.
    """
    import os
    import traceback
    import re

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import requests
        import numpy as np
        import osmnx as ox
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker

        import re as _re2

        _cm1 = _re2.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", start_place.strip()
        )
        lat1, lon1 = (
            (float(_cm1.group(1)), float(_cm1.group(2)))
            if _cm1
            else ox.geocode(start_place)
        )
        _cm2 = _re2.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", end_place.strip()
        )
        lat2, lon2 = (
            (float(_cm2.group(1)), float(_cm2.group(2)))
            if _cm2
            else ox.geocode(end_place)
        )

        n_samples = max(10, min(100, int(n_samples)))

        # Build sample points along the great-circle transect
        lats = np.linspace(lat1, lat2, n_samples)
        lons = np.linspace(lon1, lon2, n_samples)

        # Query OpenTopoData in one batch (max 100 per request)
        locs = "|".join(f"{la:.6f},{lo:.6f}" for la, lo in zip(lats, lons))
        url = f"https://api.opentopodata.org/v1/srtm90m?locations={locs}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            return f"Elevation API error: {data.get('status', 'unknown')}"

        elevations = []
        for r in data.get("results", []):
            elev = r.get("elevation")
            elevations.append(float(elev) if elev is not None else 0.0)

        if not elevations:
            return "No elevation data returned for this transect."

        # Compute approximate distance along the transect
        import math as _math

        _dLat = _math.radians(lat2 - lat1)
        _dLon = _math.radians(lon2 - lon1)
        _a = (
            _math.sin(_dLat / 2) ** 2
            + _math.cos(_math.radians(lat1))
            * _math.cos(_math.radians(lat2))
            * _math.sin(_dLon / 2) ** 2
        )
        total_km = 6371.0 * 2 * _math.atan2(_math.sqrt(_a), _math.sqrt(1 - _a))
        distances = np.linspace(0, total_km, len(elevations))

        elev_arr = np.array(elevations)
        elev_min = float(elev_arr.min())
        elev_max = float(elev_arr.max())
        elev_mean = float(elev_arr.mean())

        # ── Plot ────────────────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(10, 4), facecolor="#0f1117")
        ax.set_facecolor("#1a1d2e")

        # Filled area under profile
        ax.fill_between(
            distances, elev_min - 10, elev_arr, color="#3b82f6", alpha=0.3, zorder=2
        )
        ax.plot(distances, elev_arr, color="#60a5fa", linewidth=1.8, zorder=3)

        # Start / end markers
        ax.scatter(
            [distances[0]],
            [elev_arr[0]],
            color="#10b981",
            s=60,
            zorder=5,
            label=f"Start: {start_place[:30]} ({elev_arr[0]:.0f}m)",
        )
        ax.scatter(
            [distances[-1]],
            [elev_arr[-1]],
            color="#f59e0b",
            s=60,
            zorder=5,
            label=f"End: {end_place[:30]} ({elev_arr[-1]:.0f}m)",
        )

        # Peak marker
        peak_idx = int(np.argmax(elev_arr))
        ax.scatter(
            [distances[peak_idx]],
            [elev_arr[peak_idx]],
            color="#ef4444",
            marker="^",
            s=80,
            zorder=6,
            label=f"Peak: {elev_max:.0f}m",
        )

        ax.set_ylim(max(0, elev_min - 50), elev_max + 80)
        ax.set_xlim(0, total_km)
        ax.set_xlabel("Distance (km)", color="#94a3b8", fontsize=9)
        ax.set_ylabel("Elevation (m)", color="#94a3b8", fontsize=9)
        ax.tick_params(colors="#475569", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#2d3148")

        title = f"Terrain Profile: {start_place[:25]} → {end_place[:25]}"
        ax.set_title(title, color="#e2e8f0", fontsize=11, fontweight="bold", pad=10)

        stats_text = (
            f"Total distance: {total_km:.1f}km  |  "
            f"Min: {elev_min:.0f}m  Max: {elev_max:.0f}m  Mean: {elev_mean:.0f}m  |  "
            f"Gain: {max(0, elev_arr[-1]-elev_arr[0]):.0f}m  "
            f"Loss: {max(0, elev_arr[0]-elev_arr[-1]):.0f}m"
        )
        ax.text(
            0.5,
            -0.18,
            stats_text,
            transform=ax.transAxes,
            ha="center",
            fontsize=7.5,
            color="#64748b",
        )

        ax.legend(
            loc="upper right",
            fontsize=7.5,
            facecolor="#1a1d2e",
            edgecolor="#2d3148",
            labelcolor="#94a3b8",
        )
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.0f}m"))
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:.1f}km"))

        ax.grid(True, color="#2d3148", linewidth=0.5, alpha=0.7)
        plt.tight_layout(rect=[0, 0.05, 1, 1])

        # Save
        if not output_filename:
            safe_start = re.sub(r"[^\w]", "_", start_place.lower())[:12].strip("_")
            safe_end = re.sub(r"[^\w]", "_", end_place.lower())[:12].strip("_")
            output_filename = f"profile_{safe_start}_to_{safe_end}"
        if output_filename.lower().endswith(".png"):
            output_filename = output_filename[:-4]

        out_path = os.path.join(outputs_dir, f"{output_filename}.png")
        fig.savefig(
            out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
        )
        plt.close(fig)

        # Also save sample points as GPKG for reference
        try:
            import geopandas as gpd
            from shapely.geometry import Point, LineString

            pts_gdf = gpd.GeoDataFrame(
                [
                    {
                        "distance_km": round(float(d), 3),
                        "elevation_m": round(float(e), 1),
                        "geometry": Point(lo, la),
                    }
                    for d, e, la, lo in zip(distances, elevations, lats, lons)
                ],
                crs="EPSG:4326",
            )
            line_gdf = gpd.GeoDataFrame(
                [
                    {
                        "from": start_place,
                        "to": end_place,
                        "distance_km": round(total_km, 2),
                        "elev_min_m": round(elev_min, 1),
                        "elev_max_m": round(elev_max, 1),
                        "geometry": LineString(zip(lons, lats)),
                    }
                ],
                crs="EPSG:4326",
            )
            pts_gdf.to_file(out_path.replace(".png", "_points.gpkg"), driver="GPKG")
            line_gdf.to_file(out_path.replace(".png", "_line.gpkg"), driver="GPKG")
        except Exception:
            pass

        return (
            f"Terrain profile from '{start_place}' to '{end_place}': "
            f"{total_km:.1f}km transect, {n_samples} samples. "
            f"Elevation — min: {elev_min:.0f}m, max: {elev_max:.0f}m, mean: {elev_mean:.0f}m. "
            f"Saved to outputs/{output_filename}.png. "
            f"Call emit_ui_spec with ui_type='image', image_path='outputs/{output_filename}.png'."
        )

    except Exception as e:
        return f"terrain_profile failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = terrain_profile
TOOL_SCHEMA = TerrainProfileArgs
