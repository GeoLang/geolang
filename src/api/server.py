import json
import logging
import os
import uuid

import asyncio
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

from letta_client import Letta

from src.agents.agent_manager import PERSONA, load_external_tools, register_tool
from src.agents.workflows import (
    extract_text_and_ui_spec,
    get_progress_text,
    infer_ui_spec_from_text,
    TOOL_MESSAGE_TYPES,
)
from src.core.utils import (
    AGENT_ID_FILE,
    CATALOGUE_FILE,
    EXEC_DIR,
    LETTA_URL,
    OUTPUTS_DIR,
    SESSIONS_FILE,
    USER_DATA_DIR,
    load_catalogue,
    load_sessions,
    load_shares,
    save_catalogue,
    save_sessions,
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

client = Letta(base_url=LETTA_URL, timeout=300)
agent_id: str = None




@app.on_event("startup")
async def startup():
    global agent_id

    # Always upsert tools so code changes take effect without resetting the agent
    tool_names = []
    for func, schema, helpers in load_external_tools():
        tool_obj = register_tool(client, func=func, args_schema=schema, helpers=helpers)
        tool_names.append(tool_obj.name)
    logger.info(f"Registered {len(tool_names)} tools: {tool_names}")

    # Resume existing agent if available (tools already updated above)
    if os.path.exists(AGENT_ID_FILE):
        with open(AGENT_ID_FILE) as f:
            saved_id = f.read().strip()
        try:
            client.agents.get(saved_id)
            agent_id = saved_id
            # Always sync persona so server.py changes take effect without resetting the agent
            try:
                blocks = client.agents.core_memory.retrieve(agent_id=agent_id)
                for block in blocks if isinstance(blocks, list) else [blocks]:
                    if getattr(block, "label", None) == "persona":
                        client.blocks.modify(block_id=block.id, value=PERSONA)
                        logger.info("Persona block updated on existing agent")
                        break
            except Exception as e:
                logger.warning(f"Could not update persona block: {e}")

            # Sync tool list so newly added tools are available without recreating the agent
            try:
                existing_tools = client.agents.tools.list(agent_id=agent_id)
                existing_names = {t.name for t in existing_tools}
                # Build name→id map from globally registered tools
                all_tools = client.tools.list()
                tool_id_map = {t.name: t.id for t in all_tools}
                for name in tool_names:
                    if name not in existing_names and name in tool_id_map:
                        client.agents.tools.attach(
                            tool_id=tool_id_map[name], agent_id=agent_id
                        )
                        logger.info(f"Attached new tool to agent: {name}")
            except Exception as e:
                logger.warning(f"Could not sync agent tools: {e}")

            logger.info(f"Resumed existing agent: {agent_id}")
            return
        except Exception:
            logger.info("Saved agent not found, creating new one")

    shared_block = client.blocks.create(
        label="gis_workflow",
        description="Shared data for GIS tasks (e.g., dataset paths, results).",
        value="No datasets yet.",
    )

    agent = client.agents.create(
        name="gis-agent",
        llm_config={
            "model": "grok-4-1-fast-reasoning",
            "model_endpoint_type": "openai",
            "model_endpoint": "https://api.x.ai/v1",
            "context_window": 131072,
        },
        embedding_config={
            "embedding_provider": "vllm",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_endpoint_type": "openai",
            "embedding_endpoint": os.environ.get("VLLM_API_BASE", "http://localhost:8000"),
            "embedding_dim": 384,
        },
        memory_blocks=[
            {"label": "persona", "value": PERSONA},
            {
                "label": "human",
                "value": "User needs geospatial analysis with GeoPandas and QGIS.",
            },
        ],
        block_ids=[shared_block.id],
        tools=tool_names,
    )

    agent_id = agent.id
    with open(AGENT_ID_FILE, "w") as f:
        f.write(agent_id)
    logger.info(f"Created agent: {agent_id}")



class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(request: ChatRequest):
    if not agent_id:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        response = client.agents.messages.create(
            agent_id=agent_id,
            messages=[{"role": "user", "content": request.message}],
        )
        text, ui_spec, viewer_commands = extract_text_and_ui_spec(response)
        return {"text": text, "ui_spec": ui_spec, "viewer_commands": viewer_commands}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def agent_event_stream(message: str):
    """Run the Letta agent stream and yield normalized (kind, payload) events.

    Single source of truth for the marker parsing. kinds:
    "text", "progress", "viewer_cmd", "ui_spec", "error", "keepalive".
    /chat/agui renders these as AG-UI events.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def run_in_thread():
        try:
            stream = client.agents.messages.stream(
                agent_id=agent_id,
                messages=[{"role": "user", "content": message}],
            )
            for event in stream:
                loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, {"__error__": str(e)})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    Thread(target=run_in_thread, daemon=True).start()

    all_content = []
    assistant_texts = []
    ui_spec = None

    while True:
        try:
            event = await asyncio.wait_for(q.get(), timeout=15.0)
        except asyncio.TimeoutError:
            # Send keepalive comment to prevent browser SSE timeout
            yield ("keepalive", None)
            continue
        if event is None:
            break

        if isinstance(event, dict) and "__error__" in event:
            yield ("error", event["__error__"])
            break

        msg_type = str(getattr(event, "message_type", "") or "")
        content = str(getattr(event, "content", "") or "")
        tool_call = getattr(event, "tool_call", None)
        tool_return = str(getattr(event, "tool_return", "") or "")

        # Debug: log every tool call and return
        if tool_call and msg_type in TOOL_MESSAGE_TYPES:
            tool_name = str(getattr(tool_call, "name", "") or "")
            tool_args = str(getattr(tool_call, "arguments", "") or "")
            logger.info(f"TOOL CALL: {tool_name}({tool_args[:200]})")
        if tool_return and tool_return.strip():
            logger.info(f"TOOL RETURN: {tool_return[:300]}")
            # Surface tool errors as progress events so the user sees them
            if tool_return.startswith("❌") or tool_return.startswith("ERROR"):
                short = tool_return[:200].split("\n")[0]
                yield ("progress", short)

        all_content.extend([content, tool_return])

        # Real-time tool progress
        if tool_call and msg_type in TOOL_MESSAGE_TYPES:
            tool_name = str(getattr(tool_call, "name", "") or "")
            tool_args = str(getattr(tool_call, "arguments", "") or "")
            yield ("progress", get_progress_text(tool_name, tool_args))

        # UI spec from emit_ui_spec tool — check both content and tool_return
        if ui_spec is None:
            for candidate in (tool_return, content):
                if "__UI_SPEC__:" in candidate:
                    try:
                        ui_spec = json.loads(candidate.split("__UI_SPEC__:", 1)[1])
                        break
                    except Exception:
                        pass

        # Viewer commands from viewer_control tool
        for candidate in (tool_return, content):
            if "__VIEWER_CMD__:" in candidate:
                for part in candidate.split("__VIEWER_CMD__:")[1:]:
                    try:
                        cmd = json.loads(part.split("\n")[0].strip())
                        yield ("viewer_cmd", cmd)
                    except Exception:
                        pass

        # Assistant text
        if msg_type in ("assistant_message", "assistant") and content:
            assistant_texts.append(content)
            yield ("text", content)

    if ui_spec is None:
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
    try:
        async for kind, payload in events:
            yield render_agui_event(encoder, kind, payload)
    except Exception as e:
        yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(e)))
    yield encoder.encode(
        RunFinishedEvent(
            type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id
        )
    )


@app.post("/chat/agui")
async def chat_agui(input: RunAgentInput, request: Request):
    """AG-UI event endpoint: the agent pipeline rendered as AG-UI SSE."""
    if not agent_id:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    user_messages = [m for m in input.messages if getattr(m, "role", None) == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message in input")
    prompt = user_messages[-1].content or ""

    return StreamingResponse(
        agui_stream(
            agent_event_stream(prompt),
            thread_id=input.thread_id,
            run_id=input.run_id,
            accept=request.headers.get("accept"),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/outputs/{filename}")
async def get_output(filename: str):
    path = os.path.join(OUTPUTS_DIR, filename)
    if not os.path.exists(path):
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

    path = None
    # Build candidate names: exact, without extension, with .gpkg added
    stem, ext = os.path.splitext(filename)
    candidates = [filename]
    if ext:
        candidates.append(stem)  # try without extension
    else:
        candidates.append(filename + ".gpkg")  # try with .gpkg

    for d in search_dirs:
        for name in candidates:
            candidate = os.path.join(d, name)
            if os.path.exists(candidate):
                path = candidate
                break
        if path:
            break

    if not path:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    try:
        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        return JSONResponse(content=json.loads(gdf.to_json()))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to convert to GeoJSON: {str(e)}"
        )


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
    if agent_id:
        col_preview = ", ".join(cols[:10]) + ("..." if len(cols) > 10 else "")
        summary = (
            f"[Dataset uploaded] '{stem}': {metadata['geometry_type']}, "
            f"{metadata['row_count']} features, CRS: EPSG:4326, columns: {col_preview}. "
            f"File path for tools: {metadata['relative_path']}"
        )
        try:
            client.agents.messages.create(
                agent_id=agent_id,
                messages=[{"role": "user", "content": summary}],
            )
        except Exception as e:
            logger.warning(f"Could not notify agent of upload: {e}")

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

    path = None
    stem, ext = os.path.splitext(filename)
    candidates = [filename]
    if ext:
        candidates.append(stem)
    else:
        candidates.append(filename + ".gpkg")

    for d in search_dirs:
        for name in candidates:
            candidate = os.path.join(d, name)
            if os.path.exists(candidate):
                path = candidate
                break
        if path:
            break

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "agent_id": agent_id}


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
    if agent_id:
        bounds = gdf.total_bounds
        center_lon = round(float((bounds[0] + bounds[2]) / 2), 4)
        center_lat = round(float((bounds[1] + bounds[3]) / 2), 4)
        summary = (
            f"[User drew a shape on the map] '{safe_name}': {metadata['geometry_type']}, "
            f"center lon={center_lon}, lat={center_lat}. "
            f"File path for tools: {relative_path}"
        )
        try:
            client.agents.messages.create(
                agent_id=agent_id,
                messages=[{"role": "user", "content": summary}],
            )
        except Exception as e:
            logger.warning(f"Could not notify agent of drawn area: {e}")

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


# ── Session management ────────────────────────────



def _ensure_current_session():
    """Make sure the current agent_id is tracked in sessions."""
    global agent_id
    if not agent_id:
        return
    sessions = load_sessions()
    if agent_id not in sessions:
        sessions[agent_id] = {
            "name": "Default",
            "created_at": datetime.now().isoformat(),
        }
        save_sessions(sessions)


class RenameSessionRequest(BaseModel):
    name: str


class SwitchSessionRequest(BaseModel):
    session_id: str


@app.get("/sessions")
async def list_sessions():
    _ensure_current_session()
    sessions = load_sessions()
    result = []
    for sid, info in sessions.items():
        result.append(
            {
                "id": sid,
                "name": info.get("name", "Unnamed"),
                "created_at": info.get("created_at", ""),
                "active": sid == agent_id,
            }
        )
    result.sort(key=lambda s: s["created_at"], reverse=True)
    return result


@app.post("/sessions/new")
async def create_session():
    """Create a new session (new Letta agent) and switch to it."""
    global agent_id

    sessions = load_sessions()

    # Create a new agent with the same config
    shared_block = client.blocks.create(
        label="gis_workflow",
        description="Shared data for GIS tasks (e.g., dataset paths, results).",
        value="No datasets yet.",
    )

    tool_names = []
    for func, schema, helpers in load_external_tools():
        tool_obj = register_tool(client, func=func, args_schema=schema, helpers=helpers)
        tool_names.append(tool_obj.name)

    agent = client.agents.create(
        name=f"gis-agent-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        llm_config={
            "model": "grok-4-1-fast-reasoning",
            "model_endpoint_type": "openai",
            "model_endpoint": "https://api.x.ai/v1",
            "context_window": 131072,
        },
        embedding_config={
            "embedding_provider": "vllm",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_endpoint_type": "openai",
            "embedding_endpoint": os.environ.get("VLLM_API_BASE", "http://localhost:8000"),
            "embedding_dim": 384,
        },
        memory_blocks=[
            {"label": "persona", "value": PERSONA},
            {
                "label": "human",
                "value": "User needs geospatial analysis with GeoPandas and QGIS.",
            },
        ],
        block_ids=[shared_block.id],
        tools=tool_names,
    )

    new_id = agent.id
    sessions[new_id] = {
        "name": f"Session {len(sessions) + 1}",
        "created_at": datetime.now().isoformat(),
    }
    save_sessions(sessions)

    agent_id = new_id
    with open(AGENT_ID_FILE, "w") as f:
        f.write(agent_id)

    return {"id": new_id, "name": sessions[new_id]["name"]}


@app.post("/sessions/switch")
async def switch_session(request: SwitchSessionRequest):
    """Switch to an existing session."""
    global agent_id
    sessions = load_sessions()
    if request.session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        client.agents.get(request.session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Agent no longer exists")

    agent_id = request.session_id
    with open(AGENT_ID_FILE, "w") as f:
        f.write(agent_id)

    return {"id": agent_id, "name": sessions[agent_id].get("name", "Unnamed")}


@app.put("/sessions/{session_id}/rename")
async def rename_session(session_id: str, request: RenameSessionRequest):
    sessions = load_sessions()
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    sessions[session_id]["name"] = request.name
    save_sessions(sessions)
    return {"id": session_id, "name": request.name}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and its Letta agent."""
    global agent_id
    if session_id == agent_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the active session. Switch to another first.",
        )

    sessions = load_sessions()
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    # Delete the Letta agent
    try:
        client.agents.delete(session_id)
    except Exception:
        pass  # Agent may already be gone

    del sessions[session_id]
    save_sessions(sessions)
    return {"deleted": session_id}


@app.get("/debug/tools")
async def debug_tools():
    """List tools attached to the current agent — for debugging tool registration."""
    if not agent_id:
        return {"error": "No agent"}
    try:
        tools = client.agents.tools.list(agent_id=agent_id)
        return {"agent_id": agent_id, "tools": [t.name for t in tools]}
    except Exception as e:
        return {"error": str(e)}


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
