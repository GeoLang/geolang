"""Sensor state on a live map, read from agora.

A feed writes readings into agora under the map document, and agora keeps the
latest value per asset and reading kind. This reads that state back, either now
or as of a moment in the past, so what the model answers with is what the viewer
is drawing.

The map comes from the `X-Agora-Document` header the call was bound with, so the
usual question needs no argument at all.
"""

from typing import Optional

from pydantic import BaseModel, Field

# how many assets one answer carries, so a document with thousands of them
# cannot fill the model's context
MAXIMUM_ASSETS_REPORTED = 200


class AssetReadingsArgs(BaseModel):
    document_id: Optional[str] = Field(
        None,
        description=(
            "Map document id. Defaults to the live map this call is bound to."
        ),
    )
    at: Optional[str] = Field(
        None,
        description=(
            "RFC 3339 time, such as 2026-08-25T03:00:00Z. Each asset's value as "
            "of then, and its liveness judged against then. Omit for now."
        ),
    )
    kind: Optional[str] = Field(
        None,
        description=(
            "Keep only this reading kind, such as temperature or humidity, and "
            "drop assets that do not report it."
        ),
    )
    asset_id: Optional[str] = Field(None, description="Keep only this one asset.")
    above: Optional[float] = Field(
        None, description="Keep assets whose value of kind is above this. Needs kind."
    )
    below: Optional[float] = Field(
        None, description="Keep assets whose value of kind is below this. Needs kind."
    )
    offline_only: bool = Field(
        False, description="Keep only the assets that have stopped reporting."
    )


def _values_by_kind(asset: dict) -> dict:
    values = {}
    for value in asset.get("values") or []:
        kind = value.get("kind")
        if isinstance(kind, str):
            values[kind] = {"value": value.get("value"), "at": value.get("at")}
    return values


def _within(value: object, above: float | None, below: float | None) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if above is not None and value <= above:
        return False
    return below is None or value < below


def _summary(
    asset_count: int,
    offline_count: int,
    match_count: int,
    narrowed: bool,
    kind: str | None,
    above: float | None,
    below: float | None,
) -> str:
    parts = [
        f"{asset_count} asset{'' if asset_count == 1 else 's'}",
        f"{offline_count} offline",
    ]
    bounds = []
    if above is not None:
        bounds.append(f"above {above}")
    if below is not None:
        bounds.append(f"below {below}")
    if bounds:
        parts.append(f"{match_count} {' and '.join(bounds)} {kind}")
    elif narrowed:
        parts.append(f"{match_count} matching")
    if match_count > MAXIMUM_ASSETS_REPORTED:
        parts.append(f"first {MAXIMUM_ASSETS_REPORTED} listed")
    return ", ".join(parts)


def asset_readings(
    document_id: str = None,
    at: str = None,
    kind: str = None,
    asset_id: str = None,
    above: float = None,
    below: float = None,
    offline_only: bool = False,
) -> str:
    """
    Read what the sensors on the live map are reporting: every asset, whether it
    is still reporting, and its latest value per reading kind.
    Use this for questions about current or past conditions on the map, such as
    which assets are over 30 degrees right now, what the cold room was doing at
    3am, or which sensors have stopped reporting.
    """
    import asyncio
    import json

    from src.core import agora
    from src.core.bound_document import current_bound_document
    from src.core.user_token import current_user_token

    document = document_id or current_bound_document()
    if not document:
        return (
            "ERROR: no live map is bound to this call. Ask from the map in the "
            "viewer, or pass document_id with the map's document id."
        )
    if (above is not None or below is not None) and not kind:
        return "ERROR: above and below need kind, the reading kind they compare."

    try:
        assets = asyncio.run(
            agora.document_assets(document, current_user_token(), at)
        )
    except agora.AgoraError as e:
        return f"ERROR: {e}"

    asset_count = len(assets)
    offline_count = sum(1 for asset in assets if not asset.get("online"))

    matched = []
    for asset in assets:
        if asset_id is not None and asset.get("asset") != asset_id:
            continue
        if offline_only and asset.get("online"):
            continue
        values = _values_by_kind(asset)
        if kind is not None:
            if kind not in values:
                continue
            values = {kind: values[kind]}
        if (above is not None or below is not None) and not _within(
            values[kind]["value"], above, below
        ):
            continue
        matched.append(
            {
                "asset": asset.get("asset"),
                "online": bool(asset.get("online")),
                "values": values,
            }
        )

    narrowed = kind is not None or asset_id is not None or offline_only
    answer = {
        "document_id": document,
        "at": at,
        "asset_count": asset_count,
        "offline_count": offline_count,
        "match_count": len(matched),
        "assets": matched[:MAXIMUM_ASSETS_REPORTED],
        "summary": _summary(
            asset_count, offline_count, len(matched), narrowed, kind, above, below
        ),
    }
    return json.dumps(answer, indent=2)


TOOL_FUNCTION = asset_readings
TOOL_SCHEMA = AssetReadingsArgs
