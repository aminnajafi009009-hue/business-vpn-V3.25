"""
tonpays.py
کلاینت درگاه TonPays (تون‌پیز) — API استاندارد، مطابق مستندات رسمی (https://doc.tonpays.online).

  • احراز هویت: هدر X-API-Key
  • ساخت فاکتور:    POST /api/v1/invoices/create   {amount(تومان), order_id(≤۲۰ کاراکتر), callback_url, buyer_chat_id}
  • استعلام:         GET  /api/v1/invoices/check/{invoice_id}
  • لغو (فقط pending): POST /api/v1/invoices/{invoice_id}/cancel
  • ثبت کارت عودت مبلغ بیشتر: POST /api/v1/invoices/{invoice_id}/refund {invoice_id, card_number, card_holder}
  • ارسال رسید مابقی (مبلغ کمتر): POST /api/v1/invoices/{invoice_id}/receipt  (multipart file)
  • وب‌هوک: هدر اختیاری X-TonPays-Signature = "t=<unix>,v1=<hmac_sha256_hex>" روی «timestamp.raw_body»
           (فقط وقتی «سکرت امضا» در پروفایل ساخته شده باشد ارسال می‌شود)
  • وضعیت‌ها: pending | processing | action_required_less | action_required_more | completed | rejected | expired | canceled
    فقط completed با paid=true یعنی پرداخت‌شده. مبلغ نهایی واریزی (final_amount) با مبلغ فاکتور (request_amount)
    فرق دارد (کسر/اعشار یکتا)؛ شارژ/تحویل همیشه به‌اندازه‌ی request_amount (مبلغ فاکتور ما) است.
  • خطاها: {"detail": {"code": "...", "message": "..."}}
  • محدودیت: ساخت/استعلام ۶۰ در دقیقه، ۱۲۰ درخواست از هر آی‌پی در دقیقه.

این ماژول sync است (requests)؛ نسخه‌های async در online_gateways.py با asyncio.to_thread.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time

import requests

import database as db
from payment_manager_v3 import TonPaysConfig

logger = logging.getLogger(__name__)

SETTING_KEY = "pm_tonpays"
DEFAULT_BASE_URL = "https://tonpays.online"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
SIGNATURE_TOLERANCE_SECONDS = 600
_USER_AGENT = "VPNShopBot-TonPays/1.0"
_INVOICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
VALID_LINK_MODES = ("web", "telegram")

_CODE_TEXT = {
    "MISSING_API_KEY": "هدر API Key ارسال نشد",
    "INVALID_API_KEY": "API Key نامعتبر است",
    "INACTIVE_API_KEY": "API Key غیرفعال است",
    "WRONG_API_KEY_KIND": "نوع کلید اشتباه است (باید کلید «استاندارد» باشد)",
    "ACCOUNT_NOT_VERIFIED": "حساب فروشگاه در TonPays هنوز تأیید نشده است",
    "ACCOUNT_SUSPENDED": "حساب فروشگاه در TonPays تعلیق شده است",
    "STORE_INACTIVE": "فروشگاه در TonPays غیرفعال است",
    "DUPLICATE_ORDER_ID": "شناسه سفارش تکراری است",
    "AMOUNT_TOO_LOW": "مبلغ کمتر از حداقل مجاز TonPays است",
    "AMOUNT_TOO_HIGH": "مبلغ بیشتر از سقف مجاز TonPays است",
    "INVALID_BUYER_CHAT_ID": "شناسه تلگرام خریدار نامعتبر است",
    "INVALID_CALLBACK_URL": "آدرس وب‌هوک نامعتبر است (باید HTTPS عمومی باشد)",
    "RATE_LIMIT_EXCEEDED": "سقف تعداد درخواست؛ کمی بعد دوباره تلاش کنید",
    "INVOICE_NOT_FOUND": "فاکتور پیدا نشد",
    "ACCESS_DENIED": "دسترسی به این فاکتور ندارید",
    "BUYER_HAS_PENDING_INVOICE": "خریدار فاکتور در انتظار پرداخت دارد",
    "BUYER_HAS_PROCESSING_INVOICE": "خریدار فاکتور در انتظار تأیید دارد",
    "BUYER_HAS_NEED_ACTION_LESS": "خریدار فاکتور «مبلغ کمتر» باز دارد",
    "BUYER_HAS_NEED_ACTION_MORE": "خریدار فاکتور «مبلغ بیشتر» باز دارد",
    "BUYER_INVOICE_BUSY": "ساخت فاکتور برای این خریدار در جریان است",
    "CANCEL_NOT_ALLOWED_PROCESSING": "فاکتور در انتظار تأیید لغو نمی‌شود",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_LESS": "فاکتور مبلغ کمتر لغو نمی‌شود",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_MORE": "فاکتور مبلغ بیشتر لغو نمی‌شود",
    "INVOICE_ALREADY_CANCELED": "فاکتور قبلاً لغو شده",
    "INVOICE_EXPIRED": "فاکتور منقضی شده",
}


# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------
def cfg() -> TonPaysConfig:
    try:
        d = json.loads(db.get_setting(SETTING_KEY, "{}") or "{}")
        if not isinstance(d, dict):
            d = {}
    except Exception:
        logger.exception("خواندن تنظیمات pm_tonpays ناموفق بود")
        d = {}
    return TonPaysConfig.from_dict(d)


def api_key() -> str:
    return (cfg().api_key or "").strip()


def webhook_secret() -> str:
    return (cfg().webhook_secret or "").strip()


def base_url() -> str:
    return ((cfg().base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")


def enabled() -> bool:
    c = cfg()
    return bool(c.enabled and (c.api_key or "").strip())


def min_amount() -> int:
    try:
        return max(int(cfg().min_amount or 0), 0)
    except (TypeError, ValueError):
        return 0


def amount_ok(amount: int) -> bool:
    try:
        a = int(amount)
    except (TypeError, ValueError):
        return False
    return a > 0 and a >= min_amount()


def _headers() -> dict:
    return {"X-API-Key": api_key(), "Accept": "application/json", "User-Agent": _USER_AGENT}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def error_code(body) -> str:
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            return str(d.get("code") or "")
    return ""


def _error_text(status: int | None, body) -> str:
    code = error_code(body)
    msg = ""
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        msg = str(body["detail"].get("message") or "")[:200]
    base = _CODE_TEXT.get(code) or (f"خطای HTTP {status}" if status else "خطا")
    extra = f" ({code})" if code and code not in _CODE_TEXT else ""
    return base + extra + (f" — {msg}" if msg and not _CODE_TEXT.get(code) else "")


def _request(method: str, path: str, payload: dict | None = None, files=None) -> dict:
    """خروجی: {"ok","status","data","error","code"} — هیچ استثنایی نشت نمی‌کند و کلید API در لاگ/خطا نمی‌آید."""
    if not api_key():
        return {"ok": False, "status": None, "data": None, "error": "API Key تون‌پیز تنظیم نشده است", "code": ""}
    try:
        kw: dict = {"headers": _headers(), "timeout": (CONNECT_TIMEOUT, READ_TIMEOUT), "allow_redirects": False}
        if files is not None:
            kw["files"] = files
        elif payload is not None:
            kw["json"] = payload
        resp = requests.request(method, f"{base_url()}{path}", **kw)
    except requests.RequestException as exc:
        logger.warning("TonPays %s %s خطای شبکه: %s", method, path, type(exc).__name__)
        return {"ok": False, "status": None, "data": None, "error": f"خطای شبکه ({type(exc).__name__})", "code": ""}
    try:
        body = resp.json()
    except ValueError:
        body = None
        logger.error("TonPays %s %s پاسخ غیر-JSON | status=%s", method, path, resp.status_code)
    if 200 <= resp.status_code < 300 and isinstance(body, dict):
        return {"ok": True, "status": resp.status_code, "data": body, "error": None, "code": ""}
    if 300 <= resp.status_code < 400:
        return {"ok": False, "status": resp.status_code, "data": None,
                "error": "ریدایرکت از سمت TonPays (Base URL را بررسی کنید)", "code": ""}
    return {"ok": False, "status": resp.status_code, "data": body if isinstance(body, dict) else None,
            "error": _error_text(resp.status_code, body), "code": error_code(body)}


# ---------------------------------------------------------------------------
# عملیات
# ---------------------------------------------------------------------------
def create_invoice(amount_toman: int, order_id: str, callback_url: str | None = None,
                   buyer_chat_id: int | None = None) -> dict:
    """ساخت فاکتور. order_id حداکثر ۲۰ کاراکتر. بدون retry کورکورانه (retry هوشمند در online_gateways)."""
    payload: dict = {"amount": int(amount_toman), "order_id": str(order_id)[:20]}
    if callback_url:
        payload["callback_url"] = callback_url
    if buyer_chat_id and cfg().send_buyer:
        try:
            payload["buyer_chat_id"] = int(buyer_chat_id)
        except (TypeError, ValueError):
            pass
    res = _request("POST", "/api/v1/invoices/create", payload)
    if res["ok"]:
        iid = str((res["data"] or {}).get("invoice_id") or "").strip()
        if not _INVOICE_ID_RE.match(iid):
            return {"ok": False, "status": res["status"], "data": res["data"],
                    "error": "پاسخ نامعتبر از TonPays (invoice_id ندارد)", "code": ""}
    return res


def get_status(invoice_id: str) -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    return _request("GET", f"/api/v1/invoices/check/{iid}")


def cancel_invoice(invoice_id: str) -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    return _request("POST", f"/api/v1/invoices/{iid}/cancel")


def register_refund_card(invoice_id: str, card_number: str, card_holder: str) -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    return _request("POST", f"/api/v1/invoices/{iid}/refund",
                    {"invoice_id": iid, "card_number": re.sub(r"\D", "", card_number or ""), "card_holder": (card_holder or "").strip()})


def send_remainder_receipt(invoice_id: str, filename: str, content: bytes, content_type: str = "image/jpeg") -> dict:
    iid = str(invoice_id or "").strip()
    if not _INVOICE_ID_RE.match(iid):
        return {"ok": False, "status": None, "data": None, "error": "invoice_id نامعتبر", "code": ""}
    return _request("POST", f"/api/v1/invoices/{iid}/receipt", files={"file": (filename or "receipt.jpg", content, content_type)})


def pick_links(data: dict | None) -> dict:
    """invoice_url = ربات تلگرام؛ web_invoice_url = صفحه‌ی وب (با شناسه‌ی خریدار). بسته به link_mode ترجیح اول فرق می‌کند."""
    d = data if isinstance(data, dict) else {}

    def _u(k):
        v = d.get(k)
        return v.strip() if isinstance(v, str) and v.strip().lower().startswith(("https://", "http://", "tg://")) else None

    tg, web = _u("invoice_url"), _u("web_invoice_url")
    if cfg().link_mode == "telegram":
        primary = tg or web
    else:
        primary = web or tg
    secondary = tg if (tg and tg != primary) else (web if (web and web != primary) else None)
    return {"primary": primary, "secondary": secondary}


# ---------------------------------------------------------------------------
# امضا
# ---------------------------------------------------------------------------
def verify_signature(raw_body: bytes, header: str | None, secret: str | None = None, now: float | None = None) -> bool:
    """X-TonPays-Signature: t=<unix>,v1=<hmac_sha256_hex>  روی  f"{t}.{raw_body}"."""
    secret = (secret if secret is not None else webhook_secret()).strip()
    if not secret or not header:
        return False
    parts = {}
    for chunk in str(header).split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    t, v1 = parts.get("t", ""), parts.get("v1", "").lower()
    if not t.isdigit() or not v1:
        return False
    if abs((now if now is not None else time.time()) - int(t)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), t.encode() + b"." + (raw_body or b""), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


# ---------------------------------------------------------------------------
# ارزیابی
# ---------------------------------------------------------------------------
def _amt(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def evaluate(payment: dict, data: dict) -> tuple[str, str]:
    """ارزیابی پاسخ استعلام/وب‌هوک. خروجی (state, reason):
       paid | pending | action_less | review | closed | error
    فقط status=completed و paid=true پرداخت‌شده است؛ شناسه‌ی سفارش و مبلغ فاکتور باید با فاکتور ما بخواند."""
    if not isinstance(data, dict):
        return "error", "پاسخ نامعتبر"
    status = str(data.get("status") or "").lower()
    if status in ("pending", "processing"):
        return "pending", status
    if status == "action_required_less":
        return "action_less", status
    if status == "action_required_more":
        return "review", f"مبلغ بیشتر از فاکتور واریز شده (مازاد: {_amt(data.get('refund_amount')) or '؟'} تومان)؛ مازاد باید عودت شود"
    if status in ("expired", "canceled", "cancelled"):
        return "closed", status
    if status == "rejected":
        rr = data.get("reject_reasons")
        return "closed", "rejected" + (": " + "، ".join(str(x) for x in rr)[:200] if isinstance(rr, list) and rr else "")
    if status != "completed" or data.get("paid") is not True:
        return "error", f"وضعیت ناشناخته: {status[:40]}"

    stored = str(payment.get("ref_id") or "")
    iid = str(data.get("invoice_id") or "")
    if stored and iid and stored != iid:
        return "review", "invoice_id پاسخ با فاکتور مطابقت ندارد"
    oid = data.get("order_id")
    if oid not in (None, "") and str(oid) != str(payment.get("hash_id")):
        return "review", "order_id پاسخ با فاکتور مطابقت ندارد"
    ra = _amt(data.get("request_amount"))
    if ra is not None and ra != int(payment["price"]):
        return "review", f"مبلغ فاکتور تون‌پیز ({ra:,}) با مبلغ ثبت‌شده ({int(payment['price']):,}) مطابقت ندارد"
    return "paid", ""
