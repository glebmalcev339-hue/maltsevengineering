#!/usr/bin/env python3
"""Maltsev Engineering — production-сервер магазина (только stdlib).

Заказы в SQLite, админка по паролю, ПВЗ Ozon из открытых данных,
онлайн-оплата через ЮKassa (без ключей — наглядный mock-режим для проверки).

    python3 server.py --open            # локальный запуск + открыть магазин
    python3 server.py --host 127.0.0.1 --port 8080
    python3 server.py --reset-admin-password
"""
import argparse
import json
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maps
import orders as orders_mod
from payments_yookassa import YooKassa, YooKassaError

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
ACCESS_FILE = os.path.join(ROOT, "ADMIN_ACCESS.txt")
CONFIG_FILE = os.path.join(ROOT, "config.json")
CACHE_SEED = os.path.join(ROOT, "cache_seed.sqlite3")

BODY_LIMIT = 256 * 1024
IMPORT_BODY_LIMIT = 5 * 1024 * 1024

STATIC = {
    "/": ("market.html", "text/html; charset=utf-8", True),
    "/market.html": ("market.html", "text/html; charset=utf-8", True),
    "/admin.html": ("admin.html", "text/html; charset=utf-8", True),
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8", False),
    "/sitemap.xml": ("sitemap.xml", "application/xml; charset=utf-8", False),
    "/market.yml": ("market.yml", "application/xml; charset=utf-8", False),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", False),
    "/favicon.svg": ("favicon.svg", "image/svg+xml", False),
    "/icon-180.png": ("icon-180.png", "image/png", False),
    "/icon-512.png": ("icon-512.png", "image/png", False),
    "/og-cover.jpg": ("og-cover.jpg", "image/jpeg", False),
}

CONFIG = {"catalogs": {}, "instance_id": "", "yk": None, "public_base": "", "mock_pay": True}


# ------------------------------------------------------------ ограничения

class RateLimit:
    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key, max_n, window_sec):
        now = time.time()
        with self.lock:
            slot = self.hits.setdefault(key, [])
            slot[:] = [t for t in slot if t > now - window_sec]
            if len(slot) >= max_n:
                return False
            slot.append(now)
            return True


LIMITER = RateLimit()
OVERPASS_LOCK = threading.Lock()  # вежливые последовательные запросы к Overpass
MOCK_PAYMENTS = {}  # token -> {payment_id, number, amount_minor, created}
MOCK_LOCK = threading.Lock()


def client_ip(handler):
    return handler.headers.get("X-Forwarded-For", handler.client_address[0]).split(",")[0].strip()[:64]


