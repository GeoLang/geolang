"""
TileTopia tileset discovery tool.

Lists 3D tilesets hosted by the platform's TileTopia service (ingested assets)
and its open data catalog, with viewer-loadable URLs for the viewer's tileset
action (the viewer reaches TileTopia through the same-origin /tiles/ proxy).
"""
from pydantic import BaseModel, Field
from typing import Optional


class ListTilesetsArgs(BaseModel):
    search: Optional[str] = Field(
        None, description="Optional name filter for hosted assets."
    )
    category: Optional[str] = Field(
        None,
        description=(
            "Optional catalog category filter: terrain, buildings, imagery, "
            "pointcloud, vector, or weather."
        ),
    )


def list_tilesets(search: str = None, category: str = None) -> str:
    """
    List 3D tilesets available from the platform's TileTopia service: hosted
    assets (ingested 3D Tiles) and the open data catalog. Returns viewer-loadable
    URLs. To display one, call viewer_control with action='run' and the viewer
    action that adds a tileset from a url, with the listed url. Use this when the user asks what 3D layers, tilesets, terrain,
    or buildings are available to show.
    """
    import os
    import traceback

    from src.core.user_token import service_headers

    base_url = os.environ.get("TILETOPIA_URL", "http://tiletopia:3000").rstrip("/")
    headers = service_headers()

    try:
        import requests

        lines = []

        params = {"q": search} if search else {}
        resp = requests.get(
            f"{base_url}/api/v1/assets", params=params, headers=headers, timeout=15
        )
        resp.raise_for_status()
        assets = resp.json()
        if assets:
            lines.append("Hosted tilesets (TileTopia assets):")
            for a in assets:
                asset_id = a.get("id")
                name = a.get("name") or asset_id
                status = a.get("status", "")
                # the browser reaches tiletopia via the /tiles/ same-origin proxy
                url = f"/tiles/v1/assets/{asset_id}/tileset.json"
                lines.append(f"  • {name} (id={asset_id}, status={status}) → url: {url}")

        cat_params = {"category": category} if category else {}
        resp = requests.get(
            f"{base_url}/api/v1/catalog", params=cat_params, headers=headers, timeout=15
        )
        resp.raise_for_status()
        catalog = [d for d in resp.json() if d.get("enabled", True)]
        if catalog:
            lines.append("Open data catalog:")
            for d in catalog:
                lines.append(
                    f"  • {d.get('name')} ({d.get('category')}, {d.get('provider')}) "
                    f"→ url: {d.get('url')}"
                )

        if not lines:
            return "No tilesets available: TileTopia has no hosted assets or catalog entries."

        lines.append(
            "Load one with viewer_control(action='run') and the viewer action that "
            "adds a tileset from a url."
        )
        return "\n".join(lines)

    except Exception as e:
        return f"TileTopia listing failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = list_tilesets
TOOL_SCHEMA = ListTilesetsArgs
