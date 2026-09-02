# Viewer Integration — ViewTopia Commands

The GeoLang agent emits **viewer commands** over the `/chat/agui` SSE channel. ViewTopia listens for `viewer_cmd` events and dispatches them by `action` name to handlers registered in [`viewtopia/src/viewer/commands.ts`](https://github.com/GeoLang/viewtopia/blob/main/src/viewer/commands.ts).

This doc covers commands that the agent server is responsible for emitting. The frontend side is documented in [`viewtopia/docs/duckdb-wasm.md`](https://github.com/GeoLang/viewtopia/blob/main/docs/duckdb-wasm.md).

## SSE event shape

A tool returns the command as a marker in its result string:

```
__VIEWER_CMD__:{"action": "<name>", "params": { ... }}
```

`agent_event_stream` pulls every such marker out of the run's text and the AG-UI encoder sends each one as a `CUSTOM` event, so the viewer receives:

```json
{ "type": "CUSTOM", "name": "viewer_cmd", "value": { "action": "<name>", "params": { ... } } }
```

## `viewer_control` with `action='run'`

`run` is the only `action` [`viewer_control`](../src/agents/tools/viewer_control.py) takes. The viewer sends its action catalogue with every chat message, [`viewer_state.py`](../src/api/viewer_state.py) renders it into the run's system prompt, and the model names one entry back:

```
__VIEWER_CMD__:{"action": "run", "params": {"name": "<catalogue name>", "args": {...}}}
```

The viewer executes that against its own registry. The catalogue is the list of what exists, so no list of action names is kept here to drift from it. `args` written as JSON text is accepted, and so is that text put in `url`, which is where grok puts it. An action the catalogue marks `reads` answers a question rather than changing the map, and its answer comes back as the next run's user message. See [api_reference.md](api_reference.md#post-chatagui).

`sql_query` below is the one command this service still defines itself.

## `sql_query` — in-browser DuckDB Spatial

ViewTopia embeds DuckDB-WASM with the spatial extension. The agent can hand the viewer a SQL string and the viewer will execute it locally — no round-trip to a server, no Ptolemy query needed for ad-hoc analytical questions.

**When to emit:** the user asks an analytical/spatial question that's answerable from data the viewer already reaches (an attached viewer layer, a public GeoParquet/CSV/GeoJSON URL read with `read_parquet`/`read_csv`, a table attached by the `sql.attach_url` action), and the answer is a row set or a feature set that should be visualised. Attaching the URL is that action's job, not a statement written here.

**Params:**

| field | type | default | meaning |
|---|---|---|---|
| `sql` | string | required | DuckDB SQL. Spatial extension is loaded. |
| `show_on_map` | bool | `true` | If true, ViewTopia converts the result to GeoJSON via `ST_AsGeoJSON` and adds it as an agent layer named "SQL result", which every renderer draws. |
| `color` | string | `#3388ff` | CSS colour for the rendered layer. |
| `fit` | bool | `true` | Auto-zoom to the result extent. |

**Geometry detection** (frontend-side, in this order):
1. A DuckDB `GEOMETRY`-typed column.
2. A `VARCHAR` column named `geom`/`geometry`/`the_geom`/`wkt`/`shape` (treated as WKT).
3. A numeric `lon`/`lat` pair (also `lng`, `long`, `longitude`, `x`/`y`).

If none match, no layer is added and the query still counts as answered: `queryAsGeoJson` raises `NoGeometryError`, the result summary is published as usual, and `viewtopia:sql_error` is dispatched only when the query itself failed.

**Example — "show me populated places above 1M people":**

```
__VIEWER_CMD__:{"action": "sql_query", "params": {"sql": "SELECT name, pop_max, ST_Point(longitude, latitude) AS geom FROM read_parquet('https://example.com/places.parquet') WHERE pop_max > 1000000", "show_on_map": true, "color": "#ff8800", "fit": true}}
```

## Agent-side tool definition

[`src/agents/tools/sql_query.py`](../src/agents/tools/sql_query.py) is the module that emits this command. It takes `sql`, `show_on_map`, `color` and `fit`, and returns the marker as its result string.

It declares `TOOL_RUNS_CALLER_CODE = True`, because the SQL runs in whichever browser receives the command. That keeps it out of the MCP manifest and the MCP call path, where the SQL's author and the browser running it need not be the same person, and leaves it offered on the `/chat` path only.

### When NOT to use `sql_query`

- Data lives only in Ptolemy and is large → use the existing Ptolemy REST tools so the geodatabase does the work and returns a small result.
- The query mutates state → use Ptolemy (DuckDB-WASM is per-session and ephemeral).
- The user wants a persistent layer in a shared project → route through Ptolemy/Fenestra so collaborators see it too.
- The analysis takes more than one step → use `plan_workflow` and `run_workflow`, which give the user a reviewable plan and reusable output files.

## Round-tripping results back to the agent

A catalogue action the viewer marks `reads` does come back: the viewer posts its answer as the next run's user message, whose text is `Result of <name>: <text>`. `sql_query` is not one of those actions, so it does not.

The SSE channel is one-way (agent → viewer). The viewer stashes the last 20 SQL result summaries on `window.__viewtopiaSqlResults` and dispatches a `viewtopia:sql_result` CustomEvent, but the agent cannot read them today.

If we need the agent to reason over result rows for follow-up turns, the options are:

1. **Refine via SQL** — have the agent emit a more selective `sql_query` rather than asking for raw rows.
2. **Result echo endpoint** — add `POST /chat/sql_result` on the agent server, have ViewTopia post the result summary back, and forward it to sibyl as a session message (the same path `/upload` and `/draw` use).
3. **Tool-call return value** — if/when the SSE channel becomes bidirectional (e.g. ChatGPT-style tool-call protocol), the result flows back natively.

Deferred until a concrete use case forces the choice. Option 2 is the cheapest if needed.
