# Viewer Integration — ViewTopia Commands

The GeoLang agent emits **viewer commands** over the `/chat/stream` SSE channel. ViewTopia listens for `viewer_cmd` events and dispatches them by `action` name to handlers registered in [`viewtopia/src/viewer-commands.js`](https://github.com/GeoLang/viewtopia/blob/main/src/viewer-commands.js).

This doc covers commands that the agent server is responsible for emitting. The frontend side is documented in [`viewtopia/docs/duckdb-wasm.md`](https://github.com/GeoLang/viewtopia/blob/main/docs/duckdb-wasm.md).

## SSE event shape

```json
{ "type": "viewer_cmd", "cmd": { "action": "<name>", "params": { ... } } }
```

## `sql_query` — in-browser DuckDB Spatial

ViewTopia embeds DuckDB-WASM with the spatial extension. The agent can hand the viewer a SQL string and the viewer will execute it locally — no round-trip to a server, no Ptolemy query needed for ad-hoc analytical questions.

**When to emit:** the user asks an analytical/spatial question that's answerable from data already attached to the viewer (a layer, a remote GeoParquet/CSV, an attached PostGIS table), and the answer is a row set or a feature set that should be visualised.

**Params:**

| field | type | default | meaning |
|---|---|---|---|
| `sql` | string | required | DuckDB SQL. Spatial extension is loaded. |
| `show_on_map` | bool | `true` | If true, ViewTopia converts the result to GeoJSON via `ST_AsGeoJSON` and renders it on Cesium + Leaflet. |
| `color` | string | `#3388ff` | CSS colour for the rendered layer. |
| `fit` | bool | `true` | Auto-zoom to the result extent. |

**Geometry detection** (frontend-side, in this order):
1. A DuckDB `GEOMETRY`-typed column.
2. A `VARCHAR` column named `geom`/`geometry`/`the_geom`/`wkt`/`shape` (treated as WKT).
3. A numeric `lon`/`lat` pair (also `lng`, `long`, `longitude`, `x`/`y`).

If none match the viewer dispatches a `viewtopia:sql_error` CustomEvent.

**Example — "show me populated places above 1M people":**

```json
{
  "type": "viewer_cmd",
  "cmd": {
    "action": "sql_query",
    "params": {
      "sql": "SELECT name, pop_max, ST_Point(longitude, latitude) AS geom FROM read_parquet('https://example.com/places.parquet') WHERE pop_max > 1000000",
      "show_on_map": true,
      "color": "#ff8800"
    }
  }
}
```

## Agent-side tool definition

The Letta agent needs a tool registered (in [`geolang/src/tools/`](../src/tools/)) that returns this command. Sketch:

```python
def sql_query(sql: str, show_on_map: bool = True, color: str = "#3388ff") -> dict:
    """Run a DuckDB Spatial SQL query in the user's browser and optionally render the result as a map layer.

    Use this for ad-hoc analytical questions over data already accessible to the viewer
    (attached layers, public GeoParquet/CSV, PostGIS via Ptolemy). Prefer this over a
    Ptolemy REST call when the data is reachable from the browser — it's lower latency
    and doesn't burden the server.

    The DuckDB spatial extension is pre-loaded. Geometry is detected from a GEOMETRY
    column, a WKT column named geom/geometry/wkt/shape, or a lon/lat numeric pair.
    """
    return {
        "type": "viewer_cmd",
        "cmd": {"action": "sql_query", "params": {"sql": sql, "show_on_map": show_on_map, "color": color}},
    }
```

### When NOT to use `sql_query`

- Data lives only in Ptolemy and is large → use the existing Ptolemy REST tools so the geodatabase does the work and returns a small result.
- The query mutates state → use Ptolemy (DuckDB-WASM is per-session and ephemeral).
- The user wants a persistent layer in a shared project → route through Ptolemy/Fenestra so collaborators see it too.

## Round-tripping results back to the agent

The SSE channel is one-way (agent → viewer). The viewer currently stashes the last 20 SQL result summaries on `window.__viewtopiaSqlResults` and dispatches a `viewtopia:sql_result` CustomEvent, but the agent cannot read them today.

If we need the agent to reason over result rows for follow-up turns, the options are:

1. **Refine via SQL** — have the agent emit a more selective `sql_query` rather than asking for raw rows.
2. **Result echo endpoint** — add `POST /chat/sql_result` on the agent server; have ViewTopia post the result summary back. Letta puts it in working memory for the next turn.
3. **Tool-call return value** — if/when the SSE channel becomes bidirectional (e.g. ChatGPT-style tool-call protocol), the result flows back natively.

Deferred until a concrete use case forces the choice. Option 2 is the cheapest if needed.