# ----------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "MaltsevShop/5.0"

    def log_message(self, fmt, *args):  # тихие логи без тел запросов
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path.split('?')[0]} -> {args[0]}\n")

    # -- helpers ----------------------------------------------------
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message}, status=status)

    def read_json(self, limit=BODY_LIMIT):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return None, "Слишком большой запрос"
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, ""
        try:
            return json.loads(raw.decode("utf-8")), ""
        except Exception:
            return None, "Некорректный JSON"

    def bearer_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    def need_admin(self):
        if not orders_mod.check_session(CONFIG["db"], self.bearer_token()):
            self.send_error_json("Войдите в админку заново", status=401)
            return False
        return True

    # -- static -----------------------------------------------------
    def serve_static(self, path):
        entry = STATIC.get(path)
        if not entry:
            self.send_error_json("Не найдено", status=404)
            return
        filename, content_type, no_store = entry
        full = os.path.join(ROOT, filename)
        if not os.path.isfile(full):
            self.send_error_json("Не найдено", status=404)
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET --------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        ip = client_ip(self)
        if not LIMITER.allow("g:" + ip, 180, 60):
            self.send_error_json("Слишком много запросов, подождите минуту", status=429)
            return
        if path in STATIC or path == "/market_2.html":
            if path == "/market_2.html":
                self.send_error_json("Не найдено", status=404)
                return
            self.serve_static(path)
        elif path == "/api/cities":
            if not LIMITER.allow("cities:" + ip, 30, 60):
                self.send_error_json("Слишком частый поиск городов", status=429)
                return
            try:
                cities = maps.search_cities(DATA_DIR, (query.get("q") or [""])[0])
                self.send_json({"cities": cities})
            except maps.GeoError as exc:
                self.send_error_json(str(exc), status=502)
        elif path == "/api/pickups":
            if not LIMITER.allow("pickups:" + ip, 8, 60):
                self.send_error_json("Слишком частый поиск ПВЗ, подождите минуту", status=429)
                return
            city_id = (query.get("city_id") or [""])[0]
            if not city_id:
                self.send_error_json("Не указан город", status=400)
                return
            try:
                with OVERPASS_LOCK:
                    points, scope = maps.search_pickups(DATA_DIR, city_id)
                self.send_json({"points": points, "scope": scope})
            except maps.GeoError as exc:
                self.send_error_json(str(exc), status=502)
        elif path == "/api/pickup-address":
            try:
                result = maps.pickup_address(DATA_DIR, (query.get("city_id") or [""])[0],
                                             (query.get("point_id") or [""])[0])
                self.send_json(result)
            except maps.GeoError as exc:
                self.send_error_json(str(exc), status=502)
        elif path == "/api/payments/status":
            number = (query.get("order") or [""])[0]
            order = orders_mod.get_order_by_number(CONFIG["db"], number) if number else None
            if not order:
                self.send_error_json("Заказ не найден", status=404)
                return
            order = self.poll_live_payment(order)
            resp = {"ok": True, "order_number": number,
                    "payment_status": order["payment_status"],
                    "amount_minor": order["total_minor"]}
            with MOCK_LOCK:
                for token, mock in MOCK_PAYMENTS.items():
                    if mock["number"] == number and mock.get("status") == "pending":
                        resp["confirmation_url"] = self.mock_url(token)
            self.send_json(resp)
        elif path == "/pay/mock":
            self.serve_mock_page(query)
        elif path == "/api/admin/session":
            if not self.need_admin():
                return
            self.send_json({"ok": True, "version": orders_mod.API_VERSION})
        elif path == "/api/admin/orders/export":
            if not self.need_admin():
                return
            self.send_json(orders_mod.export_orders(CONFIG["db"], CONFIG["instance_id"]))
        elif path == "/api/admin/orders":
            if not self.need_admin():
                return
            archived = (query.get("archived") or ["0"])[0] == "1"
            try:
                page = int((query.get("page") or ["1"])[0])
            except ValueError:
                page = 1
            data = orders_mod.list_orders(CONFIG["db"], q=(query.get("q") or [""])[0][:100],
                                          status=(query.get("status") or [""])[0],
                                          archived=archived, page=page)
            data.update({"version": orders_mod.API_VERSION, "release": orders_mod.RELEASE,
                         "instance_id": CONFIG["instance_id"]})
            self.send_json(data)
        else:
            self.send_error_json("Не найдено", status=404)

    # -- POST -------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        ip = client_ip(self)
        if not LIMITER.allow("g:" + ip, 180, 60):
            self.send_error_json("Слишком много запросов, подождите минуту", status=429)
            return
        if path == "/api/orders":
            payload, error = self.read_json()
            if error:
                self.send_error_json(error)
                return
            try:
                receipt = orders_mod.create_order(CONFIG["db"], CONFIG["catalogs"], payload)
                self.send_json({"ok": True, "version": orders_mod.API_VERSION,
                                "release": orders_mod.RELEASE, "order": receipt})
            except orders_mod.OrderError as exc:
                self.send_error_json(str(exc))
        elif path == "/api/payments":
            payload, error = self.read_json()
            if error:
                self.send_error_json(error)
                return
            self.api_create_payment(payload.get("order_number", ""))
        elif path == "/api/payments/notification":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, BODY_LIMIT)) if length else b""
            self.api_notification(raw)
        elif path == "/api/pay/mock_confirm":
            payload, error = self.read_json()
            if error:
                self.send_error_json(error)
                return
            self.api_mock_confirm(payload)
        elif path == "/api/admin/login":
            if not LIMITER.allow("login:" + ip, 10, 900):
                self.send_error_json("Слишком много попыток входа. Подождите 15 минут", status=429)
                return
            payload, error = self.read_json()
            if error:
                self.send_error_json(error)
                return
            password = payload.get("password", "") if isinstance(payload, dict) else ""
            if not password or not orders_mod.check_password(CONFIG["db"], password):
                self.send_error_json("Неверный пароль", status=401)
                return
            self.send_json({"ok": True, "token": orders_mod.create_session(CONFIG["db"])})
        elif path == "/api/admin/logout":
            orders_mod.revoke_session(CONFIG["db"], self.bearer_token())
            self.send_json({"ok": True})
        elif path == "/api/admin/orders/import":
            if not self.need_admin():
                return
            payload, error = self.read_json(limit=IMPORT_BODY_LIMIT)
            if error:
                self.send_error_json(error)
                return
            entries = payload.get("orders", []) if isinstance(payload, dict) else []
            try:
                self.send_json(orders_mod.import_orders(CONFIG["db"], entries))
            except orders_mod.OrderError as exc:
                self.send_error_json(str(exc))
        else:
            self.send_error_json("Не найдено", status=404)

    # -- PATCH ------------------------------------------------------
    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/admin/orders/"):
            self.send_error_json("Не найдено", status=404)
            return
        if not self.need_admin():
            return
        try:
            order_id = int(parsed.path.rsplit("/", 1)[1])
        except ValueError:
            self.send_error_json("Неверный номер заказа")
            return
        payload, error = self.read_json()
        if error:
            self.send_error_json(error)
            return
        try:
            orders_mod.update_order(CONFIG["db"], order_id, payload)
            self.send_json({"ok": True})
        except orders_mod.OrderError as exc:
            self.send_error_json(str(exc))

    # -- платежи ----------------------------------------------------
    def poll_live_payment(self, order):
        """Сверка подвисшего платежа с ЮKassa. Вебхуки не обязательны."""
        if CONFIG["mock_pay"] or order["payment_status"] != "pending":
            return order
        payment_id = order.get("payment_id", "")
        if not payment_id or payment_id.startswith("mock_"):
            return order
        try:
            live = CONFIG["yk"].get_payment(payment_id)
        except Exception:
            return order  # ЮKassa недоступна — отдаём последние известные данные
        status = live.get("status", "")
        if status == "succeeded":
            amount = int(float(live.get("amount", {}).get("value", "0")) * 100)
            orders_mod.set_payment(CONFIG["db"], order["number"], payment_id,
                                   "succeeded", amount)
            return orders_mod.get_order_by_number(CONFIG["db"], order["number"]) or order
        if status == "canceled":
            orders_mod.set_payment(CONFIG["db"], order["number"], payment_id,
                                   "canceled", order["total_minor"])
            return orders_mod.get_order_by_number(CONFIG["db"], order["number"]) or order
        return order

    def mock_url(self, token):
        host = self.headers.get("Host", "localhost:8080")
        proto = "https" if self.headers.get("X-Forwarded-Proto", "") == "https" else "http"
        return f"{proto}://{host}/pay/mock?token={token}"

    def api_create_payment(self, number):
        order = orders_mod.get_order_by_number(CONFIG["db"], number) if number else None
        if not order:
            self.send_error_json("Заказ не найден", status=404)
            return
        if order["payment_status"] == "succeeded":
            self.send_error_json("Заказ уже оплачен", status=409)
            return
        if order["total_minor"] <= 0:
            self.send_error_json("У заказа нулевая сумма, оплата невозможна")
            return
        if not CONFIG["mock_pay"] and order["payment_status"] == "pending" \
                and order.get("payment_id") and not order["payment_id"].startswith("mock_"):
            try:
                live = CONFIG["yk"].get_payment(order["payment_id"])
                if live.get("status") == "pending":
                    conf = (live.get("confirmation") or {}).get("confirmation_url", "")
                    if conf:
                        self.send_json({"ok": True, "payment_id": order["payment_id"],
                                        "confirmation_url": conf, "status": "pending",
                                        "mode": "live"})
                        return
                elif live.get("status") == "succeeded":
                    amount = int(float(live.get("amount", {}).get("value", "0")) * 100)
                    orders_mod.set_payment(CONFIG["db"], number, order["payment_id"],
                                           "succeeded", amount)
                    self.send_error_json("Заказ уже оплачен", status=409)
                    return
                # canceled/waiting — ниже создадим новый платёж со свежим ключом
            except Exception:
                pass
        if CONFIG["mock_pay"]:
            token = secrets.token_urlsafe(24)
            payment_id = "mock_" + secrets.token_urlsafe(8)
            with MOCK_LOCK:
                MOCK_PAYMENTS[token] = {"payment_id": payment_id, "number": number,
                                        "amount_minor": order["total_minor"],
                                        "status": "pending", "created": time.time()}
            orders_mod.set_payment(CONFIG["db"], number, payment_id, "pending", order["total_minor"])
            self.send_json({"ok": True, "payment_id": payment_id,
                            "confirmation_url": self.mock_url(token),
                            "status": "pending", "mode": "mock"})
            return
        try:
            items = [{"description": it["name"][:128], "quantity": it["quantity"],
                      "amount_minor": it["price_minor"]} for it in order["items"]]
            customer = order["customer"] or {}
            pay = CONFIG["yk"].create_payment(
                order_number=number, amount_minor=order["total_minor"],
                description=f"Заказ {number} — Maltsev Engineering",
                return_url=CONFIG["public_base"].rstrip("/") + "/market.html",
                email=customer.get("email", ""), phone=customer.get("phone", ""),
                items=items, idempotence_key=f"{number}-{order['id']}-{int(time.time())}"
                if order.get("payment_id") else
                f"{number}-{order['id']}")
            orders_mod.set_payment(CONFIG["db"], number, pay["payment_id"], "pending",
                                   order["total_minor"])
            self.send_json({"ok": True, "payment_id": pay["payment_id"],
                            "confirmation_url": pay["confirmation_url"],
                            "status": "pending", "mode": "live"})
        except YooKassaError as exc:
            self.send_error_json(f"ЮKassa: {exc}", status=502)
        except Exception:
            self.send_error_json("Платёжный сервис временно недоступен. Заказ сохранён, "
                                 "попробуйте оплату позже из письма или свяжитесь с продавцом", status=502)

    def api_notification(self, raw):
        if CONFIG["mock_pay"] or not raw:
            self.send_error_json("Уведомления выключены", status=404)
            return
        try:
            parsed = YooKassa.parse_notification(raw)
        except YooKassaError as exc:
            self.send_error_json(str(exc))
            return
        order = None
        if parsed.get("order_number"):
            order = orders_mod.get_order_by_number(CONFIG["db"], parsed["order_number"])
        if not order and parsed.get("payment_id"):
            order = orders_mod.find_by_payment_id(CONFIG["db"], parsed["payment_id"])
        if not order:
            self.send_error_json("Заказ не найден", status=404)
            return
        try:
            trusted = CONFIG["yk"].confirm_notification(
                raw, expected_amount_minor=order["total_minor"],
                expected_order_number=order["number"])
        except YooKassaError as exc:
            self.send_error_json(f"Проверка не пройдена: {exc}")
            return
        status = trusted["status"]
        if status == "succeeded":
            orders_mod.set_payment(CONFIG["db"], order["number"], trusted["payment_id"],
                                   "succeeded", trusted["amount_minor"])
        elif status == "canceled":
            orders_mod.set_payment(CONFIG["db"], order["number"], trusted["payment_id"],
                                   "canceled", order["total_minor"])
        self.send_json({"ok": True})

    # -- mock-страница ----------------------------------------------
    def serve_mock_page(self, query):
        if not CONFIG["mock_pay"]:
            self.send_error_json("Не найдено", status=404)
            return
        token = (query.get("token") or [""])[0]
        with MOCK_LOCK:
            mock = MOCK_PAYMENTS.get(token)
        if not mock or mock.get("status") != "pending":
            html = "<!doctype html><html lang=ru><meta charset=utf-8><meta name=robots content=noindex>"
            html += "<title>Ссылка недействительна</title><body style='font-family:sans-serif;padding:40px'>"
            html += "<h1>Ссылка недействительна</h1><p>Платёж уже завершён или ссылка устарела. "
            html += "Вернитесь в магазин и получите новую ссылку на оплату.</p></body></html>"
            body = html.encode("utf-8")
            self.send_response(410)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        amount = f"{mock['amount_minor'] / 100:,.2f}".replace(",", " ").replace(".", ",")
        html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Демо-оплата {mock['number']}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f6f8;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:16px}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;width:100%;
