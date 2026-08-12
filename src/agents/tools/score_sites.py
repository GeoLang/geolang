from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import caller_outputs_dir


class ScoreSitesArgs(BaseModel):
    sites: str = Field(
        ...,
        description=(
            "Semicolon-separated list of site names to evaluate. Each site will be geocoded. "
            "IMPORTANT: Use semicolons (;) to separate sites, not commas. "
            "Example: 'Shoreditch London; Kings Cross London; Brixton London'"
        ),
    )
    criteria: str = Field(
        "population,amenities,transport,flood_risk,green_space",
        description=(
            "Comma-separated scoring criteria. Available:\n"
            "  population    — population within 2km (GHSL/WorldPop)\n"
            "  amenities     — count of shops, cafes, restaurants within 1km\n"
            "  transport     — count of bus stops and transit stations within 1km\n"
            "  flood_risk    — elevation-based flood risk (lower elevation = lower score)\n"
            "  green_space   — green space coverage percentage within 1km\n"
            "  competition   — count of competing businesses (fewer = higher score)\n"
            "Default: population,amenities,transport,flood_risk,green_space"
        ),
    )
    weights: Optional[str] = Field(
        None,
        description=(
            "Optional comma-separated weights matching the criteria order. "
            "E.g. '3,2,2,1,1' to weight population 3x. "
            "If omitted, all criteria are weighted equally."
        ),
    )
    competition_type: Optional[str] = Field(
        None,
        description=(
            "OSM data type for competition criterion. Uses download_osm_data syntax. "
            "E.g. 'supermarkets', 'pharmacies', 'amenity=cafe'. "
            "Only needed if 'competition' is in criteria."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def score_sites(
    sites: str,
    criteria: str = "population,amenities,transport,flood_risk,green_space",
    weights: str = None,
    competition_type: str = None,
    output_filename: str = None,
) -> str:
    """
    Multi-criteria site scoring and ranking. Evaluates multiple locations
    against weighted criteria and produces a ranked comparison table with
    normalised scores (0-100). Saves results as a point GPKG.

    Pass sites as a semicolon-separated list of place names (use ; not , between sites).
    Use this when the user wants to compare locations, rank sites,
    or find the best location based on multiple factors.
    """
    import os
    import traceback

    outputs_dir = caller_outputs_dir()

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import Point

        # Parse inputs — semicolon-separated site names
        site_names = [s.strip() for s in sites.split(";") if s.strip()]
        if len(site_names) < 2:
            return "Provide at least 2 site names, separated by semicolons (;)."
        site_list = [{"name": name} for name in site_names]

        criteria_list = [c.strip().lower() for c in criteria.split(",") if c.strip()]
        if not criteria_list:
            return "No valid criteria provided."

        # Parse weights — strip parentheses and other non-numeric chars
        if weights:
            import re

            cleaned = re.sub(r"[^\d.,\s-]", "", weights)
            weight_vals = [float(w.strip()) for w in cleaned.split(",") if w.strip()]
            if len(weight_vals) != len(criteria_list):
                return f"Number of weights ({len(weight_vals)}) must match criteria ({len(criteria_list)})."
        else:
            weight_vals = [1.0] * len(criteria_list)

        # Geocode all sites — parse raw coords if passed instead of a place name
        import re as _re

        for site in site_list:
            if "lon" not in site or "lat" not in site:
                _cm = _re.match(
                    r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", site["name"].strip()
                )
                if _cm:
                    site["lat"], site["lon"] = float(_cm.group(1)), float(_cm.group(2))
                else:
                    try:
                        lat, lon = ox.geocode(site["name"])
                        site["lat"] = lat
                        site["lon"] = lon
                    except Exception as e:
                        return f"Could not geocode '{site['name']}': {e}"

        # Evaluate criteria per site — batch OSM queries where possible
        raw_scores = {c: [] for c in criteria_list}
        osm_criteria = {"amenities", "transport", "green_space", "competition"}
        needs_osm = bool(osm_criteria & set(criteria_list))

        import osmnx as ox

        ox.settings.timeout = 30
        ox.settings.overpass_rate_limit = False

        for site in site_list:
            lat, lon = site["lat"], site["lon"]

            # Batch all OSM data in a single Overpass query
            osm_features = None
            if needs_osm:
                tags = {}
                if "amenities" in criteria_list:
                    tags["amenity"] = ["cafe", "restaurant", "bar"]
                    tags["shop"] = True
                if "transport" in criteria_list:
                    tags["highway"] = "bus_stop"
                    tags["public_transport"] = True
                    tags["railway"] = ["station", "halt"]
                if "green_space" in criteria_list:
                    tags["landuse"] = ["grass", "forest", "meadow"]
                    tags["leisure"] = ["park", "garden", "nature_reserve"]
                if "competition" in criteria_list and competition_type:
                    dt_comp = competition_type.lower().strip()
                    if "=" in dt_comp:
                        ckey, cval = dt_comp.split("=", 1)
                        existing = tags.get(ckey.strip())
                        if existing is None:
                            tags[ckey.strip()] = cval.strip()
                        elif isinstance(existing, list):
                            tags[ckey.strip()] = existing + [cval.strip()]
                        else:
                            tags[ckey.strip()] = True
                if tags:
                    try:
                        osm_features = ox.features_from_point(
                            (lat, lon), tags=tags, dist=1000
                        )
                    except Exception:
                        osm_features = None

            for criterion in criteria_list:
                score = 0.0
                try:
                    if criterion == "population":
                        delta = 0.018
                        pop_url = (
                            "https://api.worldpop.org/v1/services/stats"
                            f"?dataset=wpgpas&year=2020"
                            f"&bbox={lon - delta:.4f},{lat - delta:.4f},{lon + delta:.4f},{lat + delta:.4f}"
                        )
                        try:
                            pop_resp = requests.get(pop_url, timeout=10)
                            if pop_resp.status_code == 200:
                                pop_data = pop_resp.json()
                                if (
                                    pop_data.get("status") == "success"
                                    and "data" in pop_data
                                ):
                                    pop_val = pop_data["data"].get("total_population")
                                    if pop_val is not None:
                                        score = float(pop_val)
                        except Exception:
                            pass

                    elif criterion == "amenities" and osm_features is not None:
                        amenity_col = osm_features.get("amenity")
                        shop_col = osm_features.get("shop")
                        count = 0
                        if amenity_col is not None:
                            count += int(
                                amenity_col.isin(["cafe", "restaurant", "bar"]).sum()
                            )
                        if shop_col is not None:
                            count += int(shop_col.notna().sum())
                        score = float(count)

                    elif criterion == "transport" and osm_features is not None:
                        count = 0
                        for col_name, match_vals in [
                            ("highway", ["bus_stop"]),
                            ("public_transport", None),
                            ("railway", ["station", "halt"]),
                        ]:
                            col = osm_features.get(col_name)
                            if col is not None:
                                if match_vals:
                                    count += int(col.isin(match_vals).sum())
                                else:
                                    count += int(col.notna().sum())
                        score = float(count)

                    elif criterion == "flood_risk":
                        elev_url = f"https://api.opentopodata.org/v1/srtm90m?locations={lat},{lon}"
                        try:
                            elev_resp = requests.get(elev_url, timeout=10)
                            if elev_resp.status_code == 200:
                                elev_data = elev_resp.json()
                                if elev_data.get("status") == "OK" and elev_data.get(
                                    "results"
                                ):
                                    elev = elev_data["results"][0].get("elevation")
                                    if elev is not None:
                                        score = float(elev)
                        except Exception:
                            pass

                    elif criterion == "green_space" and osm_features is not None:
                        landuse_col = osm_features.get("landuse")
                        leisure_col = osm_features.get("leisure")
                        green_mask = False
                        if landuse_col is not None:
                            green_mask = green_mask | landuse_col.isin(
                                ["grass", "forest", "meadow"]
                            )
                        if leisure_col is not None:
                            green_mask = green_mask | leisure_col.isin(
                                ["park", "garden", "nature_reserve"]
                            )
                        greens = osm_features[green_mask]
                        if len(greens) > 0:
                            greens_proj = greens.to_crs("EPSG:3857")
                            score = float(greens_proj.geometry.area.sum())

                    elif (
                        criterion == "competition"
                        and competition_type
                        and osm_features is not None
                    ):
                        dt_comp = competition_type.lower().strip()
                        if "=" in dt_comp:
                            ckey, cval = dt_comp.split("=", 1)
                            col = osm_features.get(ckey.strip())
                            if col is not None:
                                score = float((col == cval.strip()).sum())
                        else:
                            COMP_MAP = {
                                "cafes": ("amenity", "cafe"),
                                "restaurants": ("amenity", "restaurant"),
                                "shops": ("shop", None),
                                "supermarkets": ("shop", "supermarket"),
                                "pharmacies": ("amenity", "pharmacy"),
                            }
                            mapping = COMP_MAP.get(dt_comp)
                            if mapping:
                                mkey, mval = mapping
                                col = osm_features.get(mkey)
                                if col is not None:
                                    if mval:
                                        score = float((col == mval).sum())
                                    else:
                                        score = float(col.notna().sum())

                except Exception:
                    score = 0.0
                raw_scores[criterion].append(score)

        # Normalise scores to 0–100 per criterion
        norm_scores = {}
        for criterion in criteria_list:
            vals = raw_scores[criterion]
            vmin = min(vals)
            vmax = max(vals)

            if vmax == vmin:
                norm_scores[criterion] = [50.0] * len(vals)
            else:
                # For flood_risk, lower raw value = higher risk = lower score
                # For competition, fewer competitors = higher score
                if criterion in ("flood_risk",):
                    # Higher elevation = better, so normal direction
                    norm_scores[criterion] = [
                        round((v - vmin) / (vmax - vmin) * 100, 1) for v in vals
                    ]
                elif criterion in ("competition",):
                    # Fewer competitors = better (inverse)
                    norm_scores[criterion] = [
                        round((1 - (v - vmin) / (vmax - vmin)) * 100, 1) for v in vals
                    ]
                else:
                    # More = better (population, amenities, transport, green_space)
                    norm_scores[criterion] = [
                        round((v - vmin) / (vmax - vmin) * 100, 1) for v in vals
                    ]

        # Compute weighted total
        totals = []
        total_weight = sum(weight_vals)
        for i in range(len(site_list)):
            weighted_sum = sum(
                norm_scores[c][i] * w for c, w in zip(criteria_list, weight_vals)
            )
            totals.append(round(weighted_sum / total_weight, 1))

        # Build result table
        rows = []
        for i, site in enumerate(site_list):
            row = {
                "name": site["name"],
                "lon": round(site["lon"], 4),
                "lat": round(site["lat"], 4),
                "total_score": totals[i],
                "rank": 0,  # filled after sorting
            }
            for criterion in criteria_list:
                row[f"{criterion}_raw"] = round(raw_scores[criterion][i], 1)
                row[f"{criterion}_score"] = norm_scores[criterion][i]
            rows.append(row)

        # Rank by total score descending
        rows.sort(key=lambda r: r["total_score"], reverse=True)
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank

        # Save as GPKG
        gdf = gpd.GeoDataFrame(
            rows,
            geometry=[Point(r["lon"], r["lat"]) for r in rows],
            crs="EPSG:4326",
        )

        if not output_filename:
            output_filename = "site_scores"

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        gdf.to_file(output_path, driver="GPKG")

        # Build readable response
        parts = [f"Site scoring results ({', '.join(criteria_list)}):"]
        parts.append("")
        for row in rows:
            line = f"#{row['rank']} {row['name']} — {row['total_score']}/100"
            details = []
            for c in criteria_list:
                details.append(f"{c}={row[f'{c}_score']:.0f}")
            line += f" ({', '.join(details)})"
            parts.append(line)

        # Centre point
        mid_lon = np.mean([r["lon"] for r in rows])
        mid_lat = np.mean([r["lat"] for r in rows])

        parts.append("")
        parts.append(
            f"Saved to outputs/{output_filename}.gpkg. "
            f"Center: lon={mid_lon:.4f}, lat={mid_lat:.4f}"
        )

        return "\n".join(parts)

    except Exception as e:
        return f"Site scoring failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = score_sites
TOOL_SCHEMA = ScoreSitesArgs
