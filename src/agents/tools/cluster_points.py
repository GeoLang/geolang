from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class ClusterPointsArgs(BaseModel):
    input_path: str = Field(
        ...,
        description=(
            "Path to the point layer to cluster. "
            "A filename in outputs/ or user_data/, not a path."
        ),
    )
    method: str = Field(
        "dbscan",
        description=(
            "Clustering algorithm: 'dbscan' (density-based, auto-detects number of clusters, "
            "good for irregular shapes and noise) or 'kmeans' (requires n_clusters, "
            "good for roughly equal-sized spherical clusters). Default 'dbscan'."
        ),
    )
    n_clusters: Optional[int] = Field(
        None,
        description=(
            "Number of clusters for k-means. Required when method='kmeans'. "
            "Ignored for DBSCAN (which auto-detects cluster count)."
        ),
    )
    eps_km: float = Field(
        0.5,
        description=(
            "DBSCAN only: maximum distance in km between two points to be in the same cluster. "
            "Smaller = tighter clusters. Default 0.5km."
        ),
    )
    min_samples: int = Field(
        3,
        description=(
            "DBSCAN only: minimum number of points required to form a cluster. "
            "Points in groups smaller than this are labelled noise (-1). Default 3."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def cluster_points(
    input_path: str,
    method: str = "dbscan",
    n_clusters: int = None,
    eps_km: float = 0.5,
    min_samples: int = 3,
    output_filename: str = None,
) -> str:
    """
    Cluster a point layer using DBSCAN (density-based) or k-means.
    Adds a 'cluster_id' column to each point and saves a new GPKG.
    Also saves cluster hull polygons as a separate GPKG for visualisation.

    Common uses:
    - 'Find clusters of crime incidents'
    - 'Group customer locations into 5 zones'
    - 'Identify hotspot areas from event data'
    - 'Detect natural groupings in my point data'

    DBSCAN labels noise points as cluster -1 (shown separately).
    Call emit_ui_spec after with both the point and hull layers.
    """
    import os
    import traceback
    import re

    try:
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import MultiPoint

        full_path = tool_input_path("input_path", input_path)

        gdf = gpd.read_file(full_path)
        if gdf.empty:
            return f"Layer is empty: {input_path}"

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        gdf = gdf.to_crs("EPSG:4326")

        # Use centroids for non-point geometries
        if not gdf.geometry.geom_type.str.contains("Point").all():
            pts_geom = gdf.geometry.centroid
        else:
            pts_geom = gdf.geometry

        # Project to metric CRS for distance calculations
        centroid = pts_geom.unary_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
        metric_crs = f"EPSG:{utm_epsg}"

        pts_m = gpd.GeoDataFrame(geometry=pts_geom, crs="EPSG:4326").to_crs(metric_crs)
        coords = np.array([(g.x, g.y) for g in pts_m.geometry])

        method = method.lower().strip()

        if method == "kmeans":
            if not n_clusters or n_clusters < 2:
                return "n_clusters must be >= 2 for k-means."
            from sklearn.cluster import KMeans

            km = KMeans(n_clusters=int(n_clusters), random_state=42, n_init=10)
            labels = km.fit_predict(coords)
        else:
            # DBSCAN
            from sklearn.cluster import DBSCAN

            eps_m = eps_km * 1000
            db = DBSCAN(eps=eps_m, min_samples=int(min_samples), metric="euclidean")
            labels = db.fit_predict(coords)

        gdf = gdf.copy()
        gdf["cluster_id"] = labels.astype(int)

        n_clusters_found = int(len(set(labels)) - (1 if -1 in labels else 0))
        n_noise = int((labels == -1).sum())
        n_total = len(gdf)

        # Build convex hull polygons per cluster
        hull_rows = []
        for cid in sorted(set(labels)):
            if cid == -1:
                continue
            mask = labels == cid
            cluster_pts = pts_m.geometry[mask]
            if len(cluster_pts) < 3:
                hull_geom = MultiPoint(list(cluster_pts)).convex_hull
            else:
                hull_geom = MultiPoint(list(cluster_pts)).convex_hull
            # Transform hull back to WGS84
            hull_gdf_tmp = gpd.GeoDataFrame(
                geometry=[hull_geom], crs=metric_crs
            ).to_crs("EPSG:4326")
            size = int(mask.sum())
            hull_rows.append(
                {
                    "cluster_id": cid,
                    "point_count": size,
                    "geometry": hull_gdf_tmp.geometry.iloc[0],
                }
            )

        # Save point layer
        gdf.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in gdf.columns
        ]

        stem = os.path.splitext(os.path.basename(full_path))[0][:18]
        if not output_filename:
            output_filename = f"{stem}_{method}_clusters"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        pts_out = tool_output_path("output_filename", f"{output_filename}.gpkg")
        gdf.to_file(pts_out, driver="GPKG")

        # Save hull layer
        hull_filename = f"{output_filename}_hulls"
        hull_out = tool_output_path("output_filename", f"{hull_filename}.gpkg")
        if hull_rows:
            hull_gdf = gpd.GeoDataFrame(hull_rows, crs="EPSG:4326")
            hull_gdf.to_file(hull_out, driver="GPKG")
            hull_note = f" Cluster hulls saved to outputs/{hull_filename}.gpkg."
        else:
            hull_note = ""

        # Top 3 clusters by size
        if hull_rows:
            top = sorted(hull_rows, key=lambda r: r["point_count"], reverse=True)[:3]
            top_str = ", ".join(
                f"Cluster {r['cluster_id']} ({r['point_count']} pts)" for r in top
            )
            top_note = f" Largest: {top_str}."
        else:
            top_note = ""

        noise_note = f" {n_noise} noise points (cluster_id=-1)." if n_noise else ""

        return (
            f"{method.upper()} clustering complete: {n_clusters_found} clusters found "
            f"from {n_total} points.{noise_note}{top_note}{hull_note} "
            f"Points saved to outputs/{output_filename}.gpkg. "
            f"Each point carries its 'cluster_id' as an attribute."
        )

    except ImportError:
        return "cluster_points requires scikit-learn. Install with: pip install scikit-learn"
    except Exception as e:
        return f"cluster_points failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = cluster_points
TOOL_SCHEMA = ClusterPointsArgs
