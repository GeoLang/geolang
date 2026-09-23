# Viewer Integration: ViewTopia Commands

The GeoLang agent sends **viewer commands** over the `/chat/agui` SSE stream. ViewTopia listens for `viewer_cmd` events and dispatches them by `action` name to the handlers in [`viewtopia/src/viewer/commands.ts`](https://github.com/GeoLang/viewtopia/blob/master/src/viewer/commands.ts).

This page covers the commands this service emits. The browser side of `sql_query` is documented in [`viewtopia/docs/duckdb-wasm.md`](https://github.com/GeoLang/viewtopia/blob/master/docs/duckdb-wasm.md).

## SSE event shape

A tool returns the command as a marker in its result string:

```
__VIEWER_CMD__:{"action": "<name>", "params": { ... }}
```

`agent_event_stream` pulls every such marker out of the run's text and tool returns, and the AG-UI encoder sends each one as a `CUSTOM` event:

```json
{ "type": "CUSTOM", "name": "viewer_cmd", "value": { "action": "<name>", "params": { ... } } }
```

This service emits two actions: `run` from `viewer_control` and `sql_query` from the tool of that name.

## `viewer_control` with `action='run'`

`run` is the only `action` [`viewer_control`](../src/agents/tools/viewer_control.py) takes. The viewer sends its action catalogue with every chat message, [`viewer_state.py`](../src/api/viewer_state.py) writes it into the run's system prompt, and the model names one entry back:

```
__VIEWER_CMD__:{"action": "run", "params": {"name": "<catalogue name>", "args": {...}}}
```

The viewer runs that against its own action registry. The catalogue is the list of what exists, so this service keeps no list of action names. The action's parameters can be plain fields of the call, JSON text in `args`, or that JSON text in `url`, which is where grok puts it. A `url`, whether a top-level field or inside `args`, must be `http` or `https`, or the call is refused. An action the catalogue marks `reads` answers a question rather than changing the map, and its answer comes back as the next run's user message. See [api_reference.md](api_reference.md#post-chatagui).

## `sql_query`: in-browser DuckDB Spatial

ViewTopia embeds DuckDB-WASM with the spatial extension. The agent can hand the viewer a SQL string and the viewer runs it locally, with no server round trip.

**When to emit:** a one-off analytical question answerable from data the viewer already reaches (an attached viewer layer, a public GeoParquet, CSV or GeoJSON URL read with `read_parquet` or `read_csv`, or a table attached by the `sql.attach_url` action), where the answer is a row set or feature set to show. Attaching a URL as a table is that action's job, not a statement written here.

**Params:**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `sql` | string | required | DuckDB SQL. The spatial extension is loaded. |
| `show_on_map` | bool | `true` | Convert the result to GeoJSON with `ST_AsGeoJSON` and add it as an agent layer named "SQL result", which every renderer draws. |
| `color` | string | `#3388ff` | CSS colour for that layer. |
| `fit` | bool | `true` | Zoom to the result extent. |

**Geometry detection**, in the viewer, in this order:
1. A DuckDB `GEOMETRY` column.
2. A `VARCHAR` column named `geom`, `geometry`, `the_geom`, `wkt` or `shape`, read as WKT.
3. A numeric `lon`/`lat` pair (also `lng`, `long`, `longitude`, and `x`/`y`).

If none match, no layer is added and the query still counts as answered: `queryAsGeoJson` raises `NoGeometryError`, the result summary is published as usual, and `viewtopia:sql_error` is dispatched only when the query itself failed.

**Example, "show me populated places above 1M people":**

```
__VIEWER_CMD__:{"action": "sql_query", "params": {"sql": "SELECT name, pop_max, ST_Point(longitude, latitude) AS geom FROM read_parquet('https://example.com/places.parquet') WHERE pop_max > 1000000", "show_on_map": true, "color": "#ff8800", "fit": true}}
```

## Agent-side tool definition

[`src/agents/tools/sql_query.py`](../src/agents/tools/sql_query.py) emits this command. It takes `sql`, `show_on_map`, `color` and `fit`, and returns the marker as its result string.

It declares `TOOL_RUNS_CALLER_CODE = True`, because the SQL runs in whichever browser receives the command. That keeps it out of the MCP manifest and the MCP call path, where the SQL's author and the browser running it need not be the same person, and leaves it on the `/chat` path only.

### When not to use `sql_query`

- The data is only in Ptolemy and is large: use `ptolemy_query`, so the geodatabase does the work and returns a small result.
- The query changes data: use Ptolemy. DuckDB-WASM is per-session and discarded on reload.
- The user wants a persistent layer in a shared project: go through Ptolemy or Fenestra so collaborators see it too.
- The analysis takes more than one step: use `plan_workflow` and `run_workflow`, which give the user a reviewable plan and output files.

## Results back to the agent

A catalogue action the viewer marks `reads` does come back: the viewer posts its answer as the next run's user message, `Result of <name>: <text>`. `sql_query` is not a catalogue action, so its result does not.

The viewer keeps the last 20 SQL result summaries on `window.__viewtopiaSqlResults` and dispatches a `viewtopia:sql_result` CustomEvent, but the agent cannot read them. If the agent needs result rows for a follow-up turn, the options are:

1. **Refine by SQL**: the agent emits a more selective `sql_query` instead of asking for raw rows.
2. **Result echo route**: add `POST /chat/sql_result` here, have ViewTopia post the summary to it, and append it to the sibyl session the way `/upload` and `/draw` do.
3. **Tool-call return value**: if the viewer ever answers tool calls directly, the result comes back as the call's return.

None is built. Option 2 is the smallest change.
