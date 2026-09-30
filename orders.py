#!/usr/bin/env python3
"""Maltsev Engineering — заказы, SQLite, авторизация админки.

Только стандартная библиотека. Хранилище: data/orders.sqlite3.
Пароль создаётся случайно и пишется в ADMIN_ACCESS.txt (только владельцу).
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timezone

API_VERSION = 4
RELEASE = "5.0"
SESSION_HOURS = 8
PBKDF2_ROUNDS = 200_000
PER_PAGE = 20
STATUSES = ("new", "processing", "done", "cancelled")
NUM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class OrderError(Exception):
    """Проверяемая ошибка с понятным текстом для покупателя/админа."""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pwd_hash BLOB NOT NULL,
    pwd_salt BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'new',
    archived INTEGER NOT NULL DEFAULT 0,
    catalog TEXT NOT NULL DEFAULT 'main',
    catalog_label TEXT NOT NULL DEFAULT '',
    catalog_version TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL DEFAULT '[]',
    total_minor INTEGER NOT NULL DEFAULT 0,
    customer_json TEXT NOT NULL DEFAULT '{}',
    delivery_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT UNIQUE,
    fingerprint TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'site',
    legacy_id TEXT NOT NULL DEFAULT '',
    legacy_date TEXT NOT NULL DEFAULT '',
    payment_id TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT '',
    paid_minor INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, archived, id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
"""


def init_db(data_dir):
    """Создаёт базу и возвращает (db_path, instance_id)."""
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "orders.sqlite3")
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()
        if row:
            instance_id = row["value"]
        else:
            instance_id = secrets.token_hex(16)
            conn.execute("INSERT INTO meta(key, value) VALUES ('instance_id', ?)", (instance_id,))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', '1')")
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path, instance_id


