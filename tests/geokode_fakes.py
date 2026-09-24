from types import SimpleNamespace

from src.core.place_lookup import OVERPASS_URL

GEOKODE_URL = "http://geokode.test"


def json_response(body, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        ok=status_code < 400,
        raise_for_status=lambda: None,
        json=lambda: body,
    )


def geokode_hit(lat, lon, **fields):
    hit = {
        "name": None,
        "display_name": "",
        "address": {
            "house_number": None,
            "street": None,
            "city": None,
            "state": None,
            "postcode": None,
            "country": None,
            "full": None,
        },
        "country_code": None,
        "lat": lat,
        "lon": lon,
        "bbox": None,
        "kind": "place",
        "osm_type": None,
        "osm_id": None,
        "osm_key": None,
        "osm_value": None,
        "admin_level": None,
        "population": None,
        "confidence": 1.0,
        "match_type": "exact",
    }
    hit.update(fields)
    return hit


def overpass_way(role, *coordinates):
    geometry = [{"lat": lat, "lon": lon} for lon, lat in coordinates]
    return {"type": "way", "ref": 1, "role": role, "geometry": geometry}


def fake_platform(hits_for, overpass_elements=None, others=None):
    seen = SimpleNamespace(geokode=[], overpass=[], overpass_headers=[])

    def get(url, params=None, **kwargs):
        if url.startswith(GEOKODE_URL):
            seen.geokode.append(params["q"])
            return json_response({"results": hits_for(params["q"])[: params["limit"]]})
        if others is None:
            raise AssertionError(f"unexpected GET {url}")
        return others.get(url, params=params, **kwargs)

    def post(url, json=None, data=None, **kwargs):
        if url.startswith(GEOKODE_URL):
            seen.geokode.extend(json["queries"])
            answers = [hits_for(query)[: json["limit"]] for query in json["queries"]]
            return json_response({"results": answers})
        if url == OVERPASS_URL:
            seen.overpass.append(data["data"])
            seen.overpass_headers.append(kwargs.get("headers") or {})
            return json_response({"elements": overpass_elements or []})
        if others is None:
            raise AssertionError(f"unexpected POST {url}")
        return others.post(url, json=json, **kwargs)

    return SimpleNamespace(get=get, post=post, seen=seen)
