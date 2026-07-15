"""
In-browser DuckDB Spatial SQL tool.

Emits a sql_query viewer command; ViewTopia executes the SQL locally in
DuckDB-WASM (spatial extension loaded) and renders geometry results on the map.
See docs/viewer_integration.md for the protocol.
"""
from pydantic import BaseModel, Field
from typing import Optional


class SqlQueryArgs(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "DuckDB SQL to run in the viewer. The spatial extension is loaded. "
            "Geometry is detected from a GEOMETRY column, a WKT column named "
            "geom/geometry/wkt/shape, or a lon/lat numeric pair. "
            "Example: SELECT name, pop_max, ST_Point(longitude, latitude) AS geom "
            "FROM read_parquet('https://example.com/places.parquet') WHERE pop_max > 1000000"
        ),
    )
    show_on_map: bool = Field(
        True,
        description="Render the result as a map layer when a geometry column is detected.",
    )
    color: Optional[str] = Field(
        "#3388ff",
        description="CSS colour for the rendered layer, e.g. '#ff8800'.",
    )
    fit: bool = Field(
        True,
        description="Auto-zoom the camera to the result extent.",
    )


def sql_query(
    sql: str,
    show_on_map: bool = True,
    color: str = "#3388ff",
    fit: bool = True,
) -> str:
    """Run a DuckDB Spatial SQL query in the user's browser and optionally render
    the result as a map layer. Use this for ad-hoc analytical questions over data
    already reachable from the viewer: attached layers, public GeoParquet/CSV URLs
    via read_parquet/read_csv, or remote GeoJSON. Prefer this over server-side
    tools when the data is reachable from the browser: it is lower latency and
    does not burden the server. Do NOT use it for large server-side datasets,
    mutations, or results that must persist for collaborators."""
    import json

    cmd = {
        "action": "sql_query",
        "params": {
            "sql": sql,
            "show_on_map": show_on_map,
            "color": color or "#3388ff",
            "fit": fit,
        },
    }
    return f"__VIEWER_CMD__:{json.dumps(cmd)}"


TOOL_FUNCTION = sql_query
TOOL_SCHEMA = SqlQueryArgs