def _migrate(conn):
    """Добавляет колонки, которых нет в старых базах (безопасно)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    extra = {
        "payment_id": "TEXT NOT NULL DEFAULT ''",
        "payment_status": "TEXT NOT NULL DEFAULT ''",
        "paid_minor": "INTEGER NOT NULL DEFAULT 0",
        "paid_at": "TEXT NOT NULL DEFAULT ''",
        "fingerprint": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in extra.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")


# ---------------------------------------------------------------- авторизация

def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def has_admin(db_path):
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM admin WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def set_admin_password(db_path, password):
    salt = secrets.token_bytes(16)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO admin(id, pwd_hash, pwd_salt, created_at) VALUES (1, ?, ?, ?)",
            (_hash_password(password, salt), salt, utcnow()),
        )
        conn.execute("DELETE FROM sessions")  # смена пароля гасит все сеансы
        conn.commit()
    finally:
        conn.close()


def check_password(db_path, password):
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT pwd_hash, pwd_salt FROM admin WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row:
        return False
    want = _hash_password(password, bytes(row["pwd_salt"]))
    return secrets.compare_digest(want, bytes(row["pwd_hash"]))


def create_session(db_path):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = utcnow()
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        conn.execute(
            "INSERT INTO sessions(token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (digest, now, time.time() + SESSION_HOURS * 3600),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def check_session(db_path, token):
    if not token or len(token) > 200:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        if not row:
            return False
        if row["expires_at"] <= time.time():
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
            conn.commit()
            return False
        return True
    finally:
        conn.close()


def revoke_session(db_path, token):
    if not token:
        return
    digest = hashlib.sha256(token.encode()).hexdigest()
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- валидация

def _clean_str(value, max_len):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_len]


def _new_number(conn):
    for _ in range(20):
        number = "ME-" + "".join(secrets.choice(NUM_ALPHABET) for _ in range(6))
        if not conn.execute("SELECT 1 FROM orders WHERE number=?", (number,)).fetchone():
            return number
    raise OrderError("Не удалось создать номер заказа, попробуйте ещё раз")


def create_order(db_path, catalogs, payload):
    """Проверяет заказ по серверным ценам и сохраняет. Возвращает receipt."""
    if not isinstance(payload, dict):
        raise OrderError("Неверный формат заказа")
    catalog_id = payload.get("catalog", "main")
    catalog = catalogs.get(catalog_id)
    if not catalog:
        raise OrderError("Неизвестный каталог, обновите страницу")
    if payload.get("catalog_version") != catalog["version"]:
        raise OrderError("Каталог обновился, обновите страницу и отправьте заказ снова")

    items_in = payload.get("items")
    if not isinstance(items_in, list) or not items_in or len(items_in) > 50:
        raise OrderError("Корзина пуста или слишком велика")
    items = []
    total = 0
    seen = set()
    for entry in items_in:
        if not isinstance(entry, dict):
            raise OrderError("Неверная позиция корзины")
        try:
            pid = int(entry.get("id"))
            qty = int(entry.get("quantity"))
        except (TypeError, ValueError):
            raise OrderError("Неверная позиция корзины")
        if pid <= 0 or qty < 1 or qty > 99 or pid in seen:
            raise OrderError("Неверная позиция корзины")
        seen.add(pid)
        server_item = catalog["items"].get(str(pid)) or catalog["items"].get(pid)
        if not server_item:
            raise OrderError(f"Товар {pid} недоступен, обновите страницу")
        price = int(server_item["price_minor"])
        line = price * qty
        total += line
        items.append({"id": pid, "name": server_item.get("name", f"Товар {pid}"),
                      "quantity": qty, "price_minor": price, "total_minor": line})
    try:
        expected = int(payload.get("expected_total_minor"))
    except (TypeError, ValueError):
        raise OrderError("Не указана сумма заказа")
    if expected != total:
        raise OrderError("Сумма не совпала, обновите страницу и отправьте снова")

    customer_in = payload.get("customer") or {}
    name = _clean_str(customer_in.get("name", ""), 100)
    phone_raw = _clean_str(customer_in.get("phone", ""), 40)
    email = _clean_str(customer_in.get("email", ""), 120)
    if not name:
        raise OrderError("Укажите имя")
    digits = re.sub(r"\D", "", phone_raw)
    if len(digits) < 6:
        raise OrderError("Укажите полный телефон")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise OrderError("Проверьте email")
    customer = {"name": name, "phone": phone_raw, "email": email}

    delivery_in = payload.get("delivery") or {}
    address = _clean_str(delivery_in.get("address", ""), 500)
    if len(address) < 5:
        raise OrderError("Укажите полный адрес ПВЗ: город, улицу и дом")
    delivery = {
        "carrier": _clean_str(delivery_in.get("carrier", "Ozon"), 20) or "Ozon",
        "id": _clean_str(str(delivery_in.get("id") or ""), 60),
        "city": _clean_str(delivery_in.get("city", ""), 120),
        "city_id": _clean_str(str(delivery_in.get("city_id") or ""), 120),
        "city_query": _clean_str(delivery_in.get("city_query", ""), 120),
        "address": address,
        "source": _clean_str(delivery_in.get("source", ""), 30),
        "address_source": _clean_str(delivery_in.get("address_source", ""), 30),
        "osm_url": _clean_str(delivery_in.get("osm_url", ""), 200),
    }
    for coord in ("lat", "lon"):
        value = delivery_in.get(coord)
        if isinstance(value, (int, float)) and abs(value) <= 180:
            delivery[coord] = float(value)

    key = payload.get("idempotency_key", "")
    if not isinstance(key, str) or not (8 <= len(key) <= 128) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise OrderError("Неверный ключ повторной отправки, обновите страницу")
    fingerprint = hashlib.sha256(json.dumps(
        {"c": catalog_id, "i": [[it["id"], it["quantity"]] for it in items],
         "t": total, "n": name, "p": phone_raw, "e": email, "d": address},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id, number, total_minor, delivery_json, fingerprint FROM orders WHERE idempotency_key=?",
            (key,)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise OrderError("Этот заказ уже отправлялся с другими данными. "
                                 "Проверьте его в админке, прежде чем менять корзину")
            return {"id": existing["id"], "number": existing["number"],
                    "pickup": json.loads(existing["delivery_json"]).get("address", ""),
                    "total_minor": existing["total_minor"]}
        number = _new_number(conn)
        now = utcnow()
        cur = conn.execute(
            """INSERT INTO orders(number, status, archived, catalog, catalog_label, catalog_version,
                   items_json, total_minor, customer_json, delivery_json,
                   idempotency_key, fingerprint, source, created_at, updated_at)
               VALUES (?, 'new', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'site', ?, ?)""",
            (number, catalog_id, catalog.get("label", ""), catalog["version"],
             json.dumps(items, ensure_ascii=False), total,
             json.dumps(customer, ensure_ascii=False), json.dumps(delivery, ensure_ascii=False),
             key, fingerprint, now, now),
        )
        conn.commit()
        return {"id": cur.lastrowid, "number": number, "pickup": address, "total_minor": total}
    finally:
        conn.close()


# ------------------------------------------------------------------- чтение

def _row_to_order(row):
    return {
        "id": row["id"],
        "number": row["number"],
        "status": row["status"],
        "archived": bool(row["archived"]),
        "catalog_label": row["catalog_label"] or "",
        "items": json.loads(row["items_json"] or "[]"),
        "total_minor": row["total_minor"],
        "customer": json.loads(row["customer_json"] or "{}"),
        "delivery": json.loads(row["delivery_json"] or "{}"),
        "source": row["source"] or "site",
        "legacy_id": row["legacy_id"] or "",
        "legacy_date": row["legacy_date"] or "",
        "payment_status": row["payment_status"] or None,
        "payment_id": row["payment_id"] or "",
        "created_at": row["created_at"],
    }


def list_orders(db_path, q="", status="", archived=0, page=1):
    conn = _connect(db_path)
    try:
        where = ["archived=?"]
        params = [1 if archived else 0]
        if status in STATUSES:
            where.append("status=?")
            params.append(status)
        if q:
            where.append("(number LIKE ? ESCAPE '\\' OR customer_json LIKE ? ESCAPE '\\' "
                         "OR delivery_json LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([like, like, like])
        where_sql = "WHERE " + " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) c FROM orders {where_sql}", params).fetchone()["c"]
        stats = conn.execute(
            f"SELECT COUNT(*) total, SUM(status='new') new_count, "
            f"COALESCE(SUM(total_minor),0) amount FROM orders {where_sql}", params).fetchone()
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(max(1, int(page or 1)), pages)
        rows = conn.execute(
            f"SELECT * FROM orders {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        return {"orders": [_row_to_order(r) for r in rows], "page": page, "pages": pages,
                "total": total,
                "stats": {"total": stats["total"], "new_count": stats["new_count"] or 0,
                          "amount": stats["amount"] or 0}}
    finally:
        conn.close()


def update_order(db_path, order_id, change):
    if not isinstance(change, dict):
        raise OrderError("Неверный запрос")
    fields = {}
    if "status" in change:
        if change["status"] not in STATUSES:
            raise OrderError("Неизвестный статус")
        fields["status"] = change["status"]
    if "archived" in change:
        fields["archived"] = 1 if change["archived"] else 0
    if not fields:
        raise OrderError("Нечего менять")
    conn = _connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            raise OrderError("Заказ не найден")
        fields["updated_at"] = utcnow()
        conn.execute("UPDATE orders SET {} WHERE id=?".format(
            ", ".join(f"{k}=?" for k in fields)), list(fields.values()) + [order_id])
        conn.commit()
    finally:
        conn.close()


def export_orders(db_path, instance_id):
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
        return {"version": API_VERSION, "release": RELEASE, "exported_at": utcnow(),
                "instance_id": instance_id, "orders": [_row_to_order(r) for r in rows]}
    finally:
        conn.close()


# ------------------------------------------------------------------- импорт

def _import_fingerprint(entry):
    blob = json.dumps({"n": entry.get("customer", {}).get("name", ""),
                       "p": entry.get("customer", {}).get("phone", ""),
                       "t": entry.get("total_minor", 0),
                       "i": [(it.get("name", ""), it.get("quantity", 0)) for it in entry.get("items", [])],
                       "d": entry.get("legacy_date", "")}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize_import_entry(entry):
    """Принимает наш экспорт и старые локальные записи. Возвращает заказ или ошибку."""
    if not isinstance(entry, dict):
        return None, "запись не объект"
    items_in = entry.get("items")
    if not isinstance(items_in, list) or not items_in:
        return None, "нет товаров"
    items = []
    total = 0
    for raw in items_in:
        if not isinstance(raw, dict):
            return None, "битая позиция товара"
        name = _clean_str(raw.get("name", ""), 200) or "Товар из старой записи"
        try:
            qty = int(raw.get("quantity", raw.get("qty", raw.get("count", 1))))
        except (TypeError, ValueError):
            return None, f"неверное количество у «{name}»"
        if qty < 1 or qty > 999:
            return None, f"неверное количество у «{name}»"
        line_total = raw.get("total_minor", raw.get("sum", raw.get("total")))
        price = raw.get("price_minor", raw.get("price"))
        try:
            if line_total is not None:
                line_total = int(line_total)
                price_each = line_total // qty if qty else 0
            elif price is not None:
                price_each = int(price)
                line_total = price_each * qty
            else:
                price_each, line_total = 0, 0
        except (TypeError, ValueError):
            return None, f"неверная цена у «{name}»"
        if line_total < 0 or line_total > 100_000_000_00:
            return None, f"неверная цена у «{name}»"
        total += line_total
        item = {"name": name, "quantity": qty, "price_minor": price_each, "total_minor": line_total}
        try:
            item["id"] = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            pass
        items.append(item)
    given_total = entry.get("total_minor", entry.get("total"))
    try:
        if given_total is not None and int(given_total) != total and items and \
                all("id" not in it for it in items):
            total = int(given_total)  # у старой записи сохраняем её итог как есть
    except (TypeError, ValueError):
        pass

    cust_in = entry.get("customer") or {}
    customer = {"name": _clean_str(cust_in.get("name", ""), 100),
                "phone": _clean_str(cust_in.get("phone", ""), 40),
                "email": _clean_str(cust_in.get("email", ""), 120)}
    del_in = entry.get("delivery") or {}
    delivery = {"carrier": _clean_str(del_in.get("carrier", "Ozon"), 20) or "Ozon",
                "address": _clean_str(del_in.get("address", ""), 500) or "Адрес не указан в старой записи",
                "city": _clean_str(del_in.get("city", ""), 120),
                "id": _clean_str(str(del_in.get("id") or ""), 60),
                "source": _clean_str(del_in.get("source", ""), 30) or "legacy",
                "address_source": _clean_str(del_in.get("address_source", ""), 30),
                "osm_url": _clean_str(del_in.get("osm_url", ""), 200)}
    status = entry.get("status", "new")
    if status not in STATUSES:
        status = "new"
    number = entry.get("number")
    if not isinstance(number, str) or not re.fullmatch(r"ME-[A-Z0-9]{4,12}", number or ""):
        number = ""
    return {"number": number, "status": status, "archived": 1 if entry.get("archived") else 0,
            "catalog_label": _clean_str(entry.get("catalog_label", ""), 120),
            "items": items, "total_minor": total, "customer": customer, "delivery": delivery,
            "legacy_id": str(entry.get("legacy_id", entry.get("id", "")))[:60],
            "legacy_date": str(entry.get("legacy_date", entry.get("date", entry.get("created_at", ""))))[:60]}, ""


def import_orders(db_path, entries):
    if not isinstance(entries, list) or len(entries) > 50:
        raise OrderError("Импорт — списком до 50 записей")
    imported, duplicates, rejected = 0, 0, []
    conn = _connect(db_path)
    try:
        for index, raw in enumerate(entries):
            norm, error = _normalize_import_entry(raw)
            if error:
                rejected.append({"index": index, "error": error})
                continue
            number = norm["number"]
            if number and conn.execute("SELECT 1 FROM orders WHERE number=?", (number,)).fetchone():
                duplicates += 1
                continue
            fingerprint = _import_fingerprint(norm)
            if not number and conn.execute(
                    "SELECT 1 FROM orders WHERE fingerprint=? AND source='legacy'", (fingerprint,)).fetchone():
                duplicates += 1
                continue
            if not number:
                number = _new_number(conn)
            now = utcnow()
            conn.execute(
                """INSERT INTO orders(number, status, archived, catalog, catalog_label, items_json,
                       total_minor, customer_json, delivery_json, fingerprint, source,
                       legacy_id, legacy_date, created_at, updated_at)
                   VALUES (?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?, 'legacy', ?, ?, ?, ?)""",
                (number, norm["status"], norm["archived"], norm["catalog_label"],
                 json.dumps(norm["items"], ensure_ascii=False), norm["total_minor"],
                 json.dumps(norm["customer"], ensure_ascii=False),
                 json.dumps(norm["delivery"], ensure_ascii=False),
                 fingerprint, norm["legacy_id"], norm["legacy_date"], now, now))
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "duplicates": duplicates, "rejected": rejected}


# ------------------------------------------------------------------ платежи

def get_order_by_number(db_path, number):
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM orders WHERE number=?", (number,)).fetchone()
        return _row_to_order(row) if row else None
    finally:
        conn.close()


def set_payment(db_path, number, payment_id="", status="", paid_minor=0):
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE orders SET payment_id=?, payment_status=?, paid_minor=?, paid_at=?, updated_at=? "
            "WHERE number=?",
            (payment_id, status, paid_minor, utcnow() if status == "succeeded" else "", utcnow(), number))
        conn.commit()
    finally:
        conn.close()


def find_by_payment_id(db_path, payment_id):
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM orders WHERE payment_id=?", (payment_id,)).fetchone()
        return _row_to_order(row) if row else None
    finally:
        conn.close()
