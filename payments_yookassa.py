#!/usr/bin/env python3
"""ЮKassa для Maltsev Engineering — production-модуль (только stdlib, без pip-пакетов).

Использование (встраивается в server.py / orders.py, см. INTEGRATION.md):

    from payments_yookassa import YooKassa, YooKassaError

    yk = YooKassa(shop_id=os.environ["YOOKASSA_SHOP_ID"],
                  secret=os.environ["YOOKASSA_SECRET_KEY"])

    pay = yk.create_payment(order_number="ME-AB12CD", amount_minor=158400,
                            description="Заказ ME-AB12CD — Maltsev Engineering",
                            return_url="https://ваш-домен/market.html",
                            email="buyer@example.com", phone="+7 900 000-00-00",
                            items=[{"description": "Корпус для ...", "quantity": 1,
                                    "amount_minor": 158400}])
    # pay -> {"payment_id": ..., "confirmation_url": "https://yoomoney.ru/checkout/...",
    #         "status": "pending", "amount_minor": 158400}

    # Вебхук ЮKassa (HTTP-уведомления) -> POST api/payments/notification:
    trusted = yk.confirm_notification(raw_body_bytes)  # сверяется с API ЮKassa
    # trusted -> {"event": "payment.succeeded", "payment_id": ..., "status": "succeeded",
    #             "amount_minor": ..., "order_number": "ME-AB12CD"}

Документация API: https://yookassa.ru/developers/api
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
import uuid

API_BASE = "https://api.yookassa.ru/v3"

# Миграция БД заказов: статусы оплаты. Выполнить один раз (см. INTEGRATION.md).
MIGRATION_SQL = """
ALTER TABLE orders ADD COLUMN payment_id TEXT DEFAULT NULL;
ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT NULL;  -- pending|succeeded|canceled
ALTER TABLE orders ADD COLUMN paid_at TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_payment ON orders(payment_id);
"""


class YooKassaError(Exception):
    pass


def minor_to_amount(amount_minor):
    """Копейки -> строка '1234.00' для API ЮKassa."""
    return f"{int(amount_minor) / 100:.2f}"


def normalize_phone(phone):
    """Телефон -> E.164 без плюса (требование чека ЮKassa), либо ''."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


class YooKassa:
    def __init__(self, shop_id=None, secret=None, timeout=25):
        self.shop_id = shop_id or os.environ.get("YOOKASSA_SHOP_ID", "")
        self.secret = secret or os.environ.get("YOOKASSA_SECRET_KEY", "")
        self.timeout = timeout
        if not self.shop_id or not self.secret:
            raise YooKassaError("Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")

    def _request(self, method, path, payload=None, idempotence_key=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(API_BASE + path, data=data, method=method)
        token = base64.b64encode(f"{self.shop_id}:{self.secret}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if idempotence_key:
            req.add_header("Idempotence-Key", idempotence_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            raise YooKassaError(f"ЮKassa HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise YooKassaError(f"Нет связи с ЮKassa: {e}")

    def build_receipt(self, email, phone, items, vat_code=1):
        """Чек 54-ФЗ. vat_code=1 — без НДС (УСН/самозанятые). Для ОСН укажите свой код."""
        phone = normalize_phone(phone)
        email = (email or "").strip()
        if not email and not phone:
            return None  # Некуда отправлять чек — ЮKassa отклонит receipt
        receipt_items = []
        for it in items:
            receipt_items.append({
                "description": str(it["description"])[:128],
                "quantity": str(it.get("quantity", 1)),
                "amount": {"value": minor_to_amount(it["amount_minor"]),
                           "currency": "RUB"},
                "vat_code": vat_code,
                "payment_mode": "full_prepayment",
                "payment_subject": "commodity",
            })
        customer = {}
        if email:
            customer["email"] = email[:64]
        if phone:
            customer["phone"] = phone
        return {"customer": customer, "items": receipt_items}

    def create_payment(self, *, order_number, amount_minor, description,
                       return_url, email="", phone="", items=None,
                       vat_code=1, idempotence_key=None, metadata=None):
        """Создать платёж. Возвращает dict с confirmation_url для редиректа."""
        if int(amount_minor) <= 0:
            raise YooKassaError("Нулевая сумма платежа")
        payload = {
            "amount": {"value": minor_to_amount(amount_minor), "currency": "RUB"},
            "capture": True,  # автоподтверждение (деньги сразу списываются)
            "confirmation": {"type": "redirect",
                             "return_url": f"{return_url}?pay=pending&order={order_number}"},
            "description": str(description)[:128],
            "metadata": {"order_number": order_number, "source": "maltsev_market",
                         **(metadata or {})},
        }
        receipt = self.build_receipt(email, phone, items or [], vat_code)
        if receipt:
            payload["receipt"] = receipt
        result = self._request("POST", "/payments", payload,
                               idempotence_key or str(uuid.uuid4()))
        conf = (result.get("confirmation") or {}).get("confirmation_url", "")
        if not conf:
            raise YooKassaError(f"ЮKassa не вернула ссылку на оплату: {result.get('status')}")
        return {"payment_id": result.get("id"), "confirmation_url": conf,
                "status": result.get("status", "pending"),
                "amount_minor": int(float(result["amount"]["value"]) * 100)}

    def get_payment(self, payment_id):
        """Запросить платёж из API (источник правды при проверке вебхука)."""
        if not payment_id or len(str(payment_id)) > 64:
            raise YooKassaError("Некорректный payment_id")
        return self._request("GET", f"/payments/{payment_id}")

    @staticmethod
    def parse_notification(raw_body):
        """Разобрать HTTP-уведомление ЮKassa (без доверия — только структура)."""
        try:
            data = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body)
        except Exception:
            raise YooKassaError("Некорректный JSON уведомления")
        if data.get("type") != "notification" or not isinstance(data.get("object"), dict):
            raise YooKassaError("Неизвестный тип уведомления")
        obj = data["object"]
        return {"event": data.get("event"), "payment_id": obj.get("id"),
                "status": obj.get("status"),
                "amount_minor": int(float(obj.get("amount", {}).get("value", "0")) * 100),
                "order_number": (obj.get("metadata") or {}).get("order_number")}

    def confirm_notification(self, raw_body, expected_amount_minor=None,
                             expected_order_number=None):
        """Проверить уведомление через API ЮKassa. Возвращает доверенный статус.

        Сверяет сумму и номер заказа — поддельный вебхук не пройдёт.
        """
        parsed = self.parse_notification(raw_body)
        payment = self.get_payment(parsed["payment_id"])  # источник правды
        status = payment.get("status")
        amount = int(float(payment.get("amount", {}).get("value", "0")) * 100)
        order_number = (payment.get("metadata") or {}).get("order_number")
        if expected_amount_minor is not None and amount != int(expected_amount_minor):
            raise YooKassaError(f"Сумма в ЮKassa ({amount}) не совпала с заказом")
        if expected_order_number and order_number != expected_order_number:
            raise YooKassaError("Номер заказа в платеже не совпал")
        return {"event": parsed["event"], "payment_id": payment.get("id"),
                "status": status, "amount_minor": amount, "order_number": order_number}
