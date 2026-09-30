#!/usr/bin/env python3
"""Maltsev Engineering — города и ПВЗ Ozon из открытых данных.

- Поиск городов, ПВЗ и обратный геокодинг: Photon (komoot), данные OSM.
- Запасной движок ПВЗ: Overpass API (данные OpenStreetMap, © OSM contributors, ODbL).
- Точные границы города для области поиска: Nominatim lookup (только геометрия,
  результат кешируется надолго, чтобы не дёргать публичный сервер).

Только стандартная библиотека. Кеш: data/cache.sqlite3.
"""
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "MaltsevEngineeringShop/5.0 (https://maltsev-engineering.ru)"}
PHOTON = "https://photon.komoot.io"
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
PLACE_VALUES = {"city", "town", "village", "hamlet", "municipality", "borough"}
CACHE_TTL = {"cities:": 30 * 86400, "geom:": 90 * 86400, "pickups:": 30 * 86400, "addr:": 90 * 86400}


class GeoError(Exception):
    pass


def _http_json(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:
        raise GeoError(str(exc) or "нет ответа")


# ---------------------------------------------------------------------- кеш

def _cache_conn(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "cache.sqlite3"), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)")
    return conn


def cache_get(data_dir, key, allow_stale=False):
    """Чтение кеша. allow_stale=True — вернуть даже просроченное (для аварийного режима)."""
    conn = _cache_conn(data_dir)
    try:
        row = conn.execute("SELECT value, expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if row[1] > time.time():
            return json.loads(row[0])
        if allow_stale:
            return json.loads(row[0])
        return None
    finally:
        conn.close()


def cache_set(data_dir, key, value):
    ttl = 86400
    for prefix, seconds in CACHE_TTL.items():
        if key.startswith(prefix):
            ttl = seconds
    conn = _cache_conn(data_dir)
    try:
        conn.execute("INSERT OR REPLACE INTO cache(key, value, expires_at) VALUES (?, ?, ?)",
                     (key, json.dumps(value, ensure_ascii=False), time.time() + ttl))
        conn.commit()
    finally:
        conn.close()


# -------------------------------------------------------------------- города

def _city_label(props, name):
    state = props.get("state") or props.get("county") or ""
    country = props.get("country") or ""
    parts = [name]
    if state and state != name:
        parts.append(state)
    if country and country not in ("Россия", "Russia"):
        parts.append(country)
    return ", ".join(parts)


def search_cities(data_dir, query):
    """Photon → [{id, name, label, center, bounds}]."""
    query = " ".join((query or "").split())
    if len(query) < 2:
        return []
    cache_key = "cities:" + query.lower()
    cached = cache_get(data_dir, cache_key)
    if cached is not None:
        return cached
    params = urllib.parse.urlencode({"q": query, "limit": 15})
    try:
        data = _http_json(f"{PHOTON}/api/?{params}", timeout=12)
    except GeoError:
        stale = cache_get(data_dir, cache_key, allow_stale=True)
        if stale is not None:
            return stale
        raise GeoError("Сервис городов не ответил вовремя. Повторите позже или укажите адрес ПВЗ вручную")
    cities = []
    seen = set()
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        if props.get("osm_key") != "place" or props.get("osm_value") not in PLACE_VALUES:
            continue
        geom = feature.get("geometry", {}).get("coordinates") or []
        if len(geom) != 2:
            continue
        lon, lat = float(geom[0]), float(geom[1])
        name = props.get("name") or props.get("city") or ""
        if not name:
            continue
        key = (props.get("osm_type"), props.get("osm_id"))
        if key in seen:
            continue
        seen.add(key)
        half = {"city": 0.12, "town": 0.06}.get(props.get("osm_value"), 0.03)
        dlon = half / max(0.3, math.cos(math.radians(lat)))
        city_id = f"ph:{props.get('osm_type')}:{props.get('osm_id')}:{lat:.5f}:{lon:.5f}"
        cities.append({
            "id": city_id,
            "name": name,
            "label": _city_label(props, name),
            "center": [lat, lon],
            "bounds": [[lat - half, lon - dlon], [lat + half, lon + dlon]],
        })
        if len(cities) >= 8:
            break
    cache_set(data_dir, cache_key, cities)
    return cities


def _parse_city_id(city_id):
    try:
        _, osm_type, osm_id, lat, lon = str(city_id).split(":")
        assert osm_type in ("N", "W", "R")
        return osm_type, int(osm_id), float(lat), float(lon)
    except (ValueError, AssertionError):
        raise GeoError("Неверный город, выберите его из поиска заново")


def city_geometry(data_dir, city_id):
    """Точные границы через Nominatim lookup (кеш 30 дней) или приближение."""
    osm_type, osm_id, lat, lon = _parse_city_id(city_id)
    cache_key = f"geom:{osm_type}:{osm_id}"
    cached = cache_get(data_dir, cache_key)
    if cached is not None:
        return cached
    params = urllib.parse.urlencode({"osm_ids": f"{osm_type}{osm_id}", "format": "jsonv2"})
    try:
        data = _http_json(f"{NOMINATIM}/lookup?{params}", timeout=12)
        box = (data[0].get("boundingbox") or []) if data else []
        south, north, west, east = (float(v) for v in box[:4])
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        geom = {"bounds": [[south, west], [north, east]], "center": [lat, lon], "exact": True}
    except Exception:
        half = 0.10
        dlon = half / max(0.3, math.cos(math.radians(lat)))
        geom = {"bounds": [[lat - half, lon - dlon], [lat + half, lon + dlon]],
                "center": [lat, lon], "exact": False}
    cache_set(data_dir, cache_key, geom)
    return geom


# ---------------------------------------------------------------------- ПВЗ

# Основной запрос: только точки с точными тегами — быстрый даже на больших городах.
_OVERPASS_QUERY = ('[out:json][timeout:55];'
                   '(node["brand"="Ozon"]({area});'
                   ' node["brand:wikidata"="Q2365235"]({area}););'
                   'out tags;')
# Добор: линии/отношения и точки без brand-тега. На больших городах может не
# успеть — тогда тихо пропускаем: основное покрытие уже получено выше.
_OVERPASS_QUERY_EXTRA = ('[out:json][timeout:25];'
                         '(way["brand"="Ozon"]({area});'
                         ' relation["brand"="Ozon"]({area});'
                         ' node["shop"="outpost"]["name"~"Ozon",i]({area});'
                         ' node["amenity"="parcel_locker"]["name"~"Ozon",i]({area}););'
                         'out center tags;')


def _try_plan(query, quick=False):
    """План попыток по зеркалам. Возвращает данные или бросает GeoError."""
    last_error = "нет ответа"
    if quick:
        plan = [(OVERPASS_URLS[0], 30)]
    else:
        plan = [(OVERPASS_URLS[0], 45), (OVERPASS_URLS[0], 60),
                (OVERPASS_URLS[1], 12), (OVERPASS_URLS[2], 12)]
    for attempt, (base, timeout) in enumerate(plan):
        if attempt:
            time.sleep(2)
        url = base + "?data=" + urllib.parse.quote(query)
        try:
            data = _http_json(url, timeout=timeout)
            remark = str(data.get("remark", "")).lower()
            if "rate_limited" in remark or "timed out" in remark \
                    or "timeout" in remark or "error" in remark:
                raise GeoError("Overpass перегружен, повтор...")
            return data
        except GeoError as exc:
            last_error = str(exc)
    raise GeoError(last_error)


def _overpass_tile(area):
    """Один тайл: основной запрос + одна быстрая перепроверка пустого ответа."""
    query = _OVERPASS_QUERY.format(area=area)
    try:
        data = _try_plan(query)
    except GeoError as exc:
        raise GeoError(f"База пунктов выдачи временно недоступна ({exc}). "
                       "Укажите проверенный адрес ПВЗ вручную")
    if not data.get("elements"):
        time.sleep(1)
        try:
            retry = _try_plan(query, quick=True)
            if retry.get("elements"):
                return retry
        except GeoError:
            pass
    return data


def _overpass_extra(area):
    """Добор линий/точек без brand-тега. Best effort: тихо пропускаем при неудаче."""
    try:
        return _http_json(OVERPASS_URLS[0] + "?data=" +
                          urllib.parse.quote(_OVERPASS_QUERY_EXTRA.format(area=area)),
                          timeout=25).get("elements", [])
    except GeoError:
        return []


def _split_area(south, west, north, east, max_span=0.25, max_tiles=9):
    """Режет большую область на тайлы: маленькие запросы быстрые и надёжные."""
    lat_tiles = min(3, max(1, math.ceil((north - south) / max_span)))
    lon_tiles = min(3, max(1, math.ceil((east - west) / max_span)))
    while lat_tiles * lon_tiles > max_tiles:
        if lon_tiles >= lat_tiles:
            lon_tiles -= 1
        else:
            lat_tiles -= 1
    tiles = []
    for i in range(lat_tiles):
        for j in range(lon_tiles):
            s = south + (north - south) * i / lat_tiles
            n = south + (north - south) * (i + 1) / lat_tiles
            w = west + (east - west) * j / lon_tiles
            e = west + (east - west) * (j + 1) / lon_tiles
            tiles.append((s, w, n, e))
    return tiles


def _point_address(tags):
    street = tags.get("addr:street") or tags.get("addr:place") or ""
    house = tags.get("addr:housenumber") or ""
    city = tags.get("addr:city") or tags.get("addr:suburb") or ""
    if street and house:
        addr = f"{street}, {house}"
    elif street:
        addr = street
    else:
        return None
    if city:
        addr += f", {city}"
    return addr


def _element_to_point(el, city_id, city_name):
    el_type = el.get("type")
    if el_type == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    tags = el.get("tags", {})
    name = tags.get("name") or "Ozon"
    address = _point_address(tags)
    hint = None
    if not address and name.lower() not in ("ozon", "озон", "ozon банк"):
        hint = name
    kind = {"node": "node", "way": "way", "relation": "relation"}.get(el_type, "node")
    return {"id": f"{kind}/{el.get('id')}", "city_id": city_id, "city": city_name,
            "lat": lat, "lon": lon, "name": name,
            "address": address, "address_hint": hint,
            "hours": tags.get("opening_hours"),
            "osm_url": f"https://www.openstreetmap.org/{kind}/{el.get('id')}"}


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * \
        math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _photon_pvz_query(lat, lon, query, bbox=None):
    params = {"q": query, "limit": 50}
    if bbox:
        params["bbox"] = bbox
    else:
        params["lat"] = f"{lat:.5f}"
        params["lon"] = f"{lon:.5f}"
    return _http_json(f"{PHOTON}/api/?{urllib.parse.urlencode(params)}",
                      timeout=15).get("features", [])


def _photon_pickups(data_dir, city_id, clat, clon):
    """ПВЗ через Photon: bias для маленьких городов, тайлы bbox для больших."""
    geom = city_geometry(data_dir, city_id)
    (south, west), (north, east) = geom["bounds"]
    if (north - south) <= 0.3 and (east - west) <= 0.3:
        jobs = [("Ozon", None), ("Озон", None)]
        radius_cap, total_cap = 30.0, 150
    else:
        jobs = []
        for (s, w, n, e) in _split_area(south, west, north, east,
                                        max_span=0.3, max_tiles=6):
            bbox = f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}"
            jobs.append(("Ozon", bbox))
            jobs.append(("Озон", bbox))
        radius_cap, total_cap = None, 600

    def fetch(job):
        query, bbox = job
        try:
            return _photon_pvz_query(clat, clon, query, bbox), False
        except GeoError:
            return [], True

    features, any_error = [], False
    with ThreadPoolExecutor(max_workers=3) as pool:
        for feats, failed in pool.map(fetch, jobs):
            features.extend(feats)
            any_error = any_error or failed
    if not features and any_error:
        raise GeoError("Photon не ответил")

    seen = set()
    scored = []
    for feature in features:
        props = feature.get("properties", {})
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        key = (props.get("osm_type"), props.get("osm_id"))
        if key in seen or not all(key):
            continue
        seen.add(key)
        plat, plon = float(coords[1]), float(coords[0])
        dist = _haversine_km(clat, clon, plat, plon)
        if radius_cap and dist > radius_cap:
            continue
        scored.append((dist, plat, plon, props))
    scored.sort(key=lambda row: row[0])
    points = []
    for _, plat, plon, props in scored[:total_cap]:
        kind = {"N": "node", "W": "way", "R": "relation"}.get(props.get("osm_type"), "node")
        osm_id = props["osm_id"]
        street = props.get("street") or ""
        house = props.get("housenumber") or ""
        city = props.get("city") or props.get("town") or props.get("village") or \
            props.get("district") or props.get("county") or ""
        name = props.get("name") or "Ozon"
        address, hint = None, None
        if street and house:
            address = f"{street}, {house}" + (f", {city}" if city else "")
        elif street:
            hint = street + (f", {city}" if city else "")
        elif name.lower() not in ("ozon", "озон", "озон банк", "ozon банк"):
            hint = name
        points.append({"id": f"{kind}/{osm_id}", "city_id": str(city_id), "city": city,
                       "lat": plat, "lon": plon, "name": name,
                       "address": address, "address_hint": hint, "hours": None,
                       "osm_url": f"https://www.openstreetmap.org/{kind}/{osm_id}"})
    return points


