import inspect
import json
import logging
import os
import re
import uuid

import asyncio
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Thread

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ag_ui.core import (
    CustomEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

import httpx

from src.agents.agent_manager import PERSONA, load_external_tools
from src.agents.workflows import get_progress_text, infer_ui_spec_from_text
from src.core.auth import require_platform_token
from src.core.user_token import bearer_token, user_token_scope
from src.core.utils import (
    EXEC_DIR,
    OUTPUTS_DIR,
    SIBYL_URL,
    USER_DATA_DIR,
    load_catalogue,
    load_shares,
    save_catalogue,
    save_shares,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GeoLang API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# sibyl owns the agent loop and session history, geolang runs the tools
SIBYL_TIMEOUT = 30.0


def _preload_geo_stack():
    """Pay the geo-stack import cost at boot instead of on the first tool call."""
    try:
        import geopandas
        import rasterio

        logger.info(
            f"Geo stack preloaded: geopandas {geopandas.__version__}, "
            f"rasterio {rasterio.__version__}"
        )
    except Exception as e:
        logger.warning(f"Geo stack preload failed (first tool call will be slow): {e}")


@app.on_event("startup")
async def startup():
    Thread(target=_preload_geo_stack, daemon=True).start()


async def sibyl_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Call sibyl, turning an unreachable service into a 503."""
    try:
        async with httpx.AsyncClient(
            base_url=SIBYL_URL, timeout=SIBYL_TIMEOUT
        ) as client:
            return await client.request(method, path, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503, detail=f"Agent service unreachable: {e}"
        )


async def notify_agent(text: str) -> None:
    """Append a message to sibyl's active session without running the model."""
    try:
        async with httpx.AsyncClient(
            base_url=SIBYL_URL, timeout=SIBYL_TIMEOUT
        ) as client:
            sessions = (await client.get("/sessions")).json()
            session = next((s for s in sessions if s.get("active")), None)
            if session is None:
                session = (
                    await client.post("/sessions", json={"name": "Default"})
                ).json()
            await client.post(
                f"/sessions/{session['id']}/messages", json={"content": text}
            )
    except Exception as e:
        logger.warning(f"Could not notify agent: {e}")


def _slim_schema(node):
    """strip pydantic boilerplate the model doesn't need: titles, anyOf-null wrappers, null defaults"""
    if isinstance(node, dict):
        options = [o for o in node.get("anyOf", []) if o != {"type": "null"}]
        if len(options) == 1:
            del node["anyOf"]
            node.update(options[0])
        node.pop("title", None)
        if "default" in node and node["default"] is None:
            del node["default"]
        for child in node.values():
            _slim_schema(child)
    elif isinstance(node, list):
        for child in node:
            _slim_schema(child)
    return node


@app.get("/tools")
def list_tools():
    """Tool manifest for sibyl: what it can call and with which arguments."""
    tools = []
    for func, schema in load_external_tools():
        if schema is None:
            logger.warning(f"Tool {func.__name__} has no TOOL_SCHEMA, skipping")
            continue
        tools.append(
            {
                "name": func.__name__,
                "description": inspect.getdoc(func) or "",
                "parameters": _slim_schema(schema.model_json_schema()),
            }
        )
    return {"tools": tools}


class ToolCallRequest(BaseModel):
    args: dict = {}
    # Set by callers that run a tool outside the model's turn, such as the
    # viewer's plan-approval button: the result is appended to the sibyl session
    # so the model knows it happened. sibyl itself never sets it, which is what
    # keeps a run the model asked for from being reported back to it twice.
    notify: bool = False


# sync so FastAPI runs it in the threadpool: tools block for minutes
@app.post("/tools/{name}")
def run_tool(
    name: str,
    request: ToolCallRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Execute a tool in-process and return its string result.

    sibyl passes the caller's bearer through on every tool call of a run, and the
    viewer sends its own on the plan-approval path. Whatever arrives is what the
    tool's outbound calls go out as.

    With `PLATFORM_JWT_SECRET` set the bearer must be a live platform token.
    Without the secret the route is open, which is the standalone dev flow.
    """
    token = bearer_token(authorization)
    # before the lookup, so an unauthenticated caller learns nothing from a 404
    require_platform_token(token)

    # schema-less modules are not in the manifest either, so they are unknown here
    entry = next(
        (t for t in load_external_tools() if t[0].__name__ == name and t[1]), None
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
    func, schema = entry

    try:
        args = schema(**request.args).model_dump(exclude_unset=True)
    except ValidationError as e:
        return {"result": f"❌ Invalid arguments: {e}"}

    try:
        with user_token_scope(token):
            result = func(**args)
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        result = f"❌ Tool execution failed: {e}"

    if request.notify:
        # the viewer parses the markers itself, and truncation would leave half a
        # JSON blob in the session, so the model gets the prose only
        prose = re.sub(r"\n?__[A-Z_]+__:.*", "", str(result))
        # this route is sync, so it runs in a worker thread with no event loop
        asyncio.run(notify_agent(f"[{name} run from the viewer] {prose[:800]}"))

    return {"result": result}


def marker_payloads(content: str, marker: str):
    """Every JSON payload on a ``MARKER:{json}`` line of a tool's output."""
    for part in content.split(marker)[1:]:
        line = part.split("\n")[0].strip()
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning(f"unparseable {marker} payload: {line[:120]}")
            continue
        yield payload


async def agent_event_stream(message: str, user_token: str | None = None):
    """Run a sibyl agent run and yield normalized (kind, payload) events.

    Single source of truth for the marker parsing. kinds:
    "text", "progress", "viewer_cmd", "ui_spec", "plan", "run", "error",
    "keepalive".
    /chat/agui renders these as AG-UI events.

    `user_token` is the caller's bearer. sibyl holds it for the run and sends it
    back on every tool call, so the tools act as this user. Without one the run
    is anonymous.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    body = {"system_prompt": PERSONA, "message": message}
    if user_token:
        body["user_token"] = user_token

    def run_in_thread():
        # no read timeout: a tool call can keep the stream silent for minutes
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    f"{SIBYL_URL}/runs",
                    json=body,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.strip():
                            loop.call_soon_threadsafe(q.put_nowait, json.loads(line))
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, {"__error__": str(e)})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    Thread(target=run_in_thread, daemon=True).start()

    all_content = []
    assistant_texts = []
    ui_spec = None
    planned = False

    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=15.0)
        except asyncio.TimeoutError:
            # Send keepalive comment to prevent browser SSE timeout
            yield ("keepalive", None)
            continue
        if event is None:
            break

        if "__error__" in event:
            yield ("error", event["__error__"])
            break

        kind = event.get("kind")

        if kind == "tool_call":
            tool_name = str(event.get("name", "") or "")
            tool_args = str(event.get("args", "") or "")
            logger.info(f"TOOL CALL: {tool_name}({tool_args[:200]})")
            yield ("progress", get_progress_text(tool_name, tool_args))
            continue

        if kind in ("text", "tool_return"):
            content = str(event.get("content", "") or "")

            if kind == "tool_return":
                logger.info(f"TOOL RETURN: {event.get('name')} {content[:300]}")
                # Surface tool errors as progress events so the user sees them
                if content.startswith("❌") or content.startswith("ERROR"):
                    yield ("progress", content[:200].split("\n")[0])
            else:
                assistant_texts.append(content)

            all_content.append(content)

            # UI spec from the emit_ui_spec tool
            if ui_spec is None and "__UI_SPEC__:" in content:
                try:
                    ui_spec = json.loads(content.split("__UI_SPEC__:", 1)[1])
                except Exception:
                    pass

            # Viewer commands from the viewer_control tool
            if "__VIEWER_CMD__:" in content:
                for part in content.split("__VIEWER_CMD__:")[1:]:
                    try:
                        cmd = json.loads(part.split("\n")[0].strip())
                        yield ("viewer_cmd", cmd)
                    except Exception:
                        pass

            # Workflow plan from plan_workflow, awaiting the user's approval
            for plan in marker_payloads(content, "__PLAN__:"):
                planned = True
                yield ("plan", plan)

            # Per-step outcome of a run the model started itself, from run_workflow
            for report in marker_payloads(content, "__RUN__:"):
                yield ("run", report)

            if kind == "text":
                yield ("text", content)
            continue

        if kind == "error":
            yield ("error", event.get("message", ""))
            break

        if kind == "done":
            break

    # a plan names output files that do not exist yet, and the inference below
    # cannot tell those from files a tool actually wrote
    if ui_spec is None and not planned:
        import re as _re

        # Primary: infer from full content, filter to files the agent mentioned
        ui_spec = infer_ui_spec_from_text(" ".join(all_content))
        if ui_spec and ui_spec.get("layers") and assistant_texts:
            agent_text = " ".join(assistant_texts)
            filtered = [
                l
                for l in ui_spec["layers"]
                if l["file"] in agent_text
                or l["file"].replace("outputs/", "") in agent_text
            ]
            if filtered:
                ui_spec["layers"] = filtered

        # Fallback: scan tool returns for explicit "Saved to outputs/foo.gpkg" lines
        if not ui_spec or not ui_spec.get("layers"):
            saved = _re.findall(
                r"[Ss]aved to outputs/([\w\-]+\.gpkg)", " ".join(all_content)
            )
            if saved:
                seen = {}
                for fname in saved:
                    seen[fname] = {
                        "name": fname.replace("_", " ").replace(".gpkg", ""),
                        "file": f"outputs/{fname}",
                    }
                coord_spec = infer_ui_spec_from_text(" ".join(assistant_texts))
                center = coord_spec.get("center") if coord_spec else None
                ui_spec = {"type": "map", "layers": list(seen.values())}
                if center:
                    ui_spec["center"] = center
                    ui_spec["zoom"] = 13
    if ui_spec:
        yield ("ui_spec", ui_spec)


def render_agui_event(encoder: EventEncoder, kind: str, payload) -> str:
    """Render one normalized (kind, payload) event as AG-UI SSE frame(s)."""
    if kind == "keepalive":
        return ": keepalive\n\n"
    if kind == "text":
        # one message_id per assistant message: start, content, end
        message_id = str(uuid.uuid4())
        return (
            encoder.encode(
                TextMessageStartEvent(message_id=message_id, role="assistant")
            )
            + encoder.encode(
                TextMessageContentEvent(message_id=message_id, delta=payload)
            )
            + encoder.encode(TextMessageEndEvent(message_id=message_id))
        )
    if kind == "progress":
        return encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="progress", value={"text": payload})
        )
    if kind == "viewer_cmd":
        return encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="viewer_cmd", value=payload)
        )
    if kind == "ui_spec":
        return encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="ui_spec", value=payload)
        )
    if kind == "plan":
        return encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="plan", value=payload)
        )
    if kind == "run":
        return encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="run", value=payload)
        )
    if kind == "error":
        return encoder.encode(
            RunErrorEvent(type=EventType.RUN_ERROR, message=str(payload))
        )
    return ""


async def agui_stream(events, thread_id: str, run_id: str, accept: str | None = None):
    """Render a normalized (kind, payload) async stream as AG-UI SSE.

    RUN_STARTED first, RUN_FINISHED last (replaces the legacy done).
    """
    encoder = EventEncoder(accept=accept)
    yield encoder.encode(
        RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id)
    )
    # RUN_ERROR is terminal in AG-UI: nothing may follow it, so an errored run
    # must not fall through to RUN_FINISHED
    errored = False
    try:
        async for kind, payload in events:
            yield render_agui_event(encoder, kind, payload)
            if kind == "error":
                errored = True
                break
    except Exception as e:
        errored = True
        yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(e)))
    if not errored:
        yield encoder.encode(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id
            )
        )


@app.post("/chat/agui")
async def chat_agui(input: RunAgentInput, request: Request):
    """AG-UI event endpoint: the agent pipeline rendered as AG-UI SSE."""
    user_messages = [m for m in input.messages if getattr(m, "role", None) == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message in input")
    prompt = user_messages[-1].content or ""

    return StreamingResponse(
        agui_stream(
            agent_event_stream(
                prompt, user_token=bearer_token(request.headers.get("authorization"))
            ),
            thread_id=input.thread_id,
            run_id=input.run_id,
            accept=request.headers.get("accept"),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── serving a file by name, without leaving the tree ─────────────────


def allowed_roots() -> list[str]:
    """Directories a served file may resolve into.

    user_data subdirs are deliberately not listed. Confinement to the parents
    covers them, and a subdir that is itself a symlink must not be able to
    widen the boundary.
    """
    return [OUTPUTS_DIR, str(USER_DATA_DIR), EXEC_DIR]


def name_candidates(filename: str) -> list[str]:
    """The name as given, plus the extension-swapped form the viewer links to."""
    stem, ext = os.path.splitext(filename)
    return [filename, stem if ext else filename + ".gpkg"]


def resolve_under(names, search_dirs, roots) -> str | None:
    """First of `names` found in `search_dirs`, confined to `roots`.

    Both sides are resolved before the comparison, so a `..` segment, an
    absolute name, and a symlink pointing out of the tree all miss. Directory
    order beats name order, which is the lookup the viewer already links
    against.
    """
    resolved_roots = [Path(root).resolve() for root in roots]
    for directory in search_dirs:
        for name in names:
            if not name:
                continue
            # an absolute name swallows the directory, and is then out of tree
            candidate = (Path(directory) / name).resolve()
            if not any(candidate.is_relative_to(r) for r in resolved_roots):
                continue
            if candidate.exists():
                return str(candidate)
    return None


@app.get("/outputs/{filename}")
async def get_output(filename: str):
    path = resolve_under([filename], [OUTPUTS_DIR], [OUTPUTS_DIR])
    if not path:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.get("/download/{filename}")
async def download_file(filename: str):
    """Download an output file as an attachment."""
    safe = os.path.basename(filename)
    path = os.path.join(OUTPUTS_DIR, safe)
    if not os.path.exists(path):
        # Try without/with .gpkg extension
        stem, ext = os.path.splitext(safe)
        alt = stem if ext else safe + ".gpkg"
        alt_path = os.path.join(OUTPUTS_DIR, alt)
        if os.path.exists(alt_path):
            path = alt_path
            safe = alt
        else:
            raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=safe, media_type="application/octet-stream")


@app.get("/geojson/{filename:path}")
async def get_geojson(filename: str):
    """Convert a vector file (GPKG, SHP, GeoJSON) to GeoJSON for Leaflet."""
    import geopandas as gpd

    # Search in outputs, user_data (and subdirs), and natural_earth directories
    user_data_subdirs = (
        [str(p) for p in USER_DATA_DIR.rglob("*") if p.is_dir()]
        if USER_DATA_DIR.exists()
        else []
    )
    search_dirs = [
        OUTPUTS_DIR,
        str(USER_DATA_DIR),
        *user_data_subdirs,
        EXEC_DIR,
        os.path.join(EXEC_DIR, "natural_earth"),
        os.path.join(EXEC_DIR, "natural_earth_110m"),
        os.path.join(EXEC_DIR, "natural_earth_50m"),
        os.path.join(EXEC_DIR, "natural_earth_10m"),
    ]

    path = resolve_under(name_candidates(filename), search_dirs, allowed_roots())

    if not path:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    try:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        return JSONResponse(content=json.loads(gdf.to_json()))
    except Exception:
        # the reader quotes the absolute path and the byte it choked on, so the
        # reason is logged rather than returned
        logger.exception(f"GeoJSON conversion failed for {filename}")
        raise HTTPException(status_code=500, detail="Failed to convert to GeoJSON")


@app.get("/datasets")
async def get_datasets():
    return load_catalogue()


@app.post("/upload")
async def upload_dataset(file: UploadFile = File(...)):
    import geopandas as gpd
    import pandas as pd

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename).suffix.lower()
    stem = Path(file.filename).stem
    content = await file.read()

    raw_path = USER_DATA_DIR / file.filename
    with open(raw_path, "wb") as f:
        f.write(content)

    data_path = raw_path

    # Unzip shapefile bundles or GPKG zips
    if suffix == ".zip":
        extract_dir = USER_DATA_DIR / stem
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(raw_path) as z:
            z.extractall(extract_dir)
        raw_path.unlink()
        shp_files = list(extract_dir.rglob("*.shp"))
        gpkg_files = list(extract_dir.rglob("*.gpkg"))
        if shp_files:
            data_path = shp_files[0]
            stem = data_path.stem
        elif gpkg_files:
            data_path = gpkg_files[0]
            stem = data_path.stem
        else:
            raise HTTPException(400, "No supported file found in zip")

    # Convert CSV with lat/lon columns to GPKG
    if suffix == ".csv":
        df = pd.read_csv(data_path)
        lat_col = next(
            (c for c in df.columns if c.lower() in ("lat", "latitude", "y")), None
        )
        lon_col = next(
            (c for c in df.columns if c.lower() in ("lon", "lng", "longitude", "x")),
            None,
        )
        if not lat_col or not lon_col:
            data_path.unlink()
            raise HTTPException(
                400, f"CSV needs lat/lon columns. Found: {list(df.columns)}"
            )
        gdf = gpd.GeoDataFrame(
            df, geometry=gpd.points_from_xy(df[lon_col], df[lat_col]), crs="EPSG:4326"
        )
        gpkg_path = USER_DATA_DIR / f"{stem}.gpkg"
        gdf.to_file(gpkg_path, driver="GPKG")
        data_path.unlink()
        data_path = gpkg_path

    # Read metadata
    try:
        gdf = gpd.read_file(data_path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
            gdf.to_file(data_path, driver="GPKG")
        geom_types = gdf.geometry.geom_type.dropna().value_counts()
        cols = [c for c in gdf.columns if c != "geometry"]
        metadata = {
            "name": stem,
            "filename": data_path.name,
            "relative_path": str(data_path.relative_to(Path(EXEC_DIR))),
            "geometry_type": geom_types.index[0] if len(geom_types) else "Unknown",
            "crs": "EPSG:4326",
            "columns": cols,
            "bbox": list(map(float, gdf.total_bounds)),
            "row_count": int(len(gdf)),
            "uploaded_at": datetime.now().isoformat(),
        }
    except Exception as e:
        if data_path.exists():
            data_path.unlink()
        raise HTTPException(500, f"Could not read file: {e}")

    catalogue = load_catalogue()
    catalogue = [d for d in catalogue if d["name"] != stem]
    catalogue.append(metadata)
    save_catalogue(catalogue)

    # Notify the agent about the new dataset
    col_preview = ", ".join(cols[:10]) + ("..." if len(cols) > 10 else "")
    await notify_agent(
        f"[Dataset uploaded] '{stem}': {metadata['geometry_type']}, "
        f"{metadata['row_count']} features, CRS: EPSG:4326, columns: {col_preview}. "
        f"File path for tools: {metadata['relative_path']}"
    )

    return metadata


@app.get("/stats/{filename:path}")
async def get_stats(filename: str):
    """Return summary statistics for a vector layer."""
    import geopandas as gpd
    import numpy as np

    user_data_subdirs = (
        [str(p) for p in USER_DATA_DIR.rglob("*") if p.is_dir()]
        if USER_DATA_DIR.exists()
        else []
    )
    search_dirs = [OUTPUTS_DIR, str(USER_DATA_DIR), *user_data_subdirs, EXEC_DIR]

    path = resolve_under(name_candidates(filename), search_dirs, allowed_roots())

    if not path:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    try:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        count = int(len(gdf))
        geom_type = (
            gdf.geometry.geom_type.value_counts().index[0] if count else "Unknown"
        )

        # Area in km² for polygon layers
        area_km2 = None
        if "Polygon" in geom_type:
            try:
                gdf_proj = gdf.to_crs("EPSG:3857")
                area_km2 = round(float(gdf_proj.geometry.area.sum()) / 1e6, 2)
            except Exception:
                pass

        # Pick the most informative categorical column
        PRIORITY_COLS = [
            "cuisine",
            "amenity",
            "shop",
            "building",
            "leisure",
            "landuse",
            "type",
            "highway",
            "natural",
            "tourism",
            "office",
            "public_transport",
            "mode",
        ]
        cat_col = None
        for col in PRIORITY_COLS:
            if col in gdf.columns:
                nuniq = gdf[col].nunique()
                if 2 <= nuniq <= 50:
                    cat_col = col
                    break
        if not cat_col:
            for col in gdf.select_dtypes(include="object").columns:
                if col in ("geometry", "name", "id"):
                    continue
                nuniq = gdf[col].nunique()
                if 2 <= nuniq <= 30:
                    cat_col = col
                    break

        breakdown = None
        if cat_col:
            # Exclude NaN and stringified nulls before counting
            series = gdf[cat_col].dropna()
            series = series[
                ~series.astype(str)
                .str.lower()
                .isin({"nan", "none", "null", "<na>", ""})
            ]
            vc = series.value_counts().head(8)
            if not vc.empty:
                breakdown = {
                    "column": cat_col,
                    "values": [
                        {"label": str(k), "count": int(v)} for k, v in vc.items()
                    ],
                }

        # Top numeric columns (skip lat/lon, ids)
        numeric_stats = []
        skip_numeric = {
            "lat",
            "lon",
            "latitude",
            "longitude",
            "x",
            "y",
            "id",
            "osm_id",
            "fid",
            "objectid",
        }
        for col in gdf.select_dtypes(include="number").columns[:4]:
            if col.lower() in skip_numeric:
                continue
            s = gdf[col].dropna()
            if len(s) == 0:
                continue
            numeric_stats.append(
                {
                    "column": col,
                    "min": round(float(s.min()), 2),
                    "max": round(float(s.max()), 2),
                    "mean": round(float(s.mean()), 2),
                }
            )

        return {
            "count": count,
            "geometry_type": geom_type,
            "area_km2": area_km2,
            "breakdown": breakdown,
            "numeric": numeric_stats[:3],
        }
    except Exception:
        logger.exception(f"Stats failed for {filename}")
        raise HTTPException(status_code=500, detail="Could not read the layer")


@app.get("/health")
async def health():
    return {"status": "ok"}


class DrawRequest(BaseModel):
    geojson: dict
    name: str = "drawn_area"


@app.post("/draw")
async def save_drawn_area(request: DrawRequest):
    """Save a GeoJSON feature drawn by the user on the map to a GPKG in user_data/."""
    import geopandas as gpd
    import json as _json
    import re

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w]", "_", request.name.strip())[:30] or "drawn_area"
    gpkg_path = USER_DATA_DIR / f"{safe_name}.gpkg"

    try:
        gdf = gpd.GeoDataFrame.from_features(
            (
                request.geojson.get("features", [request.geojson])
                if request.geojson.get("type") == "FeatureCollection"
                else [request.geojson]
            ),
            crs="EPSG:4326",
        )
        if gdf.empty:
            raise ValueError("No features in drawn GeoJSON")
        gdf.to_file(gpkg_path, driver="GPKG")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not save drawn area: {e}")

    relative_path = str(gpkg_path.relative_to(Path(EXEC_DIR)))
    geom_types = gdf.geometry.geom_type.dropna().value_counts()
    metadata = {
        "name": safe_name,
        "filename": gpkg_path.name,
        "relative_path": relative_path,
        "geometry_type": geom_types.index[0] if len(geom_types) else "Unknown",
        "crs": "EPSG:4326",
        "columns": [],
        "bbox": list(map(float, gdf.total_bounds)),
        "row_count": int(len(gdf)),
        "uploaded_at": datetime.now().isoformat(),
    }

    catalogue = load_catalogue()
    catalogue = [d for d in catalogue if d["name"] != safe_name]
    catalogue.append(metadata)
    save_catalogue(catalogue)

    # Notify the agent about the drawn area
    bounds = gdf.total_bounds
    center_lon = round(float((bounds[0] + bounds[2]) / 2), 4)
    center_lat = round(float((bounds[1] + bounds[3]) / 2), 4)
    await notify_agent(
        f"[User drew a shape on the map] '{safe_name}': {metadata['geometry_type']}, "
        f"center lon={center_lon}, lat={center_lat}. "
        f"File path for tools: {relative_path}"
    )

    return metadata


class ExportPDFRequest(BaseModel):
    title: str = "GeoLang Analysis Report"
    summary: str = ""
    layers: list = []
    center: list = [20, 0]
    zoom: int = 10
    basemap: str = "osm"
    width: int = 1280
    height: int = 900


@app.post("/export-pdf")
async def export_pdf(request: ExportPDFRequest):
    """Generate a PDF report using a Playwright headless screenshot (captures real tile imagery)."""
    from playwright.async_api import async_playwright
    from urllib.parse import quote

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_filename = f"report_{timestamp}.pdf"
    pdf_path = os.path.join(OUTPUTS_DIR, pdf_filename)

    base_url = os.environ.get("APP_BASE_URL", "http://localhost:8080")
    layer_param = ",".join(os.path.basename(f) for f in request.layers)
    lat, lon = request.center[0], request.center[1]
    url = (
        f"{base_url}/?screenshot=1"
        f"&lat={lat}&lon={lon}&zoom={request.zoom}"
        f"&basemap={request.basemap}"
        f"&title={quote(request.title)}"
        f"&summary={quote(request.summary)}"
        + (f"&layers={layer_param}" if layer_param else "")
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                viewport={"width": request.width, "height": request.height}
            )
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2500)
            await page.pdf(
                path=pdf_path,
                width=f"{request.width}px",
                height=f"{request.height}px",
                print_background=True,
            )
            await browser.close()

        return {"pdf_filename": pdf_filename, "download_url": f"/download/{pdf_filename}"}

    except Exception as e:
        logger.error(f"PDF export failed: {e}")
        raise HTTPException(status_code=500, detail=f"PDF export failed: {e}")


class ExportPNGRequest(BaseModel):
    center: list = [20, 0]
    zoom: int = 10
    layers: list = []
    width: int = 1280
    height: int = 800
    basemap: str = "osm"


@app.post("/export-png")
async def export_png(request: ExportPNGRequest):
    from playwright.async_api import async_playwright

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_filename = f"map_{timestamp}.png"
    png_path = os.path.join(OUTPUTS_DIR, png_filename)

    base_url = os.environ.get("APP_BASE_URL", "http://localhost:8080")
    layer_param = ",".join(os.path.basename(f) for f in request.layers)
    lat, lon = request.center[0], request.center[1]
    url = (
        f"{base_url}/?screenshot=1"
        f"&lat={lat}&lon={lon}&zoom={request.zoom}"
        f"&basemap={request.basemap}"
        + (f"&layers={layer_param}" if layer_param else "")
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                viewport={"width": request.width, "height": request.height}
            )
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2500)
            map_el = await page.query_selector("#map")
            if map_el:
                await map_el.screenshot(path=png_path)
            else:
                await page.screenshot(path=png_path, full_page=False)
            await browser.close()

        return {"png_filename": png_filename, "download_url": f"/download/{png_filename}"}
    except Exception as e:
        logger.error(f"PNG export failed: {e}")
        raise HTTPException(status_code=500, detail=f"PNG export failed: {e}")


# ── Session management (proxied to sibyl) ────────────────────────────


class RenameSessionRequest(BaseModel):
    name: str


class SwitchSessionRequest(BaseModel):
    session_id: str


@app.get("/sessions")
async def list_sessions():
    response = await sibyl_request("GET", "/sessions")
    return response.json()


@app.post("/sessions/new")
async def create_session():
    """Create a new session and make it the active one."""
    existing = (await sibyl_request("GET", "/sessions")).json()
    name = f"Session {len(existing) + 1}"
    created = (await sibyl_request("POST", "/sessions", json={"name": name})).json()
    return {"id": created["id"], "name": created["name"]}


@app.post("/sessions/switch")
async def switch_session(request: SwitchSessionRequest):
    """Switch to an existing session."""
    response = await sibyl_request(
        "POST", f"/sessions/{request.session_id}/activate"
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    return response.json()


@app.put("/sessions/{session_id}/rename")
async def rename_session(session_id: str, request: RenameSessionRequest):
    response = await sibyl_request(
        "PATCH", f"/sessions/{session_id}", json={"name": request.name}
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"id": session_id, "name": request.name}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    response = await sibyl_request("DELETE", f"/sessions/{session_id}")
    if response.status_code == 400:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the active session. Switch to another first.",
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    return response.json()


# ── Model selection (proxied to sibyl) ───────────────────────────────


def _sibyl_passthrough(response: httpx.Response) -> Response:
    """Hand sibyl's status and body back untouched."""
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


@app.get("/models")
async def list_models():
    """Sibyl's model profiles and which one is active."""
    return _sibyl_passthrough(
        await sibyl_request("GET", "/models")
    )


@app.put("/model")
async def set_model(request: Request):
    """Switch sibyl's active model. 404 unknown profile, 409 not available."""
    return _sibyl_passthrough(
        await sibyl_request(
            "PUT", "/model", json=await request.json()
        )
    )


@app.get("/debug/tools")
def debug_tools():
    """Names of the tools geolang serves to sibyl."""
    return {"tools": [func.__name__ for func, _ in load_external_tools()]}


class ShareRequest(BaseModel):
    title: str = "GeoLang Analysis"
    summary: str = ""
    layers: list = []
    center: list = []
    zoom: int = 12


@app.post("/share")
async def create_share(request: ShareRequest):
    """Create a shareable snapshot of the current map state."""
    import uuid

    share_id = str(uuid.uuid4())[:8]
    shares = load_shares()
    shares[share_id] = {
        "title": request.title,
        "summary": request.summary,
        "layers": request.layers,
        "center": request.center,
        "zoom": request.zoom,
        "created_at": datetime.now().isoformat(),
    }
    save_shares(shares)
    return {"share_id": share_id, "url": f"/share/{share_id}"}


@app.get("/share/{share_id}/data")
async def get_share_data(share_id: str):
    """Return share metadata as JSON (for the client to reconstruct the view)."""
    shares = load_shares()
    if share_id not in shares:
        raise HTTPException(status_code=404, detail="Share not found")
    return shares[share_id]


@app.get("/share/{share_id}")
async def view_share(share_id: str):
    """Serve the app so the client JS can load the share by reading the URL path."""
    shares = load_shares()
    if share_id not in shares:
        raise HTTPException(status_code=404, detail="Share not found")
    static_dir = Path(__file__).parent.parent / "static"
    return FileResponse(str(static_dir / "index.html"))


@app.get("/")
async def index():
    static_dir = Path(__file__).parent.parent / "static"
    return FileResponse(str(static_dir / "index.html"))


# Mount static assets (JS, CSS, etc.)
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
