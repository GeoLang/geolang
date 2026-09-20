# OSM tag keys a caller may name on their own, meaning "everything carrying it"
OSM_TAG_KEYS = {
    "waterway",
    "natural",
    "landuse",
    "highway",
    "amenity",
    "shop",
    "building",
    "leisure",
    "office",
    "railway",
    "tourism",
    "boundary",
}

OSM_TAG_MAP = {
    "buildings": {"building": True},
    "residential": {"building": "residential"},
    "commercial": {"building": "commercial"},
    "industrial": {"building": "industrial"},
    "schools": {"amenity": "school"},
    "hospitals": {"amenity": "hospital"},
    "pharmacies": {"amenity": "pharmacy"},
    "clinics": {"amenity": "clinic"},
    "parks": {"leisure": "park"},
    "green_spaces": {"landuse": "grass"},
    "restaurants": {"amenity": "restaurant"},
    "cafes": {"amenity": "cafe"},
    "bars": {"amenity": "bar"},
    "shops": {"shop": True},
    "supermarkets": {"shop": "supermarket"},
    "offices": {"office": True},
    "parking": {"amenity": "parking"},
    "bus_stops": {"highway": "bus_stop"},
    "transit": {"public_transport": True},
    "amenities": {"amenity": True},
    "water": {"natural": "water"},
    "forests": {"landuse": "forest"},
    "rivers": {"waterway": "river"},
    "river": {"waterway": "river"},
    "streams": {"waterway": "stream"},
    "canals": {"waterway": "canal"},
    "waterways": {"waterway": True},
    "lakes": {"natural": "water"},
    "coastline": {"natural": "coastline"},
    "railways": {"railway": True},
}


def tags_for_category(category: str) -> dict:
    name = category.lower().strip()
    if "=" in name:
        key, value = name.split("=", 1)
        return {key.strip(): value.strip()}
    if name in OSM_TAG_MAP:
        return dict(OSM_TAG_MAP[name])
    if name in OSM_TAG_KEYS:
        # a bare key like "waterway": everything carrying it, not an amenity
        # named after it, which matches nothing at all
        return {name: True}
    return {"amenity": name}


def known_category(category: str) -> bool:
    name = category.lower().strip()
    return "=" in name or name in OSM_TAG_MAP or name in OSM_TAG_KEYS


def merge_tags(tag_groups: list[dict]) -> dict:
    merged = {}
    for tags in tag_groups:
        for key, value in tags.items():
            if merged.get(key) is True:
                continue
            if value is True:
                merged[key] = True
                continue
            values = list(value) if isinstance(value, list) else [value]
            existing = merged.get(key)
            existing = [] if existing is None else existing
            existing = existing if isinstance(existing, list) else [existing]
            merged[key] = existing + [v for v in values if v not in existing]
    return {
        key: value[0] if isinstance(value, list) and len(value) == 1 else value
        for key, value in merged.items()
    }


def features_matching_tags(frame, tags: dict):
    import pandas as pd

    mask = pd.Series(False, index=frame.index)
    for key, value in tags.items():
        column = frame.get(key)
        if column is None:
            continue
        if value is True:
            mask = mask | column.notna()
        elif isinstance(value, list):
            mask = mask | column.isin(value)
        else:
            mask = mask | (column == value)
    return frame[mask]