def _overpass_pickups(data_dir, city_id, city_name):
    """Запасной движок: Overpass по тайлам. Медленный, но полный."""
    geom = city_geometry(data_dir, city_id)
    (south, west), (north, east) = geom["bounds"]
    if geom.get("exact"):
        boxes = _split_area(south, west, north, east)
        areas = [f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}" for (s, w, n, e) in boxes]
        scope = "city_bbox"
    else:
        lat, lon = geom["center"]
        areas = [f"around:20000,{lat:.5f},{lon:.5f}"]
        scope = "radius_20km"
    elements = []
    seen_ids = set()
    for area in areas:
        for el in _overpass_tile(area).get("elements", []):
            key = (el.get("type"), el.get("id"))
            if key not in seen_ids:
                seen_ids.add(key)
                elements.append(el)
    if len(areas) == 1:
        for el in _overpass_extra(areas[0]):
            key = (el.get("type"), el.get("id"))
            if key not in seen_ids:
                seen_ids.add(key)
                elements.append(el)
    points = []
    for el in elements[:3000]:
        point = _element_to_point(el, str(city_id), city_name)
        if point:
            points.append(point)
    return points, scope


def search_pickups(data_dir, city_id, city_name=""):
    """ПВЗ Ozon в границах города. Возвращает (points, scope)."""
    cache_key = "pickups:" + str(city_id)
    cached = cache_get(data_dir, cache_key)
    if cached is not None:
        return cached["points"], cached["scope"]
    stale = cache_get(data_dir, cache_key, allow_stale=True)
    _, _, clat, clon = _parse_city_id(city_id)
    try:
        points = _photon_pickups(data_dir, city_id, clat, clon)
        scope = "nearest"
    except GeoError:
        try:
            points, scope = _overpass_pickups(data_dir, city_id, city_name)
        except GeoError:
            if stale is not None:
                return stale["points"], stale["scope"]
            raise GeoError("База пунктов выдачи временно недоступна. "
                           "Укажите проверенный адрес ПВЗ вручную")
    result = {"points": points, "scope": scope}
    cache_set(data_dir, cache_key, result)
    return points, scope


