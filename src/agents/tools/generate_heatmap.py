from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class GenerateHeatmapArgs(BaseModel):
    input_path: str = Field(
        ...,
        description=(
            "Point GPKG to build the density from. A filename in outputs/ or "
            "user_data/, not a path."
        ),
    )
    place_name: str = Field(
        ...,
        description="Readable label for the map title and output filename.",
    )
    value_column: Optional[str] = Field(
        None,
        description=(
            "Numeric column to weight the density by, e.g. 'population'. Each point "
            "counts equally if omitted."
        ),
    )
    bandwidth_km: Optional[float] = Field(
        None,
        description=(
            "Kernel bandwidth in km, larger is smoother. Chosen from the data "
            "extent if omitted."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output PNG name, no extension. Auto-generated if omitted.",
    )


def generate_heatmap(
    input_path: str,
    place_name: str,
    value_column: str = None,
    bandwidth_km: float = None,
    output_filename: str = None,
) -> str:
    """
    Kernel density heatmap image from a point layer, showing where features are
    concentrated. Use it when the user asks where something is densest, for
    hotspots, or for a heatmap.
    Returns a PNG path. Call emit_ui_spec after with ui_type='image'.
    """
    import traceback

    try:
        import numpy as np
        import geopandas as gpd
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from scipy.stats import gaussian_kde

        input_full = tool_input_path("input_path", input_path)

        gdf = gpd.read_file(input_full)
        if gdf.empty:
            return f"Layer is empty: {input_path}"

        # Keep only point geometries
        gdf = gdf[gdf.geometry.geom_type.isin(["Point", "MultiPoint"])].copy()
        if gdf.empty:
            return f"No point features found in '{input_path}'. Heatmap requires point geometry."

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")

        # Project to metric CRS for KDE
        gdf_proj = gdf.to_crs("EPSG:3857")

        xs = gdf_proj.geometry.x.values
        ys = gdf_proj.geometry.y.values

        # Optional weights
        weights = None
        if value_column and value_column in gdf.columns:
            w = gdf[value_column].fillna(0).astype(float).values
            if w.sum() > 0:
                weights = w / w.sum()

        # Auto bandwidth: Scott's rule scaled to ~1km minimum
        if bandwidth_km:
            bw_m = bandwidth_km * 1000.0
            # gaussian_kde bw_method = bandwidth / std_dev
            bw_factor = bw_m / np.std(xs) if np.std(xs) > 0 else 0.1
        else:
            bw_factor = "scott"

        # KDE
        xy = np.vstack([xs, ys])
        try:
            kde = gaussian_kde(xy, bw_method=bw_factor, weights=weights)
        except Exception:
            kde = gaussian_kde(xy, bw_method="scott", weights=weights)

        # Build evaluation grid
        pad = (xs.max() - xs.min()) * 0.08 or 1000
        x_min, x_max = xs.min() - pad, xs.max() + pad
        y_min, y_max = ys.min() - pad, ys.max() + pad
        grid_size = 300
        xi = np.linspace(x_min, x_max, grid_size)
        yi = np.linspace(y_min, y_max, grid_size)
        Xi, Yi = np.meshgrid(xi, yi)
        positions = np.vstack([Xi.ravel(), Yi.ravel()])
        Z = kde(positions).reshape(grid_size, grid_size)

        # Plot
        fig, ax = plt.subplots(1, 1, figsize=(10, 8))
        fig.patch.set_facecolor("#0f1117")
        ax.set_facecolor("#0f1117")

        # Background basemap using Natural Earth bounds converted back to lon/lat
        # (just a dark background — no tile fetching needed)
        im = ax.imshow(
            Z,
            origin="lower",
            extent=[x_min, x_max, y_min, y_max],
            cmap="YlOrRd",
            alpha=0.85,
            aspect="auto",
        )

        # Scatter the actual points underneath
        ax.scatter(xs, ys, s=4, c="#ffffff", alpha=0.3, zorder=5, linewidths=0)

        cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
        cb.set_label("Density", color="#94a3b8", fontsize=9)
        cb.ax.yaxis.set_tick_params(color="#94a3b8")
        plt.setp(cb.ax.yaxis.get_ticklabels(), color="#94a3b8")

        weight_note = (
            f" (weighted by {value_column})"
            if value_column and weights is not None
            else ""
        )
        ax.set_title(
            f"Density Heatmap — {place_name}{weight_note}",
            color="#e2e8f0",
            fontsize=11,
            pad=10,
        )
        ax.set_xlabel("Easting (m)", color="#475569", fontsize=8)
        ax.set_ylabel("Northing (m)", color="#475569", fontsize=8)
        ax.tick_params(colors="#475569", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#2d3148")

        plt.tight_layout()

        if not output_filename:
            import re

            safe = re.sub(r"[^\w]", "_", place_name.lower())[:18].strip("_")
            output_filename = f"{safe}_heatmap"
        if output_filename.lower().endswith(".png"):
            output_filename = output_filename[:-4]

        output_path = tool_output_path(
            "output_filename", f"{output_filename}.png"
        )
        fig.savefig(
            output_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
        )
        plt.close(fig)

        return (
            f"Heatmap generated for {len(gdf)} points in '{place_name}'. "
            f"Saved to outputs/{output_filename}.png. "
            f"Call emit_ui_spec with ui_type='image', image_path='outputs/{output_filename}.png'."
        )

    except Exception as e:
        return f"Heatmap generation failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = generate_heatmap
TOOL_SCHEMA = GenerateHeatmapArgs
