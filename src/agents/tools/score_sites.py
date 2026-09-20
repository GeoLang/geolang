from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path

from ._osm_tags import features_matching_tags, merge_tags, tags_for_category
from ._sites import resolve_sites


class ScoreSitesArgs(BaseModel):
    sites: Optional[str] = Field(
        None,
        description=(
            "Site names to evaluate, each geocoded, separated by semicolons and not "
            "commas: 'Shoreditch London; Brixton London'. A 'lat, lon' pair is taken "
            "as coordinates. Give this or sites_path, not both."
        ),
    )
    sites_path: Optional[str] = Field(
        None,
        description=(
            "The sites as a layer instead, a filename in outputs/ or user_data/ and "
            "not a path. Points or polygons, one site per feature. Use this when the "
            "user uploaded or exported their candidate sites, and call "
            "list_user_datasets to learn the filename."
        ),
    )
    name_column: Optional[str] = Field(
        None,
        description=(
            "Column of sites_path holding each site's name. Tries name, site, label "
            "and address when omitted."
        ),
    )
    criteria: str = Field(
        "population,amenities,transport,flood_risk,green_space",
        description=(
            "Comma-separated criteria:\n"
            "  population   population within 2km (GHSL/WorldPop)\n"
            "  amenities    shops, cafes, restaurants within 1km\n"
            "  transport    bus stops and transit stations within 1km\n"
            "  flood_risk   elevation-based, lower elevation scores lower\n"
            "  green_space  green space share within 1km\n"
            "  competition  competing businesses, fewer scores higher"
        ),
    )
    weights: Optional[str] = Field(
        None,
        description=(
            "Comma-separated weights in criteria order, e.g. '3,2,2,1,1'. Equal "
            "weights if omitted."
        ),
    )
    competition_type: Optional[str] = Field(
        None,
        description=(
            "What counts as competition, in download_osm_data syntax, e.g. "
            "'supermarkets' or 'amenity=cafe'. Only for the competition criterion."
        ),
    )
    competitors_path: Optional[str] = Field(
        None,
        description=(
            "The user's own competitor layer, a filename in outputs/ or user_data/ "
            "and not a path. The competition criterion then counts its features "
            "within 1km of each site instead of OSM ones. Give this or "
            "competition_type, not both."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


OPENTOPODATA_MAX_LOCATIONS = 100

# every criterion that counts nearby features counts them within this distance
SITE_RADIUS_M = 1000


def score_sites(
    sites: str = None,
    sites_path: str = None,
    name_column: str = None,
    criteria: str = "population,amenities,transport,flood_risk,green_space",
    weights: str = None,
    competition_type: str = None,
    competitors_path: str = None,
    output_filename: str = None,
) -> str:
    """
    Rank locations against weighted criteria, scored 0-100 and saved as a
    point GPKG with a comparison table. Use this when the user wants to compare
    or rank sites, or find the best location on several factors.
    """
    import traceback


    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import Point

        if competition_type and competitors_path:
            return (
                "Give either competition_type (an OSM category) or competitors_path "
                "(your own layer), not both."
            )

        site_list = resolve_sites(sites, sites_path, name_column)
        if isinstance(site_list, str):
            return site_list
        if len(site_list) < 2:
            return "Provide at least 2 site names, separated by semicolons (;)."

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

        # Evaluate criteria per site — batch OSM queries where possible
        raw_scores = {c: [] for c in criteria_list}
        osm_criteria = {"amenities", "transport", "green_space", "competition"}
        needs_osm = bool(osm_criteria & set(criteria_list))

        ox.settings.timeout = 30
        ox.settings.overpass_rate_limit = False

        competition_tags = None
        if "competition" in criteria_list and competition_type:
            competition_tags = tags_for_category(competition_type)

        competitor_metric = None
        competitor_crs = None
        if competitors_path:
            competitor_frame = gpd.read_file(
                tool_input_path("competitors_path", competitors_path)
            )
            if competitor_frame.empty:
                return f"Competitor layer is empty: {competitors_path}"
            if competitor_frame.crs is None:
                competitor_frame = competitor_frame.set_crs("EPSG:4326")
            competitor_frame = competitor_frame.to_crs("EPSG:4326")
            competitor_crs = competitor_frame.estimate_utm_crs()
            competitor_metric = competitor_frame.to_crs(competitor_crs)

        site_elevations = [None] * len(site_list)
        if "flood_risk" in criteria_list:
            for start in range(0, len(site_list), OPENTOPODATA_MAX_LOCATIONS):
                batch = site_list[start : start + OPENTOPODATA_MAX_LOCATIONS]
                locations = "|".join(f"{s['lat']:.6f},{s['lon']:.6f}" for s in batch)
                elev_url = (
                    f"https://api.opentopodata.org/v1/srtm90m?locations={locations}"
                )
                try:
                    elev_resp = requests.get(elev_url, timeout=10)
                    if elev_resp.status_code == 200:
                        elev_data = elev_resp.json()
                        if elev_data.get("status") == "OK":
                            for offset, result in enumerate(
                                elev_data.get("results", [])
                            ):
                                elev = result.get("elevation")
                                if elev is not None:
                                    site_elevations[start + offset] = float(elev)
                except Exception:
                    pass

        for site_index, site in enumerate(site_list):
            lat, lon = site["lat"], site["lon"]

            # Batch all OSM data in a single Overpass query
            osm_features = None
            if needs_osm:
                tag_groups = []
                if "amenities" in criteria_list:
                    tag_groups.append(
                        {"amenity": ["cafe", "restaurant", "bar"], "shop": True}
                    )
                if "transport" in criteria_list:
                    tag_groups.append(
                        {
                            "highway": "bus_stop",
                            "public_transport": True,
                            "railway": ["station", "halt"],
                        }
                    )
                if "green_space" in criteria_list:
                    tag_groups.append(
                        {
                            "landuse": ["grass", "forest", "meadow"],
                            "leisure": ["park", "garden", "nature_reserve"],
                        }
                    )
                if competition_tags:
                    tag_groups.append(competition_tags)
                tags = merge_tags(tag_groups)
                if tags:
                    try:
                        osm_features = ox.features_from_point(
                            (lat, lon), tags=tags, dist=SITE_RADIUS_M
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
                        elevation = site_elevations[site_index]
                        if elevation is not None:
                            score = elevation

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

                    elif criterion == "competition" and competitor_metric is not None:
                        site_point = (
                            gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326")
                            .to_crs(competitor_crs)
                            .iloc[0]
                        )
                        near = competitor_metric.geometry.distance(site_point)
                        score = float((near <= SITE_RADIUS_M).sum())

                    elif (
                        criterion == "competition"
                        and competition_tags
                        and osm_features is not None
                    ):
                        score = float(
                            len(features_matching_tags(osm_features, competition_tags))
                        )

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
        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")
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
