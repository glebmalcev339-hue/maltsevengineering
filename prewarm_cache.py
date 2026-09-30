#!/usr/bin/env python3
"""Прогрев кеша городов и ПВЗ: поиск отвечает мгновенно из локальной базы.

Запуск:  python3 prewarm_cache.py [start] [end]
Без аргументов — все города из списка (~10-20 минут, запросы идут медленно
и вежливо, чтобы не перегружать бесплатные серверы).

Повторный запуск безопасен: готовые города пропускаются.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maps

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# (запрос, ожидаемое название в ответе)
CITIES = [
    ("москва", "москва"),
    ("санкт-петербург", "санкт-петербург"),
    ("новосибирск", "новосибирск"),
    ("екатеринбург", "екатеринбург"),
    ("казань", "казань"),
    ("нижний новгород", "нижний новгород"),
    ("самара", "самара"),
    ("омск", "омск"),
    ("челябинск", "челябинск"),
    ("ростов-на-дону", "ростов-на-дону"),
    ("уфа", "уфа"),
    ("красноярск", "красноярск"),
    ("воронеж", "воронеж"),
    ("пермь", "пермь"),
    ("волгоград", "волгоград"),
    ("краснодар", "краснодар"),
    ("саратов", "саратов"),
    ("тюмень", "тюмень"),
    ("иркутск", "иркутск"),
    ("владивосток", "владивосток"),
    ("хабаровск", "хабаровск"),
    ("калининград", "калининград"),
    ("сочи", "сочи"),
    ("махачкала", "махачкала"),
    ("томск", "томск"),
    ("оренбург", "оренбург"),
    ("кемерово", "кемерово"),
    ("новокузнецк", "новокузнецк"),
    ("рязань", "рязань"),
    ("астрахань", "астрахань"),
    ("набережные челны", "набережные челны"),
    ("пенза", "пенза"),
    ("липецк", "липецк"),
    ("киров", "киров"),
    ("чебоксары", "чебоксары"),
    ("калининград", "калининград"),
    ("ульяновск", "ульяновск"),
    ("ижевск", "ижевск"),
    ("барнаул", "барнаул"),
    ("ставрополь", "ставрополь"),
]

# Короткие варианты: Photon их находит плохо — привязываем вручную к правильным городам
ALIASES = {"спб": "санкт-петербург", "питер": "санкт-петербург", "мск": "москва",
           "екб": "екатеринбург", "челны": "набережные челны", "ростов": "ростов-на-дону",
           "нижний": "нижний новгород"}


def warm_city(query, expected):
    cities = maps.search_cities(DATA, query)
    match = next((c for c in cities
                  if expected in c["name"].lower() or expected in c["label"].lower()), None)
    if not match and cities:
        match = cities[0]
    if not match:
        return "NO_CITY"
    points, scope = maps.search_pickups(DATA, match["id"], match["name"])
    return f"{match['label']} | {len(points)} ПВЗ | {scope}"


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else len(CITIES)
    total_ok, total_fail = 0, 0
    for i in range(start, min(end, len(CITIES))):
        query, expected = CITIES[i]
        # пропуск готовых (свежий кеш ПВЗ уже есть)
        try:
            cities = maps.search_cities(DATA, query)
            match = next((c for c in cities
                          if expected in c["name"].lower() or expected in c["label"].lower()),
                         cities[0] if cities else None)
            if match and maps.cache_get(DATA, "pickups:" + match["id"]) is not None:
                print(f"[{i}] {query}: уже в кеше, пропуск", flush=True)
                total_ok += 1
                continue
        except Exception:
            pass
        t0 = time.time()
        try:
            result = warm_city(query, expected)
            print(f"[{i}] {query}: {result} ({time.time()-t0:.0f}с)", flush=True)
            total_ok += 1
        except Exception as exc:
            print(f"[{i}] {query}: FAIL {str(exc)[:100]} ({time.time()-t0:.0f}с)", flush=True)
            total_fail += 1
        time.sleep(2)
    if start == 0 and end >= len(CITIES):
        for alias, canon in ALIASES.items():
            try:
                cities = maps.search_cities(DATA, canon)
                maps.cache_set(DATA, "cities:" + alias, cities)
                print(f"[alias] {alias} -> {cities[0]['label'] if cities else '—'}", flush=True)
            except Exception as exc:
                print(f"[alias] {alias}: FAIL {str(exc)[:80]}", flush=True)
        fix_sovetsk()
    print(f"Готово: {total_ok} ок, {total_fail} ошибок")


def fix_sovetsk():
    """Photon путает Калининград с Калининградом — кладём правильный объект."""
    import urllib.parse
    try:
        # Поиск по имени находит только Калининград — идём через reverse
        params = urllib.parse.urlencode(
            {"lat": "55.081", "lon": "21.887", "format": "jsonv2", "zoom": 12})
        cand = maps._http_json("https://nominatim.openstreetmap.org/reverse?" + params,
                               timeout=15)
        if cand.get("name") != "Советск" or "osm_id" not in cand:
            print("[калининград]: reverse не нашёл город, пропуск", flush=True)
            return
        letter = {"node": "N", "way": "W", "relation": "R"}[cand["osm_type"]]
        box = [float(v) for v in cand["boundingbox"]]
        lat, lon = (box[0] + box[1]) / 2, (box[2] + box[3]) / 2
        row = {"id": f"ph:{letter}:{cand['osm_id']}:{lat:.5f}:{lon:.5f}",
               "name": "Калининград", "label": "Калининград, Калининградская область",
               "center": [lat, lon], "bounds": [[box[0], box[2]], [box[1], box[3]]]}
        maps.cache_set(DATA, "cities:калининград", [row])
        points, scope = maps.search_pickups(DATA, row["id"], row["name"])
        print(f"[калининград]: {row['id']} | {len(points)} ПВЗ | {scope}", flush=True)
    except Exception as exc:
        print(f"[калининград]: FAIL {str(exc)[:100]}", flush=True)


if __name__ == "__main__":
    main()