box-shadow:0 8px 32px rgba(0,0,0,.12);text-align:center}}
.badge{{display:inline-block;background:#fff3cd;color:#856404;border-radius:8px;
padding:4px 12px;font-size:13px;margin-bottom:12px}}
.amount{{font-size:34px;font-weight:700;margin:8px 0 4px}}
button{{width:100%;padding:14px;margin-top:12px;font-size:17px;border:none;border-radius:10px;cursor:pointer}}
.pay{{background:#0a0;color:#fff}} .cancel{{background:#eee;color:#333}}
small{{color:#666}}</style></head><body><div class="card">
<div class="badge">ДЕМО-РЕЖИМ · денег нет</div>
<h2>Заказ {mock['number']}</h2><div class="amount">{amount} ₽</div>
<small>В боевой версии здесь будет страница ЮKassa</small><br>
<button class="pay" onclick="go('pay')">Оплатить</button>
<button class="cancel" onclick="go('cancel')">Отменить</button>
<script>function go(a){{fetch('/api/pay/mock_confirm',{{method:'POST',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:{token!r},action:a}})}})
.then(function(r){{return r.json()}}).then(function(d){{location.href=d.redirect||'/market.html'}})}}</script>
</div></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_mock_confirm(self, payload):
        if not CONFIG["mock_pay"]:
            self.send_error_json("Не найдено", status=404)
            return
        token = payload.get("token", "") if isinstance(payload, dict) else ""
        action = payload.get("action", "") if isinstance(payload, dict) else ""
        with MOCK_LOCK:
            mock = MOCK_PAYMENTS.get(token)
            if mock and mock.get("status") == "pending":
                mock["status"] = "succeeded" if action == "pay" else "canceled"
            else:
                mock = None
        if not mock:
            self.send_error_json("Платёж не найден", status=404)
            return
        orders_mod.set_payment(CONFIG["db"], mock["number"], mock["payment_id"],
                               mock["status"], mock["amount_minor"])
        verdict = "success" if mock["status"] == "succeeded" else "fail"
        self.send_json({"ok": True, "redirect": f"/market.html?pay={verdict}&order={mock['number']}"})


# -------------------------------------------------------------------- запуск

def load_catalogs():
    path = os.path.join(ROOT, "catalog.json")
    data = json.load(open(path, encoding="utf-8"))
    catalogs = data.get("catalogs", {})
    for catalog in catalogs.values():
        catalog["items"] = {str(k): v for k, v in catalog["items"].items()}
    return catalogs


def load_payment_config():
    shop_id = os.environ.get("YOOKASSA_SHOP_ID", "").strip()
    secret = os.environ.get("YOOKASSA_SECRET", "").strip()
    base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if os.path.isfile(CONFIG_FILE):
        try:
            file_cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception as exc:
            print(f"ВНИМАНИЕ: config.json не читается: {exc}")
            file_cfg = {}
        shop_id = shop_id or str(file_cfg.get("yookassa_shop_id", "")).strip()
        secret = secret or str(file_cfg.get("yookassa_secret", "")).strip()
        base = base or str(file_cfg.get("public_base_url", "")).strip()
    if shop_id and secret and base:
        CONFIG["yk"] = YooKassa(shop_id=shop_id, secret=secret)
        CONFIG["public_base"] = base
        CONFIG["mock_pay"] = False
    else:
        CONFIG["mock_pay"] = True


def ensure_seed_cache(data_dir=DATA_DIR, seed=CACHE_SEED):
    """На хостинге постоянный диск поначалу пуст: подкладываем туда
    прогретый кеш карты из поставки, чтобы топ-города открывались сразу."""
    target = os.path.join(data_dir, "cache.sqlite3")
    if os.path.isfile(target) or not os.path.isfile(seed):
        return False
    try:
        os.makedirs(data_dir, exist_ok=True)
        shutil.copyfile(seed, target)
        print(f"Кеш карты восстановлен из поставки ({os.path.getsize(target)} байт).")
        return True
    except OSError as exc:
        print(f"ВНИМАНИЕ: не удалось восстановить кеш карты: {exc}")
        return False


def ensure_admin():
    forced = os.environ.get("ADMIN_PASSWORD", "").strip()
    if forced:
        orders_mod.set_admin_password(CONFIG["db"], forced)
        print("Пароль админки взят из переменной ADMIN_PASSWORD.")
        return False
    if orders_mod.has_admin(CONFIG["db"]):
        return False
    password = secrets.token_urlsafe(18)
    orders_mod.set_admin_password(CONFIG["db"], password)
    with open(ACCESS_FILE, "w", encoding="utf-8") as f:
        f.write("Maltsev Engineering — доступ владельца (храните приватно!)\n\n"
                f"Пароль админки: {password}\n\n"
                "Вставьте его в форму входа на странице admin.html.\n"
                "Сброс без удаления заказов: python3 server.py --reset-admin-password\n")
    try:
        os.chmod(ACCESS_FILE, 0o600)
    except OSError:
        pass
    return True


def main():
    parser = argparse.ArgumentParser(description="Maltsev Engineering — сервер магазина")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true", help="открыть магазин в браузере")
    parser.add_argument("--open-admin", action="store_true", help="открыть админку в браузере")
    parser.add_argument("--reset-admin-password", action="store_true")
    args = parser.parse_args()
    host = os.environ.get("HOST", args.host).strip() or args.host
    try:
        port = int(os.environ.get("PORT", args.port))
    except (TypeError, ValueError):
        port = args.port

    ensure_seed_cache()
    CONFIG["catalogs"] = load_catalogs()
    CONFIG["db"], CONFIG["instance_id"] = orders_mod.init_db(DATA_DIR)
    load_payment_config()

    if args.reset_admin_password:
        password = secrets.token_urlsafe(18)
        orders_mod.set_admin_password(CONFIG["db"], password)
        with open(ACCESS_FILE, "w", encoding="utf-8") as f:
            f.write("Maltsev Engineering — доступ владельца (храните приватно!)\n\n"
                    f"Пароль админки: {password}\n\n")
        print("Новый пароль записан в ADMIN_ACCESS.txt, старые сеансы отозваны.")
        return

    fresh_password = ensure_admin()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    host_show = "localhost" if host == "0.0.0.0" else host
    base_url = f"http://{host_show}:{port}"
    with open(os.path.join(DATA_DIR, "server_address.json"), "w", encoding="utf-8") as f:
        json.dump({"url": base_url, "host": host, "port": port,
                   "pid": os.getpid(), "root": ROOT}, f, ensure_ascii=False)

    print("=" * 60)
    print(f"Магазин:  {base_url}/market.html")
    print(f"Админка:  {base_url}/admin.html")
    print(f"Оплата:   {'MOCK (демо, денег нет)' if CONFIG['mock_pay'] else 'ЮKassa LIVE'}")
    if CONFIG["mock_pay"]:
        print("ВНИМАНИЕ: без ключей ЮKassa оплата учебная! Ключи: config.json "
              "или переменные YOOKASSA_SHOP_ID/YOOKASSA_SECRET/PUBLIC_BASE_URL.")
    if fresh_password:
        print("Создан пароль админки — см. ADMIN_ACCESS.txt (никому не показывайте).")
    print("Остановка: Ctrl+C. База: data/orders.sqlite3 (не удалять!).")
    print("=" * 60)
    if args.open or args.open_admin:
        url = base_url + ("/admin.html" if args.open_admin and not args.open else "/market.html")
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен. Заказы сохранены в data/orders.sqlite3.")


if __name__ == "__main__":
    main()
