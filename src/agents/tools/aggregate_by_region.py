from pydantic import BaseModel, Field
from typing import Optional


class AggregateByRegionArgs(BaseModel):
    regions_path: str = Field(
        ...,
        description=(
            "Path to the polygon layer defining the regions to aggregate by — "
            "e.g. districts, boroughs, counties, isochrone zones. "
            "Relative to outputs/ or user_data/ or absolute."
        ),
    )
    features_path: str = Field(
        ...,
        description=(
            "Path to the layer whose values you want to aggregate — "
            "e.g. population points, schools, shops, crime incidents. "
            "Relative to outputs/ or user_data/ or absolute."
        ),
    )
    agg_columns: Optional[str] = Field(
        None,
        description=(
            "Comma-separated list of numeric columns from features_path to aggregate. "
            "E.g. 'population,income'. If omitted, feature COUNT per region is returned."
        ),
    )
    agg_func: str = Field(
        "sum",
        description=(
            "Aggregation function: 'sum', 'mean', 'count', 'max', 'min'. Default 'sum'. "
            "Use 'count' when you just want the number of features per region."
        ),
    )
    region_label_col: Optional[str] = Field(
        None,
        description=(
            "Column in regions_path to use as the region label in results — "
            "e.g. 'name', 'NAME', 'district'. Auto-detected if omitted."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def aggregate_by_region(
    regions_path: str,
    features_path: str,
    agg_columns: str = None,
    agg_func: str = "sum",
    region_label_col: str = None,
    output_filename: str = None,
) -> str:
    """
    Aggregate point or polygon features by region — count, sum, or average a
    numeric column (e.g. population, sales, incidents) per administrative area.

    Common uses:
    - 'Total population by district'
    - 'Count of hospitals per county'
    - 'Average house price by borough'
    - 'Number of schools in each catchment zone'
    - 'Sum of crime incidents by neighbourhood'

    Returns a polygon GPKG with aggregated values per region. Call emit_ui_spec
    after with ui_type='map' to draw it. The aggregated column is an attribute on
    each region, which the Feature Picker panel shows on click.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    user_data_dir = os.path.join(exec_dir, "user_data")
    os.makedirs(outputs_dir, exist_ok=True)

    _res = lambda p: (
        None
        if not p
        else (
            p
            if os.path.isabs(p) and os.path.exists(p)
            else next(
                (
                    c
                    for _b in (outputs_dir, user_data_dir, exec_dir)
                    for _n in (
                        [p] + ([] if p.lower().endswith(".gpkg") else [p + ".gpkg"])
                    )
                    for c in [os.path.join(_b, _n)]
                    if os.path.exists(c)
                ),
                None,
            )
        )
    )

    try:
        import geopandas as gpd
        import pandas as pd
        import numpy as np
        import re

        reg_full = _res(regions_path)
        if not reg_full:
            return f"Regions file not found: '{regions_path}'."
        feat_full = _res(features_path)
        if not feat_full:
            return f"Features file not found: '{features_path}'."

        gdf_reg = gpd.read_file(reg_full)
        gdf_feat = gpd.read_file(feat_full)

        if gdf_reg.empty:
            return f"Regions layer is empty: {regions_path}"
        if gdf_feat.empty:
            return f"Features layer is empty: {features_path}"

        if gdf_reg.crs is None:
            gdf_reg = gdf_reg.set_crs("EPSG:4326")
        if gdf_feat.crs is None:
            gdf_feat = gdf_feat.set_crs("EPSG:4326")
        if gdf_feat.crs != gdf_reg.crs:
            gdf_feat = gdf_feat.to_crs(gdf_reg.crs)

        # Auto-detect region label column
        if not region_label_col:
            for candidate in (
                "name",
                "NAME",
                "Name",
                "district",
                "borough",
                "region",
                "label",
                "admin_name",
                "ADM1",
                "NUTS_NAME",
            ):
                if candidate in gdf_reg.columns:
                    region_label_col = candidate
                    break
        if not region_label_col:
            # Fall back to first string column
            str_cols = gdf_reg.select_dtypes(include="object").columns.tolist()
            region_label_col = str_cols[0] if str_cols else None

        # Use centroid for non-point feature geometries for the join
        gdf_feat_join = gdf_feat.copy()
        if not gdf_feat_join.geometry.geom_type.str.contains("Point").all():
            gdf_feat_join = gdf_feat_join.copy()
            gdf_feat_join["geometry"] = gdf_feat_join.geometry.centroid

        # Spatial join: tag each feature with its region
        joined = gpd.sjoin(
            gdf_feat_join,
            gdf_reg[["geometry"] + ([region_label_col] if region_label_col else [])],
            how="inner",
            predicate="within",
        )
        joined = joined.drop(
            columns=[c for c in joined.columns if c.startswith("index_")],
            errors="ignore",
        )

        if joined.empty:
            return (
                f"No features from '{features_path}' fall within any region in '{regions_path}'. "
                f"Check that both layers overlap geographically."
            )

        agg_func = agg_func.lower().strip()
        if agg_func not in ("sum", "mean", "count", "max", "min"):
            agg_func = "sum"

        group_col = region_label_col if region_label_col else joined.index

        if agg_func == "count" or not agg_columns:
            # Count features per region
            counts = (
                joined.groupby(region_label_col)
                .size()
                .reset_index(name="feature_count")
                if region_label_col
                else None
            )
            result_cols = (
                {"feature_count": counts["feature_count"].values}
                if counts is not None
                else {}
            )
            group_keys = counts[region_label_col].values if counts is not None else []
        else:
            cols = [
                c.strip()
                for c in agg_columns.split(",")
                if c.strip() and c.strip() in joined.columns
            ]
            if not cols:
                available = [c for c in joined.columns if c not in ("geometry",)]
                return (
                    f"None of the requested columns ({agg_columns}) found in features layer. "
                    f"Available columns: {', '.join(available[:15])}"
                )

            agg_dict = {c: agg_func for c in cols}
            # Also always include count
            agg_dict["__count__"] = (
                "geometry" if "geometry" in joined.columns else cols[0],
                "count",
            )

            if region_label_col:
                agg = joined.groupby(region_label_col)[cols].agg(agg_func)
                count_series = (
                    joined.groupby(region_label_col).size().rename("feature_count")
                )
                agg = agg.join(count_series).reset_index()
                group_keys = agg[region_label_col].values
                result_cols = {c: agg[c].values for c in cols}
                result_cols["feature_count"] = agg["feature_count"].values
            else:
                return "Could not identify a region label column — specify region_label_col."

        # Merge aggregated values back onto region geometries
        out_gdf = gdf_reg.copy()
        if region_label_col and len(group_keys):
            merge_df = pd.DataFrame(result_cols)
            merge_df[region_label_col] = group_keys
            out_gdf = out_gdf.merge(merge_df, on=region_label_col, how="left")

        # Sanitise column names
        out_gdf.columns = [
            re.sub(r"[^\w]", "_", str(c))[:60] if c != "geometry" else c
            for c in out_gdf.columns
        ]

        if not output_filename:
            reg_stem = os.path.splitext(os.path.basename(reg_full))[0][:16]
            feat_stem = os.path.splitext(os.path.basename(feat_full))[0][:14]
            output_filename = f"{reg_stem}_{agg_func}_{feat_stem}"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        out_gdf.to_file(output_path, driver="GPKG")

        # Summary for response
        n_regions = len(out_gdf)
        n_matched = int((out_gdf.get("feature_count", pd.Series([0])) > 0).sum())

        if "feature_count" in out_gdf.columns:
            total = int(out_gdf["feature_count"].sum())
            top3 = out_gdf.nlargest(3, "feature_count")
            if (
                region_label_col
                and re.sub(r"[^\w]", "_", str(region_label_col))[:60] in out_gdf.columns
            ):
                lbl = re.sub(r"[^\w]", "_", str(region_label_col))[:60]
                top_str = ", ".join(
                    f"{row[lbl]} ({int(row['feature_count'])})"
                    for _, row in top3.iterrows()
                    if pd.notna(row.get("feature_count"))
                )
            else:
                top_str = ""
        else:
            total = len(joined)
            top_str = ""

        agg_col_note = (
            f" Aggregated column(s): {agg_columns}."
            if agg_columns and agg_func != "count"
            else ""
        )
        top_note = f" Top regions: {top_str}." if top_str else ""

        return (
            f"{agg_func.capitalize()} of features per region: {n_matched}/{n_regions} regions have data, "
            f"{total} total features matched.{agg_col_note}{top_note} "
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Each region carries 'feature_count' as an attribute."
        )

    except Exception as e:
        return f"aggregate_by_region failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = aggregate_by_region
TOOL_SCHEMA = AggregateByRegionArgs