# ------------------------------------------------------ уточнение адреса точки

def pickup_address(data_dir, city_id, point_id):
    """Обратный геокодинг точки. Возвращает dict для фронтенда."""
    cache_key = f"addr:{point_id}"
    cached = cache_get(data_dir, cache_key)
    if cached is not None:
        return cached
    stale = cache_get(data_dir, cache_key, allow_stale=True)
    try:
        points, _ = search_pickups(data_dir, city_id)
    except GeoError:
        if stale is not None:
            return stale
        raise
    point = next((p for p in points if p["id"] == point_id), None)
    if not point:
        raise GeoError("Точка не найдена, выберите ПВЗ заново")
    params = urllib.parse.urlencode({"lat": point["lat"], "lon": point["lon"]})
    try:
        data = _http_json(f"{PHOTON}/reverse?{params}", timeout=12)
        props = (data.get("features") or [{}])[0].get("properties", {})
    except GeoError:
        if stale is not None:
            return stale
        raise GeoError("Не удалось уточнить адрес. Введите проверенный адрес этого ПВЗ вручную")
    street = props.get("street") or props.get("name") or ""
    house = props.get("housenumber") or ""
    city = props.get("city") or props.get("district") or ""
    parts = []
    if street:
        parts.append(street + (f", {house}" if house else ""))
    if city:
        parts.append(city)
    address = ", ".join(parts)
    if not address:
        raise GeoError("Не удалось уточнить адрес. Введите проверенный адрес этого ПВЗ вручную")
    result = {"address": address, "address_source": "nearby_osm_address", "estimated": True,
              "notice": "Показан адрес ближайшего к точке объекта. Обязательно проверьте его "
                        "и при нужде исправьте вручную."}
    cache_set(data_dir, cache_key, result)
    return result
