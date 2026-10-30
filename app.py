# -*- coding: utf-8 -*-
"""FitBot v2 | ربات فروش و مدیریت برنامه‌های مربی
Flask webhook + Telegram Bot API + Supabase.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, abort, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import Client, create_client

# ══ Configuration ═══════════════════════════════════════════════════════════
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fitbot")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()
OWNER_IDS = [int(x) for x in re.split(r"[,\s]+", os.environ["OWNER_ID"].strip()) if x]
OWNER_ID = OWNER_IDS[0]
SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
SETUP_KEY = os.getenv("SETUP_KEY", "").strip()
SUPPORT_USERNAME = (os.getenv("COACH_USERNAME") or "Borhan_SK").strip().lstrip("@")
KEEPALIVE = os.getenv("KEEPALIVE", "1").lower() not in ("0", "false", "no", "off")
KEEPALIVE_SECONDS = max(300, int(os.getenv("KEEPALIVE_SECONDS", "480")))
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "1").lower() not in ("0", "false", "no", "off")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HOOK_PATH = hashlib.sha256((BOT_TOKEN + ":" + WEBHOOK_SECRET).encode()).hexdigest()[:40]

# Channels nobody (not even the owner) can remove. Enforced here AND by a DB trigger.
LOCKED_CHANNELS = [("@Borhan_SK_bot", "🤖 Borhan SK Bot")]
LOCKED_REFS = {ref.lower() for ref, _ in LOCKED_CHANNELS}

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = Flask(__name__)

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=Retry(
    total=2, backoff_factor=0.15, status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]), respect_retry_after_header=True)))

WORKERS = max(2, min(12, int(os.getenv("UPDATE_WORKERS", "6"))))
EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fitbot-update")
SEEN_UPDATES: OrderedDict[int, float] = OrderedDict()
SEEN_LOCK = threading.Lock()
USER_LOCKS: dict[int, threading.Lock] = defaultdict(threading.Lock)
CTX = threading.local()  # holds the message_id of the callback currently being handled

# ══ Texts & constants ═══════════════════════════════════════════════════════
LINE = "━━━━━━━━━━━━━━━━"
HOME_BTN = "🏠 منوی اصلی"
ADMIN_BTN = "⚙️ پنل مدیریت"

STATUS_LABELS = {
    "collecting_info": "📝 در حال تکمیل مشخصات",
    "awaiting_receipt": "💳 منتظر پرداخت و رسید",
    "pending_review": "🧾 رسید در حال بررسی",
    "in_progress": "⏳ در حال آماده‌سازی",
    "approved": "⏳ در حال آماده‌سازی",
    "delivered": "✅ تحویل شده",
    "cancelled": "🚫 لغو شده",
}
STATUS_ORDER = ["awaiting_receipt", "pending_review", "in_progress", "delivered", "cancelled"]
CAT_LABELS = {"nutrition": "🥗 تغذیه", "gym": "🏋️ باشگاه", "home": "🏠 منزل"}
PROGRAM_TYPES = {"nutrition": "🥗 برنامه تغذیه", "gym": "🏋️ تمرین باشگاه",
                 "home": "🏠 تمرین منزل", "general": "📌 برنامه عمومی"}
ETA_CODES = {"3d": "۳ روز", "4d": "۴ روز", "1w": "یک هفته"}

# Profile fields. "fixed" ones are asked once; the rest are asked on every new order.
FIELDS = {
    "gender": dict(label="جنسیت", icon="🚻", fixed=True, q="جنسیتت چیه؟", opts=["مرد", "زن"]),
    "age": dict(label="سن", icon="🎂", fixed=True, q="چند سالته؟ (فقط عدد)", kind="int", lo=5, hi=100),
    "height_cm": dict(label="قد", icon="📏", fixed=True, q="قدت چند سانتی‌متره؟ (فقط عدد)", kind="int", lo=100, hi=250, unit="سانتی‌متر"),
    "blood_type": dict(label="گروه خونی", icon="🩸", fixed=True, q="گروه خونیت چیه؟",
                       opts=["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "نمی‌دانم"], cols=3),
    "weight_kg": dict(label="وزن فعلی", icon="⚖️", fixed=False, q="وزن فعلیت چند کیلوئه؟ (عدد)", kind="float", lo=25, hi=300, unit="کیلوگرم"),
    "activity_level": dict(label="فعالیت روزانه", icon="🏃", fixed=False, q="فعالیت روزانه‌ات چطوره؟", opts=["کم", "متوسط", "زیاد"], cols=3),
    "goal": dict(label="هدف", icon="🎯", fixed=False, q="هدفت چیه؟ یکی رو انتخاب کن یا خودت بنویس.",
                 opts=["کاهش وزن", "عضله‌سازی", "تناسب اندام", "افزایش وزن"]),
    "notes": dict(label="محدودیت/توضیحات", icon="📝", fixed=False,
                  q="آسیب‌دیدگی، حساسیت غذایی یا نکته مهمی داری؟ بنویس، یا «ندارم» رو بزن.", opts=["ندارم"]),
}
FIXED_KEYS = [k for k, v in FIELDS.items() if v["fixed"]]
VARIABLE_KEYS = [k for k, v in FIELDS.items() if not v["fixed"]]
PROFILE_KEYS = FIXED_KEYS + VARIABLE_KEYS
OWNER_EXTRA_FIELDS = {"first_name": dict(label="نام", icon="🏷", q="نام شاگرد رو بفرست.")}

# ══ Small helpers ═══════════════════════════════════════════════════════════
def e(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_digits(value) -> str:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value).translate(trans).replace("٬", "").replace(",", "").replace("٫", ".").strip()


def money(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def is_owner(uid) -> bool:
    try:
        return int(uid) in OWNER_IDS
    except (TypeError, ValueError):
        return False


def status_label(s) -> str:
    return STATUS_LABELS.get(s or "", s or "نامشخص")


def full_name(c: dict | None) -> str:
    return " ".join(filter(None, [(c or {}).get("first_name"), (c or {}).get("last_name")])) or "بدون نام"


def short(text, n=28) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def btn(text, data=None, url=None) -> dict:
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = data
    return b


def chunk(buttons: list, cols: int) -> list:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def card(title: str, *lines: str) -> str:
    body = "\n".join(l for l in lines if l is not None)
    return f"<b>{title}</b>\n{LINE}\n{body}"


def card_digits(raw: str) -> str:
    return re.sub(r"\D", "", norm_digits(raw))


def fmt_card(number: str) -> str:
    d = card_digits(number)
    return " ".join(d[i:i + 4] for i in range(0, len(d), 4))

# ══ Telegram layer ══════════════════════════════════════════════════════════
class TGError(Exception):
    def __init__(self, method, code, description):
        super().__init__(f"Telegram {method} failed [{code}]: {description}")
        self.method, self.code, self.description = method, code, description or ""


def tg(method: str, payload: dict | None = None, timeout: int = 9):
    try:
        r = HTTP.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=(3, timeout))
    except requests.RequestException as ex:
        raise TGError(method, 0, str(ex)) from ex
    try:
        data = r.json()
    except ValueError:
        raise TGError(method, r.status_code, "invalid response")
    if not data.get("ok"):
        raise TGError(method, data.get("error_code", r.status_code), data.get("description", ""))
    return data.get("result")


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


def send(chat_id, text, kb=None, reply_kb=None):
    payload = {"chat_id": int(chat_id), "text": str(text), "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": True}}
    if kb:
        payload["reply_markup"] = {"inline_keyboard": kb}
    elif reply_kb:
        payload["reply_markup"] = reply_kb
    if len(payload["text"]) > 4090:
        payload["text"] = payload["text"][:4000]
    try:
        return tg("sendMessage", payload)
    except TGError as ex:
        if ex.code == 400 and "parse" in ex.description.lower():
            payload.pop("parse_mode")
            payload["text"] = _strip_tags(payload["text"])[:4096]
            return tg("sendMessage", payload)
        raise


def safe_send(chat_id, text, kb=None) -> bool:
    """Send to someone who may have blocked the bot; never raises."""
    try:
        send(chat_id, text, kb)
        return True
    except Exception as ex:
        log.info("safe_send to %s failed: %s", chat_id, ex)
        return False


def notify_owners(text, kb=None):
    for oid in OWNER_IDS:
        safe_send(oid, text, kb)


def screen(chat_id, text, kb=None):
    """Show a screen. Inside a callback, edit the clicked message in place; otherwise send."""
    mid = getattr(CTX, "mid", None)
    if mid:
        CTX.mid = None
        payload = {"chat_id": int(chat_id), "message_id": int(mid), "text": str(text)[:4090],
                   "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                   "reply_markup": {"inline_keyboard": kb or []}}
        try:
            tg("editMessageText", payload)
            return
        except TGError as ex:
            if "not modified" in ex.description.lower():
                return
            log.debug("edit failed (%s); sending new message", ex.description)
    send(chat_id, text, kb)


def answer_callback(callback_id: str, text: str = "", alert: bool = False):
    if not callback_id:
        return
    try:
        tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190], "show_alert": alert}, timeout=5)
    except Exception:
        log.debug("answerCallbackQuery failed", exc_info=True)


def send_file_id(chat_id, file_id, kind, caption="", kb=None):
    if not file_id:
        return
    method, key = ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")
    payload = {"chat_id": int(chat_id), key: file_id, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption[:1000]
    if kb:
        payload["reply_markup"] = {"inline_keyboard": kb}
    return tg(method, payload)


def send_document_bytes(chat_id, filename: str, content: bytes, caption: str = ""):
    r = HTTP.post(f"{TELEGRAM_API}/sendDocument", data={"chat_id": int(chat_id), "caption": caption[:900]},
                  files={"document": (filename, content)}, timeout=(5, 60))
    r.raise_for_status()

# ══ Database layer ══════════════════════════════════════════════════════════
def data_of(query):
    return query.execute().data or []


def count_of(query) -> int:
    res = query.execute()
    return int(getattr(res, "count", None) or len(res.data or []))


def fetch_all(table: str, page: int = 1000) -> list:
    rows, start = [], 0
    while True:
        part = data_of(sb.table(table).select("*").range(start, start + page - 1))
        rows += part
        if len(part) < page:
            return rows
        start += page


def get_customer(uid):
    rows = data_of(sb.table("customers").select("*").eq("telegram_user_id", int(uid)).limit(1))
    return rows[0] if rows else None


_KNOWN: dict[int, tuple] = {}  # uid -> (username, first, last, ts): avoids a DB write on every message


def ensure_customer(tg_user: dict):
    uid = int(tg_user["id"])
    sig = (tg_user.get("username") or "", tg_user.get("first_name") or "", tg_user.get("last_name") or "")
    cached = _KNOWN.get(uid)
    if cached and cached[:3] == sig and time.time() - cached[3] < 600:
        return
    existing = get_customer(uid)
    if not existing:
        row = {"telegram_user_id": uid, "username": sig[0], "first_name": sig[1], "last_name": sig[2],
               "role": "owner" if is_owner(uid) else "customer"}
        data_of(sb.table("customers").insert(row))
    else:
        upd = {k: v for k, v in zip(("username", "first_name", "last_name"), sig) if v and existing.get(k) != v}
        if is_owner(uid) and existing.get("role") != "owner":
            upd["role"] = "owner"
        if upd:
            upd["updated_at"] = now_iso()
            data_of(sb.table("customers").update(upd).eq("telegram_user_id", uid))
    _KNOWN[uid] = (*sig, time.time())


def update_customer(uid, fields: dict):
    fields = {**fields, "updated_at": now_iso()}
    data_of(sb.table("customers").update(fields).eq("telegram_user_id", int(uid)))


def get_state(uid):
    rows = data_of(sb.table("user_states").select("state,data").eq("telegram_user_id", int(uid)).limit(1))
    return rows[0] if rows else {"state": "idle", "data": {}}


def set_state(uid, state: str, data: dict | None = None):
    data_of(sb.table("user_states").upsert(
        {"telegram_user_id": int(uid), "state": state, "data": data or {}, "updated_at": now_iso()},
        on_conflict="telegram_user_id"))


def clear_state(uid):
    set_state(uid, "idle", {})


# ── settings (card number etc.) ──
_SETTINGS: dict = {"ts": 0, "data": {}}


def settings() -> dict:
    if time.time() - _SETTINGS["ts"] > 20:
        try:
            _SETTINGS["data"] = {r["key"]: r.get("value") for r in data_of(sb.table("settings").select("key,value"))}
            _SETTINGS["ts"] = time.time()
        except Exception:
            log.exception("settings load failed")
    return _SETTINGS["data"]


def set_setting(key: str, value: str):
    data_of(sb.table("settings").upsert({"key": key, "value": value, "updated_at": now_iso()}, on_conflict="key"))
    _SETTINGS["ts"] = 0


def clear_setting(key: str):
    data_of(sb.table("settings").delete().eq("key", key))
    _SETTINGS["ts"] = 0


# ── required channels ──
_CHANNELS: dict = {"ts": 0, "data": []}
_JOIN_OK: dict[int, float] = {}


def channels() -> list:
    if time.time() - _CHANNELS["ts"] > 30:
        try:
            rows = data_of(sb.table("required_channels").select("*").order("created_at", desc=False).limit(50))
            have = {r["chat_ref"].lower() for r in rows}
            for ref, title in LOCKED_CHANNELS:  # locked channels always present, even if the row went missing
                if ref.lower() not in have:
                    rows.insert(0, {"id": None, "chat_ref": ref, "title": title, "locked": True})
            _CHANNELS["data"], _CHANNELS["ts"] = rows, time.time()
        except Exception:
            log.exception("channels load failed")
            if not _CHANNELS["data"]:
                return [{"id": None, "chat_ref": r, "title": t, "locked": True} for r, t in LOCKED_CHANNELS]
    return _CHANNELS["data"]


def channel_url(ch: dict) -> str | None:
    ref = ch.get("chat_ref") or ""
    if ref.startswith("@"):
        return f"https://t.me/{ref[1:]}"
    return ch.get("invite_link") or None


def ensure_locked_channels():
    for ref, title in LOCKED_CHANNELS:
        try:
            if not data_of(sb.table("required_channels").select("id").eq("chat_ref", ref).limit(1)):
                data_of(sb.table("required_channels").insert({"chat_ref": ref, "title": title, "locked": True}))
        except Exception:
            log.warning("could not ensure locked channel %s", ref, exc_info=True)


def missing_channels(uid: int, force: bool = False) -> list:
    if is_owner(uid):
        return []
    if not force and time.time() - _JOIN_OK.get(uid, 0) < 60:
        return []
    missing = []
    for ch in channels():
        try:
            m = tg("getChatMember", {"chat_id": ch["chat_ref"], "user_id": int(uid)}, timeout=6)
            st = m.get("status")
            if st in ("left", "kicked") or (st == "restricted" and not m.get("is_member", True)):
                missing.append(ch)
        except TGError as ex:
            # Not verifiable (bot isn't admin there, or the target isn't a channel, e.g. a bot). Never lock users out.
            log.warning("cannot verify membership in %s: %s", ch.get("chat_ref"), ex.description)
    if not missing:
        _JOIN_OK[uid] = time.time()
    return missing


def show_join_gate(chat_id, missing: list):
    kb = []
    for ch in missing:
        url = channel_url(ch)
        if url:
            kb.append([btn(f"📢 {short(ch.get('title') or ch['chat_ref'], 30)}", url=url)])
    kb.append([btn("✅ عضو شدم، ادامه", "joined")])
    send(chat_id, card("📢 عضویت الزامی",
                       "برای استفاده از ربات اول در کانال‌های زیر عضو شو 👇",
                       "بعد از عضویت، دکمه «عضو شدم» رو بزن."), kb)

# ── data deletion (really removes rows) ──
def purge_customer(uid: int):
    uid = int(uid)
    for table, col in (("orders", "telegram_user_id"), ("programs", "customer_user_id"),
                       ("testimonials", "telegram_user_id"), ("feedback", "telegram_user_id"),
                       ("user_states", "telegram_user_id"), ("customers", "telegram_user_id")):
        data_of(sb.table(table).delete().eq(col, uid))
    _KNOWN.pop(uid, None)
    _JOIN_OK.pop(uid, None)


def purge_order(order_id: int):
    data_of(sb.table("orders").delete().eq("id", int(order_id)))

# ══ Customer screens ════════════════════════════════════════════════════════
def reply_kb(uid):
    row = [{"text": HOME_BTN}]
    if is_owner(uid):
        row.append({"text": ADMIN_BTN})
    return {"keyboard": [row], "resize_keyboard": True, "is_persistent": True}


def main_keyboard(uid):
    kb = [
        [btn("🛒 سفارش برنامه جدید", "buy")],
        [btn("📦 سفارش‌ها و پرداخت", "my:orders"), btn("📚 برنامه‌های من", "my:programs")],
        [btn("👤 پروفایل من", "my:profile"), btn("🏆 سوابق و نظرات", "hist")],
        [btn("💬  مشاوره و پشتیبانی", "support"), btn("📩 پیشنهاد و انتقاد", "feedback")],
    ]
    if is_owner(uid):
        kb.append([btn(ADMIN_BTN, "a:menu")])
    return kb


def main_menu(chat_id, uid, name="", fresh=False):
    text = card("🌟 Darya FitBot | 📝برنامه اختصاصی تمرین و تغذیه",
                f"سلام {e(name) or 'دوست عزیز'} 👋",
                "از همین‌جا می‌تونی برنامه سفارش بدی، پرداخت کنی و برنامه‌ات رو دریافت کنی.",
                "<i>همه‌چیز با چند کلیک ساده 👇</i>")
    if fresh:
        send(chat_id, "✨ خوش اومدی!", reply_kb=reply_kb(uid))
        send(chat_id, text, main_keyboard(uid))
    else:
        screen(chat_id, text, main_keyboard(uid))


def services_for(active_only=True):
    q = sb.table("services").select("*")
    if active_only:
        q = q.eq("active", True)
    return data_of(q.order("created_at", desc=False).limit(60))


def show_buy(chat_id):
    services = services_for()
    if not services:
        screen(chat_id, card("🛒 سفارش برنامه", "فعلاً خدمتی فعال نیست. بعداً سر بزن 🌱"), [[btn("🔙 منو", "home")]])
        return
    kb = [[btn(f"{CAT_LABELS.get(s['category'], '✨').split()[0]} {short(s['name'], 26)} · {money(s['price'])} ت", f"svc:{s['id']}")]
          for s in services]
    kb.append([btn("🔙 منو", "home")])
    screen(chat_id, card("🛒 سفارش برنامه", "یکی از برنامه‌ها رو انتخاب کن 👇"), kb)


def service_detail(chat_id, sid):
    rows = data_of(sb.table("services").select("*").eq("id", sid).eq("active", True).limit(1))
    if not rows:
        screen(chat_id, "این خدمت دیگه در دسترس نیست.", [[btn("🔙 خدمات", "buy")]])
        return
    s = rows[0]
    screen(chat_id, card(f"✨ {e(s['name'])}", e(s.get("description") or "—"), "",
                         f"💰 <b>هزینه:</b> {money(s['price'])} تومان"),
           [[btn("📝 ثبت سفارش", f"order:{sid}")], [btn("🔙 بازگشت", "buy")]])


# ── smart form ──
def fmt_value(key, v):
    f = FIELDS.get(key, {})
    if v in (None, ""):
        return "—"
    return f"{v} {f['unit']}" if f.get("unit") else str(v)


def validate_value(key, raw):
    f = FIELDS.get(key) or OWNER_EXTRA_FIELDS.get(key) or {}
    v = str(raw).strip()
    if not v:
        return None, "لطفاً یک پاسخ بفرست."
    kind = f.get("kind")
    if kind in ("int", "float"):
        try:
            num = float(norm_digits(v))
        except ValueError:
            return None, "لطفاً فقط عدد بفرست. مثال: ۷۲"
        num = int(num) if kind == "int" else round(num, 1)
        if not f["lo"] <= num <= f["hi"]:
            return None, f"عدد باید بین {f['lo']} تا {f['hi']} باشه."
        return num, None
    return v[:300], None


def question_kb(key, customer, in_form, cancel_cb):
    f = FIELDS.get(key, {})
    kb = chunk([btn(o, f"fv:{i}") for i, o in enumerate(f.get("opts", []))], f.get("cols", 2))
    prev = (customer or {}).get(key)
    if in_form and not f.get("fixed") and prev not in (None, ""):
        kb.append([btn(f"✅ همون قبلی: {short(fmt_value(key, prev), 22)}", "fv:keep")])
    kb.append([btn("✖️ لغو", cancel_cb)])
    return kb


def ask_question(chat_id, uid, st):
    fields, i = st["fields"], st["i"]
    key = fields[i]
    f = FIELDS[key]
    bar = "▰" * (i + 1) + "▱" * (len(fields) - i - 1)
    text = card(f"{f['icon']} {f['label']}", f"{bar}  <i>{i + 1} از {len(fields)}</i>", "", e(f["q"]))
    screen(chat_id, text, question_kb(key, get_customer(uid), True, "form:cancel"))


def start_order(chat_id, uid, sid):
    rows = data_of(sb.table("services").select("*").eq("id", sid).eq("active", True).limit(1))
    if not rows:
        screen(chat_id, "این خدمت پیدا نشد یا غیرفعال شده.", [[btn("🔙 خدمات", "buy")]])
        return
    s = rows[0]
    # drop abandoned half-filled orders so nothing stale stays in the DB
    data_of(sb.table("orders").delete().eq("telegram_user_id", uid).eq("status", "collecting_info"))
    created = data_of(sb.table("orders").insert({
        "telegram_user_id": uid, "service_id": s["id"], "service_name": s["name"], "category": s["category"],
        "price": int(s["price"]), "status": "collecting_info", "payment_status": "unpaid", "customer_info": {}}))
    if not created:
        screen(chat_id, "ثبت سفارش انجام نشد. دوباره تلاش کن.", [[btn("🔙 منو", "home")]])
        return
    customer = get_customer(uid) or {}
    fields = [k for k in FIXED_KEYS if customer.get(k) in (None, "")] + VARIABLE_KEYS
    st = {"mode": "order", "order_id": created[0]["id"], "fields": fields, "i": 0, "info": {}}
    set_state(uid, "form", st)
    returning = len(fields) == len(VARIABLE_KEYS)
    note = "اطلاعات ثابتت از قبل ثبت شده؛ فقط چند مورد تغییرپذیر رو می‌پرسم ✨" if returning else "فقط یک‌بار این اطلاعات رو می‌پرسم؛ دفعه بعد سریع‌تره ✨"
    send(chat_id, card(f"✅ سفارش #{created[0]['id']} ثبت شد", e(s["name"]), f"<i>{note}</i>"))
    ask_question(chat_id, uid, st)


def advance_form(uid, chat_id, st, value, keep=False):
    key = st["fields"][st["i"]]
    if not keep:
        st["info"][key] = value
    st["i"] += 1
    if st["i"] < len(st["fields"]):
        set_state(uid, "form", st)
        ask_question(chat_id, uid, st)
        return
    finish_form(uid, chat_id, st)


def finish_form(uid, chat_id, st):
    if st["info"]:
        update_customer(uid, st["info"])
    customer = get_customer(uid) or {}
    snapshot = {k: customer.get(k) for k in PROFILE_KEYS}
    data_of(sb.table("orders").update({"customer_info": snapshot, "status": "awaiting_receipt",
                                       "payment_status": "unpaid", "updated_at": now_iso()})
            .eq("id", st["order_id"]).eq("telegram_user_id", uid))
    clear_state(uid)
    send(chat_id, "✅ اطلاعاتت ثبت شد. حالا مرحله پرداخت 👇")
    show_payment(chat_id, uid, st["order_id"])


# ── payment & receipt ──
def show_payment(chat_id, uid, order_id):
    rows = data_of(sb.table("orders").select("*").eq("id", order_id).eq("telegram_user_id", uid).limit(1))
    if not rows:
        screen(chat_id, "سفارش پیدا نشد.", [[btn("📦 سفارش‌ها", "my:orders")]])
        return
    o = rows[0]
    if o["status"] != "awaiting_receipt":
        show_my_order(chat_id, uid, order_id)
        return
    s = settings()
    number = card_digits(s.get("card_number") or "")
    lines = [f"🛍 <b>خدمت:</b> {e(o['service_name'])}", f"💰 <b>مبلغ:</b> {money(o['price'])} تومان", ""]
    if number:
        lines += ["💳 <b>کارت‌به‌کارت کن به:</b>", f"<code>{fmt_card(number)}</code>  <i>(با لمس کپی می‌شه)</i>"]
        if s.get("card_holder"):
            lines.append(f"👤 به نام: <b>{e(s['card_holder'])}</b>")
        if s.get("card_bank"):
            lines.append(f"🏦 بانک: {e(s['card_bank'])}")
        lines += ["", "📸 بعد از پرداخت، دکمه «ارسال رسید» رو بزن و عکس رسید (یا شماره پیگیری) رو بفرست."]
    else:
        lines += ["⚠️ شماره کارت هنوز ثبت نشده. به پشتیبانی پیام بده تا اطلاعات پرداخت رو بفرستن."]
        notify_owners("⚠️ یک شاگرد به صفحه پرداخت رسید ولی شماره کارت ثبت نشده است.\nاز «پنل مدیریت ← پرداخت» ثبتش کن.")
    kb = [[btn("📤 ارسال رسید پرداخت", f"rcpt:{order_id}")],
          [btn("💬 پشتیبانی", "support"), btn("🗑 حذف سفارش", f"od:ask:{order_id}")],
          [btn("📦 سفارش‌های من", "my:orders")]]
    screen(chat_id, card(f"💳 پرداخت سفارش #{order_id}", *lines), kb)


def submit_receipt(uid, chat_id, order, file_id=None, file_kind=None, text=""):
    data_of(sb.table("orders").update({
        "status": "pending_review", "payment_status": "receipt_submitted", "receipt_file_id": file_id,
        "receipt_file_kind": file_kind, "receipt_text": text[:500] or None, "updated_at": now_iso()}).eq("id", order["id"]))
    clear_state(uid)
    send(chat_id, card(f"🧾 رسید سفارش #{order['id']} دریافت شد", "برای مربی ارسال شد؛ به محض بررسی خبرت می‌کنم ✅"),
         [[btn("📦 سفارش‌های من", "my:orders"), btn("🏠 منو", "home")]])
    c = get_customer(uid) or {}
    cap = (f"🧾 <b>رسید جدید</b> · سفارش #{order['id']}\n🛍 {e(order['service_name'])}\n💰 {money(order['price'])} تومان\n"
           f"👤 {e(full_name(c))} · <code>{uid}</code>")
    if text:
        cap += f"\n📝 {e(text[:300])}"
    oid = order["id"]
    kb = [[btn("✅ تأیید · ۳ روز", f"a:ap:{oid}:3d"), btn("✅ تأیید · ۴ روز", f"a:ap:{oid}:4d")],
          [btn("✅ تأیید · یک هفته", f"a:ap:{oid}:1w"), btn("🕒 زمان دلخواه", f"a:apx:{oid}")],
          [btn("❌ رد رسید", f"a:rj:{oid}"), btn("📋 جزئیات", f"a:ov:{oid}")]]
    for owner in OWNER_IDS:
        try:
            if file_id:
                send_file_id(owner, file_id, file_kind or "document", cap, kb)
            else:
                send(owner, cap, kb)
        except Exception:
            log.exception("could not deliver receipt to owner %s", owner)
            safe_send(owner, cap, kb)


# ── customer: orders ──
def show_my_orders(chat_id, uid):
    orders = data_of(sb.table("orders").select("id,service_name,price,status").eq("telegram_user_id", uid)
                     .neq("status", "collecting_info").order("created_at", desc=True).limit(10))
    if not orders:
        screen(chat_id, card("📦 سفارش‌های من", "هنوز سفارشی نداری 🌱"),
               [[btn("🛒 سفارش برنامه", "buy")], [btn("🔙 منو", "home")]])
        return
    kb = [[btn(f"#{o['id']} · {short(o['service_name'], 20)} · {status_label(o['status']).split()[0]}", f"my:o:{o['id']}")] for o in orders]
    kb.append([btn("🔙 منو", "home")])
    screen(chat_id, card("📦 سفارش‌های من", "برای جزئیات، پرداخت یا حذف روی سفارش بزن 👇"), kb)


def show_my_order(chat_id, uid, order_id):
    rows = data_of(sb.table("orders").select("*").eq("id", order_id).eq("telegram_user_id", uid).limit(1))
    if not rows:
        screen(chat_id, "سفارش پیدا نشد (شاید حذف شده).", [[btn("📦 سفارش‌ها", "my:orders")]])
        return
    o = rows[0]
    lines = [f"🛍 {e(o['service_name'])}", f"💰 {money(o['price'])} تومان", f"📍 وضعیت: {status_label(o['status'])}"]
    if o.get("delivery_eta"):
        lines.append(f"🕒 زمان تحویل: {e(o['delivery_eta'])}")
    lines.append(f"📅 {str(o.get('created_at') or '')[:10]}")
    kb = []
    if o["status"] == "awaiting_receipt":
        kb.append([btn("💳 پرداخت و ارسال رسید", f"pay:{order_id}")])
    if o["status"] == "delivered":
        kb.append([btn("📚 دیدن برنامه‌ام", "my:programs")])
    kb.append([btn("🗑 حذف سفارش", f"od:ask:{order_id}")])
    kb.append([btn("🔙 سفارش‌ها", "my:orders")])
    screen(chat_id, card(f"📦 سفارش #{order_id}", *lines), kb)


def ask_delete_my_order(chat_id, uid, order_id):
    rows = data_of(sb.table("orders").select("id,status,service_name").eq("id", order_id).eq("telegram_user_id", uid).limit(1))
    if not rows:
        screen(chat_id, "سفارش پیدا نشد.", [[btn("📦 سفارش‌ها", "my:orders")]])
        return
    o = rows[0]
    warn = ""
    if o["status"] in ("in_progress", "pending_review", "approved"):
        warn = "\n⚠️ این سفارش پرداخت/در حال انجام است؛ با حذف آن دیگر قابل بازگشت نیست."
    screen(chat_id, card(f"🗑 حذف سفارش #{order_id}", f"{e(o['service_name'])}",
                         "با حذف، اطلاعات این سفارش (از جمله رسید) برای همیشه از سیستم پاک می‌شود." + warn),
           [[btn("🗑 بله، حذف شود", f"od:yes:{order_id}")], [btn("↩️ نه، برگرد", f"my:o:{order_id}")]])


def delete_my_order(chat_id, uid, order_id):
    rows = data_of(sb.table("orders").select("id,status,service_name").eq("id", order_id).eq("telegram_user_id", uid).limit(1))
    if rows:
        purge_order(order_id)
        st = get_state(uid)
        if (st.get("data") or {}).get("order_id") == order_id:
            clear_state(uid)
        if rows[0]["status"] != "collecting_info":
            notify_owners(f"🗑 یک شاگرد سفارش #{order_id} ({e(rows[0]['service_name'])}) را حذف کرد.")
    screen(chat_id, card("✅ سفارش حذف شد", "اطلاعات آن برای همیشه پاک شد."),
           [[btn("📦 سفارش‌های من", "my:orders"), btn("🏠 منو", "home")]])


# ── customer: programs / profile / info ──
def show_my_programs(chat_id, uid):
    progs = data_of(sb.table("programs").select("*").eq("customer_user_id", uid).eq("active", True)
                    .order("updated_at", desc=True).limit(20))
    if not progs:
        screen(chat_id, card("📚 برنامه‌های من", "هنوز برنامه‌ای برات ثبت نشده.", "بعد از تأیید پرداخت و آماده شدن، همین‌جا می‌بینیش 🌱"),
               [[btn("🛒 سفارش برنامه", "buy")], [btn("🔙 منو", "home")]])
        return
    screen(chat_id, card("📚 برنامه‌های من", "برنامه‌هات رو پایین می‌فرستم 👇"), [[btn("🔙 منو", "home")]])
    for p in progs:
        title = PROGRAM_TYPES.get(p["program_type"], p.get("title") or "برنامه")
        send(chat_id, card(title, f"<i>به‌روزرسانی: {str(p.get('updated_at') or '')[:10]}</i>", "", e((p.get("body") or "")[:3500]) or "📎 فایل برنامه پایین‌تر است"))
        if p.get("file_id") and p.get("file_kind"):
            try:
                send_file_id(chat_id, p["file_id"], p["file_kind"], caption=title)
            except Exception:
                log.exception("program file send failed")


def profile_text(c: dict | None, title="👤 پروفایل") -> str:
    lines = []
    for key in PROFILE_KEYS:
        f = FIELDS[key]
        mark = "🔒" if f["fixed"] else "🔄"
        lines.append(f"{f['icon']} {f['label']}: <b>{e(fmt_value(key, (c or {}).get(key)))}</b>  {mark}")
    lines.append("")
    lines.append("<i>🔒 ثابت (فقط یک‌بار پرسیده می‌شه) · 🔄 هر سفارش به‌روز می‌شه</i>")
    return card(title, *lines)


def show_profile(chat_id, uid):
    c = get_customer(uid)
    un = f"@{c['username']}" if c and c.get("username") else "—"
    text = profile_text(c) + f"\n\n🆔 <code>{uid}</code> · {e(un)}"
    screen(chat_id, text, [[btn("✏️ ویرایش مشخصات", "my:edit")],
                           [btn("🗑 حذف همه اطلاعات من", "my:del")], [btn("🔙 منو", "home")]])


def show_edit_menu(chat_id, back="my:profile", prefix="pf"):
    kb = chunk([btn(f"{FIELDS[k]['icon']} {FIELDS[k]['label']}", f"{prefix}:{k}") for k in PROFILE_KEYS], 2)
    return kb + [[btn("🔙 بازگشت", back)]]


def ask_edit_field(chat_id, uid, target, key, by_owner):
    f = FIELDS.get(key) or OWNER_EXTRA_FIELDS[key]
    set_state(uid, "edit_field", {"target": int(target), "field": key, "by_owner": bool(by_owner)})
    cur = (get_customer(target) or {}).get(key)
    cancel = f"a:cv:{target}" if by_owner else "my:profile"
    kb = question_kb(key, None, False, "cancel") if key in FIELDS else [[btn("✖️ لغو", "cancel")]]
    send(chat_id, card(f"{f['icon']} {f['label']}", f"مقدار فعلی: <b>{e(fmt_value(key, cur))}</b>", "", e(f["q"])), kb)


def finish_edit_field(uid, chat_id, st, value):
    target, key = int(st["target"]), st["field"]
    update_customer(target, {key: value})
    clear_state(uid)
    if st.get("by_owner"):
        send(chat_id, "✅ ذخیره شد.")
        customer_detail(chat_id, target)
    else:
        send(chat_id, "✅ ذخیره شد.")
        show_profile(chat_id, uid)


def show_history(chat_id):
    items = data_of(sb.table("portfolio_items").select("title,body").eq("active", True).order("created_at", desc=True).limit(10))
    reviews = data_of(sb.table("testimonials").select("display_name,review_text").eq("approved", True).order("created_at", desc=True).limit(10))
    parts = ["<b>🏆 سوابق مربی</b>\n" + LINE]
    parts += [f"• <b>{e(i['title'])}</b>\n{e(i['body'])}" for i in items] or ["هنوز نمونه‌کاری ثبت نشده."]
    parts.append("\n<b>⭐ نظرات شاگردان</b>\n" + LINE)
    parts += [f"• <b>{e(r.get('display_name') or 'شاگرد')}</b>: {e(r['review_text'])}" for r in reviews] or ["هنوز نظری منتشر نشده."]
    screen(chat_id, "\n\n".join(parts)[:4000], [[btn("✍️ ثبت نظر من", "review")], [btn("🔙 منو", "home")]])


def show_support(chat_id):
    screen(chat_id, card("💬 پشتیبانی", "هر سؤال یا مشکلی داشتی مستقیم به پشتیبانی پیام بده 🙌", f"@{SUPPORT_USERNAME}"),
           [[btn("💬 ارسال پیام به پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}")], [btn("🔙 منو", "home")]])

# ══ Owner panel ═════════════════════════════════════════════════════════════
PAGE = 8
ORDER_FILTERS = {
    "all": ("📋 همه", None), "pending": ("🧾 رسید جدید", ["pending_review"]),
    "wait": ("💳 منتظر پرداخت", ["awaiting_receipt"]), "active": ("⏳ در حال انجام", ["in_progress", "approved"]),
    "done": ("✅ تحویل‌شده", ["delivered"]),
}


def back_btn(cb="a:menu", text="🔙 پنل مدیریت"):
    return [btn(text, cb)]


def owner_menu(chat_id):
    pend = count_of(sb.table("orders").select("id", count="exact").eq("status", "pending_review").limit(1))
    kb = [
        [btn(f"🧾 رسیدهای جدید ({pend})", "a:ol:pending:0")],
        [btn("📋 سفارش‌ها", "a:ol:all:0"), btn("👥 شاگردان", "a:cl:0")],
        [btn("🛍 خدمات و قیمت", "a:sl"), btn("💳 پرداخت", "a:pay")],
        [btn("📢 کانال‌های اجباری", "a:ch"), btn("📣 پیام همگانی", "a:bc")],
        [btn("🏆 سوابق و نظرات", "a:pf"), btn("📊 آمار", "a:stats")],
        [btn("💾 پشتیبان‌گیری", "a:bk"), btn("🔙 منوی اصلی", "home")],
    ]
    screen(chat_id, card("⚙️ پنل مدیریت", "همه‌چیز با یک لمس 👇",
                         "<i>هر بخش دکمه‌های حذف و ویرایش خودش رو داره.</i>"), kb)


# ── orders ──
def owner_orders(chat_id, flt: str, page: int):
    label, statuses = ORDER_FILTERS.get(flt, ORDER_FILTERS["all"])
    q = sb.table("orders").select("id,telegram_user_id,service_name,price,status").order("created_at", desc=True)
    q = q.in_("status", statuses) if statuses else q.neq("status", "collecting_info")
    rows = data_of(q.range(page * PAGE, page * PAGE + PAGE))
    has_next = len(rows) > PAGE
    rows = rows[:PAGE]
    fkb = chunk([btn(("• " if k == flt else "") + v[0], f"a:ol:{k}:0") for k, v in ORDER_FILTERS.items()], 3)
    kb = list(fkb)
    for o in rows:
        kb.append([btn(f"#{o['id']} · {short(o['service_name'], 16)} · {money(o['price'])} · {status_label(o['status']).split()[0]}", f"a:ov:{o['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️ قبلی", f"a:ol:{flt}:{page - 1}"))
    if has_next:
        nav.append(btn("بعدی ➡️", f"a:ol:{flt}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(back_btn())
    body = "سفارشی در این بخش نیست." if not rows else "برای مدیریت هر سفارش روی آن بزن 👇"
    screen(chat_id, card(f"📋 سفارش‌ها · {label}", body), kb)


def order_card(chat_id, o):
    c = get_customer(o["telegram_user_id"]) or {}
    info = o.get("customer_info") or {}
    snap = {k: info.get(k) if info.get(k) not in (None, "") else c.get(k) for k in PROFILE_KEYS}
    prof = "\n".join(f"{FIELDS[k]['icon']} {FIELDS[k]['label']}: {e(fmt_value(k, snap[k]))}" for k in PROFILE_KEYS)
    un = f"@{c['username']}" if c.get("username") else "—"
    lines = [f"🛍 {e(o['service_name'])}", f"💰 {money(o['price'])} تومان",
             f"📍 {status_label(o['status'])}", f"💳 پرداخت: {e(o.get('payment_status') or '—')}"]
    if o.get("delivery_eta"):
        lines.append(f"🕒 زمان تحویل: {e(o['delivery_eta'])}")
    lines += ["", f"👤 {e(full_name(c))} · {e(un)} · <code>{o['telegram_user_id']}</code>", "", prof]
    oid = o["id"]
    kb = []
    if o["status"] == "pending_review":
        kb += [[btn("✅ تأیید · ۳ روز", f"a:ap:{oid}:3d"), btn("✅ تأیید · ۴ روز", f"a:ap:{oid}:4d")],
               [btn("✅ تأیید · یک هفته", f"a:ap:{oid}:1w"), btn("🕒 زمان دلخواه", f"a:apx:{oid}")],
               [btn("❌ رد رسید", f"a:rj:{oid}")]]
    if o.get("receipt_file_id") or o.get("receipt_text"):
        kb.append([btn("🧾 دیدن رسید", f"a:rc:{oid}")])
    kb.append([btn("📨 ارسال/ویرایش برنامه", f"a:dv:{oid}"), btn("🔁 تغییر وضعیت", f"a:os:{oid}")])
    kb.append([btn("💰 تغییر مبلغ", f"a:oe:{oid}:price"), btn("🕒 تغییر زمان", f"a:oe:{oid}:eta")])
    kb.append([btn("👤 شاگرد", f"a:cv:{o['telegram_user_id']}"), btn("🗑 حذف کامل", f"a:dl:order:{oid}")])
    kb.append(back_btn("a:ol:all:0", "🔙 سفارش‌ها"))
    screen(chat_id, card(f"📋 سفارش #{oid}", *lines), kb)


def view_order(chat_id, oid):
    rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
    if rows:
        order_card(chat_id, rows[0])
    else:
        screen(chat_id, "سفارش پیدا نشد (شاید حذف شده).", [back_btn("a:ol:all:0", "🔙 سفارش‌ها")])


def approve_order(chat_id, oid, eta):
    rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
    if not rows:
        send(chat_id, "سفارش پیدا نشد.")
        return
    o = rows[0]
    if o["status"] != "pending_review":
        send(chat_id, f"سفارش #{oid} دیگه در وضعیت «بررسی رسید» نیست ({status_label(o['status'])}).")
        return
    data_of(sb.table("orders").update({"status": "in_progress", "payment_status": "approved", "delivery_eta": eta,
                                       "approved_at": now_iso(), "updated_at": now_iso()}).eq("id", oid))
    safe_send(o["telegram_user_id"], card(f"✅ پرداخت سفارش #{oid} تأیید شد",
                                          f"🛍 {e(o['service_name'])}", f"🕒 زمان آماده‌شدن: <b>{e(eta)}</b>",
                                          "", "برنامه‌ات رو همین‌جا برات می‌فرستم. ممنون از اعتمادت ❤️"),
              [[btn("📦 پیگیری سفارش", f"my:o:{oid}")]])
    send(chat_id, card(f"✅ سفارش #{oid} تأیید شد", f"زمان اعلام‌شده: {e(eta)}"),
         [[btn("📨 ارسال برنامه", f"a:dv:{oid}")], [btn("📋 جزئیات", f"a:ov:{oid}"), btn("⚙️ پنل", "a:menu")]])


def reject_order(chat_id, oid):
    rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
    if not rows:
        send(chat_id, "سفارش پیدا نشد.")
        return
    o = rows[0]
    data_of(sb.table("orders").update({"status": "awaiting_receipt", "payment_status": "rejected",
                                       "updated_at": now_iso()}).eq("id", oid))
    safe_send(o["telegram_user_id"], card(f"⚠️ رسید سفارش #{oid} تأیید نشد",
                                          "رسید واضح نبود یا مبلغ مطابقت نداشت.", "لطفاً رسید صحیح رو دوباره بفرست 🙏"),
              [[btn("📤 ارسال رسید جدید", f"rcpt:{oid}")], [btn("💬 پشتیبانی", "support")]])
    send(chat_id, f"❌ رسید سفارش #{oid} رد شد و به شاگرد اطلاع داده شد.", [[btn("📋 جزئیات", f"a:ov:{oid}")]])


def show_receipt(chat_id, oid):
    rows = data_of(sb.table("orders").select("receipt_file_id,receipt_file_kind,receipt_text,service_name").eq("id", oid).limit(1))
    if not rows or not (rows[0].get("receipt_file_id") or rows[0].get("receipt_text")):
        send(chat_id, "رسیدی ثبت نشده.")
        return
    r = rows[0]
    cap = f"🧾 رسید سفارش #{oid}"
    if r.get("receipt_text"):
        cap += f"\n📝 {e(r['receipt_text'])}"
    if r.get("receipt_file_id"):
        send_file_id(chat_id, r["receipt_file_id"], r.get("receipt_file_kind") or "document", cap)
    else:
        send(chat_id, cap)


# ── customers ──
def owner_customers(chat_id, page: int):
    rows = data_of(sb.table("customers").select("telegram_user_id,username,first_name,last_name,role")
                   .neq("role", "owner").order("updated_at", desc=True).range(page * PAGE, page * PAGE + PAGE))
    has_next = len(rows) > PAGE
    rows = rows[:PAGE]
    kb = [[btn("🔎 جستجو", "a:cs"), btn("➕ افزودن شاگرد", "a:ca")]]
    for c in rows:
        un = f" @{c['username']}" if c.get("username") else ""
        kb.append([btn(short(f"{full_name(c)}{un} · {c['telegram_user_id']}", 40), f"a:cv:{c['telegram_user_id']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️ قبلی", f"a:cl:{page - 1}"))
    if has_next:
        nav.append(btn("بعدی ➡️", f"a:cl:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(back_btn())
    screen(chat_id, card("👥 شاگردان", "هنوز شاگردی ثبت نشده." if not rows else "یکی رو برای مدیریت انتخاب کن 👇"), kb)


def customer_detail(chat_id, uid):
    c = get_customer(uid)
    if not c:
        screen(chat_id, card("👤 شاگرد پیدا نشد", f"آیدی <code>{uid}</code> در دیتابیس نیست.",
                             "از «افزودن شاگرد» می‌تونی دستی اضافه‌اش کنی."), [back_btn("a:cl:0", "🔙 شاگردان")])
        return
    n_orders = count_of(sb.table("orders").select("id", count="exact").eq("telegram_user_id", uid).neq("status", "collecting_info").limit(1))
    n_progs = count_of(sb.table("programs").select("id", count="exact").eq("customer_user_id", uid).limit(1))
    un = f"@{c['username']}" if c.get("username") else "—"
    text = (profile_text(c, f"👤 {e(full_name(c))}") +
            f"\n\n🆔 <code>{uid}</code> · {e(un)}\n📦 سفارش‌ها: {n_orders} · 📚 برنامه‌ها: {n_progs}")
    kb = [[btn("📚 برنامه‌ها", f"a:cp:{uid}"), btn("📦 سفارش‌ها", f"a:co:{uid}")],
          [btn("✏️ ویرایش مشخصات", f"a:ce:{uid}"), btn("💬 پیام به شاگرد", f"a:cm:{uid}")],
          [btn("🗑 حذف کامل شاگرد", f"a:dl:cust:{uid}")], back_btn("a:cl:0", "🔙 شاگردان")]
    screen(chat_id, text, kb)


def owner_edit_menu(chat_id, uid):
    kb = chunk([btn(f"{FIELDS[k]['icon']} {FIELDS[k]['label']}", f"a:cf:{uid}:{k}") for k in PROFILE_KEYS], 2)
    kb.insert(0, [btn("🏷 نام", f"a:cf:{uid}:first_name")])
    kb.append(back_btn(f"a:cv:{uid}", "🔙 شاگرد"))
    screen(chat_id, card("✏️ ویرایش مشخصات", f"شاگرد <code>{uid}</code> · کدوم مورد رو عوض کنم؟"), kb)


def owner_customer_orders(chat_id, uid):
    rows = data_of(sb.table("orders").select("id,service_name,price,status").eq("telegram_user_id", uid)
                   .neq("status", "collecting_info").order("created_at", desc=True).limit(15))
    kb = [[btn(f"#{o['id']} · {short(o['service_name'], 18)} · {status_label(o['status']).split()[0]}", f"a:ov:{o['id']}")] for o in rows]
    kb.append(back_btn(f"a:cv:{uid}", "🔙 شاگرد"))
    screen(chat_id, card("📦 سفارش‌های شاگرد", "سفارشی ندارد." if not rows else "برای مدیریت انتخاب کن 👇"), kb)


def owner_programs(chat_id, uid):
    progs = {p["program_type"]: p for p in data_of(sb.table("programs").select("program_type,updated_at").eq("customer_user_id", uid))}
    kb = []
    for t, label in PROGRAM_TYPES.items():
        mark = "✅" if t in progs else "▫️"
        kb.append([btn(f"{mark} {label}", f"a:cpt:{uid}:{t}")])
    kb.append(back_btn(f"a:cv:{uid}", "🔙 شاگرد"))
    screen(chat_id, card("📚 برنامه‌های شاگرد", f"شاگرد <code>{uid}</code>", "✅ = ثبت شده · ▫️ = خالی"), kb)


def owner_program_type(chat_id, uid, ptype):
    rows = data_of(sb.table("programs").select("*").eq("customer_user_id", uid).eq("program_type", ptype).limit(1))
    kb = [[btn("✏️ ثبت / ویرایش و ارسال", f"a:cpe:{uid}:{ptype}")]]
    if rows:
        kb.append([btn("🗑 حذف این برنامه", f"a:dl:prog:{uid}_{ptype}")])
    kb.append(back_btn(f"a:cp:{uid}", "🔙 برنامه‌ها"))
    screen(chat_id, card(PROGRAM_TYPES[ptype], "برنامه‌ای ثبت نشده." if not rows else f"آخرین به‌روزرسانی: {str(rows[0].get('updated_at') or '')[:10]}"), kb)
    if rows:
        p = rows[0]
        if p.get("body"):
            send(chat_id, e(p["body"][:3800]))
        if p.get("file_id"):
            try:
                send_file_id(chat_id, p["file_id"], p.get("file_kind") or "document")
            except Exception:
                log.exception("program file preview failed")


def save_program(owner_id, target, ptype, body, file_id=None, file_kind=None, order_id=None):
    title = PROGRAM_TYPES.get(ptype, "برنامه")
    data_of(sb.table("programs").upsert({
        "customer_user_id": int(target), "program_type": ptype, "title": title, "body": body[:10000],
        "file_id": file_id, "file_kind": file_kind, "active": True, "updated_by": int(owner_id),
        "updated_at": now_iso()}, on_conflict="customer_user_id,program_type"))
    sent = safe_send(target, card(f"🎉 {title}", "مربی برنامه‌ات رو آماده کرد!", "", e(body[:3000]) or "📎 فایل برنامه پایین‌تره 👇"),
                     [[btn("📚 برنامه‌های من", "my:programs")]])
    if sent and file_id:
        try:
            send_file_id(target, file_id, file_kind or "document", caption=title)
        except Exception:
            log.exception("could not send program file")
    if order_id:
        target_orders = data_of(sb.table("orders").select("id").eq("id", int(order_id)).limit(1))
    else:
        target_orders = data_of(sb.table("orders").select("id").eq("telegram_user_id", int(target)).eq("category", ptype)
                                .in_("status", ["in_progress", "approved"]).order("created_at", desc=True).limit(1))
    if target_orders:
        data_of(sb.table("orders").update({"status": "delivered", "program_text": body[:10000], "program_file_id": file_id,
                                           "delivered_at": now_iso(), "updated_at": now_iso()}).eq("id", target_orders[0]["id"]))
    send(owner_id, card("✅ ذخیره و ارسال شد", f"{title} برای <code>{target}</code>" + ("" if sent else "\n⚠️ شاگرد ربات رو بلاک کرده یا هنوز /start نزده؛ فقط ذخیره شد.")),
         [[btn("👤 شاگرد", f"a:cv:{target}"), btn("⚙️ پنل", "a:menu")]])


# ── services ──
def owner_services(chat_id):
    rows = services_for(active_only=False)
    kb = [[btn(f"{'✅' if s['active'] else '⏸'} {short(s['name'], 22)} · {money(s['price'])}", f"a:sv:{s['id']}")] for s in rows]
    kb.append([btn("➕ افزودن خدمت", "a:sa")])
    kb.append(back_btn())
    screen(chat_id, card("🛍 خدمات و قیمت", "خدمتی ثبت نشده." if not rows else "برای ویرایش یا حذف، روی خدمت بزن 👇"), kb)


def owner_service(chat_id, sid):
    rows = data_of(sb.table("services").select("*").eq("id", sid).limit(1))
    if not rows:
        screen(chat_id, "خدمت پیدا نشد.", [back_btn("a:sl", "🔙 خدمات")])
        return
    s = rows[0]
    text = card(f"🛍 {e(s['name'])}", f"🗂 دسته: {CAT_LABELS.get(s['category'], s['category'])}", f"💰 قیمت: {money(s['price'])} تومان",
                f"وضعیت: {'✅ فعال' if s['active'] else '⏸ غیرفعال'}", "", e(s.get("description") or "—"))
    kb = [[btn("✏️ نام", f"a:se:{sid}:name"), btn("💰 قیمت", f"a:se:{sid}:price")],
          [btn("📝 توضیحات", f"a:se:{sid}:description")],
          chunk([btn(("• " if s["category"] == k else "") + v, f"a:sc:{sid}:{k}") for k, v in CAT_LABELS.items()], 3)[0],
          [btn("⏸ غیرفعال‌سازی" if s["active"] else "▶️ فعال‌سازی", f"a:st:{sid}"), btn("🗑 حذف", f"a:dl:svc:{sid}")],
          back_btn("a:sl", "🔙 خدمات")]
    screen(chat_id, text, kb)


# ── payment settings ──
def owner_payment(chat_id):
    s = settings()
    number = card_digits(s.get("card_number") or "")
    lines = [f"💳 شماره کارت: <code>{fmt_card(number)}</code>" if number else "💳 شماره کارت: <i>ثبت نشده</i>",
             f"👤 نام صاحب کارت: {e(s.get('card_holder') or '—')}", f"🏦 بانک: {e(s.get('card_bank') or '—')}",
             "", "<i>مشتری‌ها همین اطلاعات رو موقع پرداخت می‌بینن.</i>"]
    kb = [[btn("💳 ثبت / تغییر شماره کارت", "a:pe:card")],
          [btn("👤 نام صاحب کارت", "a:pe:holder"), btn("🏦 بانک", "a:pe:bank")]]
    if number:
        kb.append([btn("🗑 حذف اطلاعات کارت", "a:pe:clear")])
    kb.append(back_btn())
    screen(chat_id, card("💳 تنظیمات پرداخت", *lines), kb)


# ── required channels ──
def owner_channels(chat_id):
    kb = []
    for ch in channels():
        locked = ch.get("locked") or ch["chat_ref"].lower() in LOCKED_REFS
        if locked:
            kb.append([btn(f"🔒 {short(ch.get('title') or ch['chat_ref'], 24)} · {ch['chat_ref']}", "a:chl")])
        else:
            kb.append([btn(f"🗑 {short(ch.get('title') or ch['chat_ref'], 24)} · {ch['chat_ref']}", f"a:dl:chan:{ch['id']}")])
    kb.append([btn("➕ افزودن کانال", "a:cha")])
    kb.append(back_btn())
    screen(chat_id, card("📢 کانال‌های اجباری",
                         "شاگردها باید در این کانال‌ها عضو باشن تا از ربات استفاده کنن.",
                         "🔒 = قفل‌شده؛ قابل حذف نیست.", "برای افزودن، ربات باید <b>ادمین</b> کانال باشه.",
                         "<i>برای حذف روی 🗑 بزن.</i>"), kb)


_BOT_ID: dict = {}


def bot_id() -> int:
    if "id" not in _BOT_ID:
        _BOT_ID["id"] = int(tg("getMe")["id"])
    return _BOT_ID["id"]


def add_channel(chat_id, raw: str):
    ref = raw.strip()
    ref = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", ref).strip("/ ")
    ref = ref.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", ref):
        send(chat_id, "آیدی کانال معتبر نیست. مثل «@mychannel» بفرست (فقط کانال‌های عمومی).")
        return False
    ref = "@" + ref
    if ref.lower() in {c["chat_ref"].lower() for c in channels()}:
        send(chat_id, "این کانال از قبل در فهرست هست.")
        return False
    try:
        chat = tg("getChat", {"chat_id": ref})
        if chat.get("type") not in ("channel", "supergroup", "group"):
            send(chat_id, "این آیدی کانال یا گروه نیست.")
            return False
        me = tg("getChatMember", {"chat_id": ref, "user_id": bot_id()})
        if me.get("status") not in ("administrator", "creator"):
            send(chat_id, "ربات هنوز ادمین این کانال نیست. اول ربات رو ادمین کن و دوباره بفرست.")
            return False
    except TGError as ex:
        send(chat_id, f"کانال پیدا نشد یا ربات به آن دسترسی ندارد.\n<code>{e(ex.description)}</code>")
        return False
    data_of(sb.table("required_channels").insert({"chat_ref": ref, "title": (chat.get("title") or ref)[:80], "locked": False}))
    _CHANNELS["ts"] = 0
    return True


# ── portfolio / reviews ──
def owner_portfolio(chat_id):
    items = data_of(sb.table("portfolio_items").select("id,title").order("created_at", desc=True).limit(20))
    kb = [[btn(f"🗑 {short(i['title'], 34)}", f"a:dl:port:{i['id']}")] for i in items]
    kb.append([btn("➕ افزودن سابقه", "a:pfa"), btn("⭐ نظرات شاگردان", "a:rv")])
    kb.append(back_btn())
    screen(chat_id, card("🏆 سوابق و نمونه‌کار", "برای حذف روی 🗑 بزن." if items else "هنوز چیزی ثبت نشده."), kb)


def owner_reviews(chat_id):
    rows = data_of(sb.table("testimonials").select("id,display_name,review_text,approved").order("created_at", desc=True).limit(15))
    kb = [[btn(f"{'✅' if r['approved'] else '⏳'} {short(r.get('display_name') or 'شاگرد', 10)}: {short(r['review_text'], 22)}", f"a:rvv:{r['id']}")] for r in rows]
    kb.append(back_btn("a:pf", "🔙 سوابق"))
    screen(chat_id, card("⭐ نظرات شاگردان", "⏳ = در انتظار انتشار · ✅ = منتشرشده" if rows else "نظری نیست."), kb)


def owner_review(chat_id, rid):
    rows = data_of(sb.table("testimonials").select("*").eq("id", rid).limit(1))
    if not rows:
        screen(chat_id, "نظر پیدا نشد.", [back_btn("a:rv", "🔙 نظرات")])
        return
    r = rows[0]
    kb = []
    if not r["approved"]:
        kb.append([btn("✅ انتشار", f"a:rva:{rid}")])
    kb.append([btn("🗑 حذف", f"a:dl:rev:{rid}")])
    kb.append(back_btn("a:rv", "🔙 نظرات"))
    screen(chat_id, card(f"⭐ {e(r.get('display_name') or 'شاگرد')}", e(r["review_text"]), "",
                         "✅ منتشرشده" if r["approved"] else "⏳ در انتظار انتشار"), kb)


# ── stats ──
def owner_stats(chat_id):
    n_c = count_of(sb.table("customers").select("telegram_user_id", count="exact").neq("role", "owner").limit(1))
    orders = fetch_all("orders")
    by = defaultdict(int)
    revenue = 0
    for o in orders:
        by[o["status"]] += 1
        if o.get("payment_status") == "approved":
            revenue += int(o.get("price") or 0)
    lines = [f"👥 شاگردان: <b>{n_c}</b>", f"📦 کل سفارش‌ها: <b>{sum(v for k, v in by.items() if k != 'collecting_info')}</b>", ""]
    lines += [f"{status_label(s)}: <b>{by[s]}</b>" for s in STATUS_ORDER if by.get(s)]
    lines += ["", f"💰 درآمد تأییدشده: <b>{money(revenue)}</b> تومان"]
    screen(chat_id, card("📊 آمار", *lines), [back_btn()])


# ── backup ──
def owner_backup(chat_id):
    dump = {"created_at": now_iso()}
    for table in ("customers", "orders", "programs", "services", "portfolio_items", "testimonials", "feedback", "required_channels", "settings"):
        try:
            dump[table] = fetch_all(table)
        except Exception:
            log.exception("backup of %s failed", table)
            dump[table] = "error"
    content = json.dumps(dump, ensure_ascii=False, indent=1, default=str).encode("utf-8")
    send_document_bytes(chat_id, f"fitbot-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json", content,
                        "💾 پشتیبان کامل اطلاعات. این فایل محرمانه است؛ امن نگهش دار.")


# ── broadcast ──
def run_broadcast(owner_id, from_chat, mid, ids):
    ok = fail = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                tg("copyMessage", {"chat_id": uid, "from_chat_id": from_chat, "message_id": mid}, timeout=10)
                ok += 1
                break
            except TGError as ex:
                if ex.code == 429 and attempt == 1:
                    m = re.search(r"retry after (\d+)", ex.description)
                    time.sleep(min(30, int(m.group(1)) + 1) if m else 3)
                    continue
                fail += 1
                break
        time.sleep(0.05)
    safe_send(owner_id, card("📣 ارسال همگانی تمام شد", f"✅ موفق: <b>{ok}</b>", f"⚠️ ناموفق (بلاک/غیرفعال): <b>{fail}</b>"),
              [back_btn()])


# ── deletion (kind → confirm → really delete) ──
DEL_INFO = {
    "order": ("سفارش", "a:ov:{id}", "a:ol:all:0"),
    "cust": ("شاگرد و تمام اطلاعاتش (پروفایل، سفارش‌ها، برنامه‌ها، رسیدها، نظرها)", "a:cv:{id}", "a:cl:0"),
    "svc": ("خدمت", "a:sv:{id}", "a:sl"),
    "port": ("این سابقه", "a:pf", "a:pf"),
    "rev": ("این نظر", "a:rvv:{id}", "a:rv"),
    "prog": ("این برنامه", "a:cp:{uid}", "a:cp:{uid}"),
    "chan": ("این کانال اجباری", "a:ch", "a:ch"),
}


def _del_ctx(kind, ident):
    uid = ident.split("_")[0] if kind == "prog" else ident
    back, after = DEL_INFO[kind][1], DEL_INFO[kind][2]
    return back.format(id=ident, uid=uid), after.format(id=ident, uid=uid)


def ask_delete(chat_id, kind, ident):
    if kind not in DEL_INFO:
        return
    if kind == "chan":
        rows = data_of(sb.table("required_channels").select("*").eq("id", int(ident)).limit(1))
        if not rows or rows[0].get("locked") or rows[0]["chat_ref"].lower() in LOCKED_REFS:
            screen(chat_id, "🔒 این کانال قفل است و قابل حذف نیست.", [back_btn("a:ch", "🔙 کانال‌ها")])
            return
    back, _ = _del_ctx(kind, ident)
    screen(chat_id, card("🗑 تأیید حذف", f"{DEL_INFO[kind][0]} برای <b>همیشه</b> از سیستم پاک می‌شود و برگشت ندارد.", "مطمئنی؟"),
           [[btn("🗑 بله، پاک شود", f"a:dy:{kind}:{ident}")], [btn("↩️ انصراف", back)]])


def do_delete(chat_id, kind, ident):
    _, after = _del_ctx(kind, ident)
    msg = "✅ برای همیشه پاک شد."
    if kind == "order":
        purge_order(int(ident))
    elif kind == "cust":
        if is_owner(int(ident)):
            screen(chat_id, "حساب مالک قابل حذف نیست.", [back_btn("a:cl:0", "🔙 شاگردان")])
            return
        purge_customer(int(ident))
        msg += "\n(اگر شاگرد دوباره /start بزنه، یک حساب خالی و جدید ساخته می‌شود.)"
    elif kind == "svc":
        data_of(sb.table("services").delete().eq("id", int(ident)))
    elif kind == "port":
        data_of(sb.table("portfolio_items").delete().eq("id", int(ident)))
    elif kind == "rev":
        data_of(sb.table("testimonials").delete().eq("id", int(ident)))
    elif kind == "prog":
        uid, ptype = ident.split("_", 1)
        data_of(sb.table("programs").delete().eq("customer_user_id", int(uid)).eq("program_type", ptype))
    elif kind == "chan":
        rows = data_of(sb.table("required_channels").select("*").eq("id", int(ident)).limit(1))
        if not rows or rows[0].get("locked") or rows[0]["chat_ref"].lower() in LOCKED_REFS:
            screen(chat_id, "🔒 این کانال قفل است و قابل حذف نیست.", [back_btn("a:ch", "🔙 کانال‌ها")])
            return
        data_of(sb.table("required_channels").delete().eq("id", int(ident)))
        _CHANNELS["ts"] = 0
    screen(chat_id, card("🗑 حذف انجام شد", msg), [[btn("🔙 ادامه", after)]])

# ══ Message handling ════════════════════════════════════════════════════════
def cancel_flow(uid, chat_id):
    st = get_state(uid)
    d = st.get("data") or {}
    if st.get("state") == "form" and d.get("mode") == "order" and d.get("order_id"):
        data_of(sb.table("orders").delete().eq("id", d["order_id"]).eq("telegram_user_id", uid).eq("status", "collecting_info"))
    clear_state(uid)


def handle_message(message: dict):
    user = message.get("from") or {}
    if not user.get("id") or not message.get("chat") or message["chat"].get("type") != "private":
        return
    uid, chat_id = int(user["id"]), int(message["chat"]["id"])
    ensure_customer(user)
    text = (message.get("text") or message.get("caption") or "").strip()
    photos = message.get("photo") or []
    doc = message.get("document") or None
    file_id = photos[-1].get("file_id") if photos else (doc or {}).get("file_id")
    file_kind = "photo" if photos else ("document" if doc else None)

    miss = missing_channels(uid)
    if miss:
        show_join_gate(chat_id, miss)
        return

    if text.startswith(("/start", "/menu", "/help")) or text == HOME_BTN:
        clear_state(uid)
        main_menu(chat_id, uid, user.get("first_name") or "", fresh=text.startswith("/start"))
        return
    if text.startswith("/cancel") or text in ("لغو", "✖️ لغو"):
        cancel_flow(uid, chat_id)
        send(chat_id, "✖️ لغو شد.", [[btn("🏠 منو", "home")]])
        return
    if is_owner(uid) and (text.startswith("/admin") or text == ADMIN_BTN):
        clear_state(uid)
        owner_menu(chat_id)
        return

    st = get_state(uid)
    state, d = st.get("state", "idle"), st.get("data") or {}

    # ── customer states ──
    if state == "form":
        if not text:
            send(chat_id, "لطفاً جواب رو به‌صورت متن بفرست یا از دکمه‌ها استفاده کن.")
            return
        key = d["fields"][d["i"]]
        value, err = validate_value(key, text)
        if err:
            send(chat_id, f"⚠️ {err}")
            return
        advance_form(uid, chat_id, d, value)
        return
    if state == "edit_field":
        if not text:
            send(chat_id, "لطفاً مقدار رو به‌صورت متن بفرست.")
            return
        value, err = validate_value(d["field"], text)
        if err:
            send(chat_id, f"⚠️ {err}")
            return
        if d.get("by_owner") and not is_owner(uid):
            clear_state(uid)
            return
        finish_edit_field(uid, chat_id, d, value)
        return
    if state == "receipt":
        rows = data_of(sb.table("orders").select("*").eq("id", d.get("order_id", 0)).eq("telegram_user_id", uid).limit(1))
        if not rows or rows[0]["status"] != "awaiting_receipt":
            clear_state(uid)
            send(chat_id, "این سفارش دیگه منتظر رسید نیست.", [[btn("📦 سفارش‌ها", "my:orders")]])
            return
        if file_id:
            submit_receipt(uid, chat_id, rows[0], file_id, file_kind, text)
        elif len(text) >= 4:
            submit_receipt(uid, chat_id, rows[0], None, None, text)
        else:
            send(chat_id, "📸 عکس رسید یا شماره پیگیری رو بفرست.")
        return
    if state == "review":
        if not text:
            send(chat_id, "لطفاً نظرت رو به‌صورت متن بفرست.")
            return
        rows = data_of(sb.table("testimonials").insert({"telegram_user_id": uid, "display_name": (user.get("first_name") or "شاگرد")[:80],
                                                        "review_text": text[:1200], "approved": False}))
        clear_state(uid)
        send(chat_id, "ممنون از نظرت ❤️ بعد از بررسی مربی منتشر می‌شه.", [[btn("🏠 منو", "home")]])
        rid = rows[0]["id"] if rows else 0
        notify_owners(card("⭐ نظر جدید", e(text[:1200]), f"از: <code>{uid}</code>"),
                      [[btn("✅ انتشار", f"a:rva:{rid}"), btn("🗑 حذف", f"a:dl:rev:{rid}")]])
        return
    if state == "feedback":
        data_of(sb.table("feedback").insert({"telegram_user_id": uid, "message": text or "[پیام غیرمتنی]"}))
        clear_state(uid)
        notify_owners(card("📩 پیام جدید از شاگرد", e(text or "[پیام غیرمتنی]"), f"از: <code>{uid}</code>"),
                      [[btn("↩️ پاسخ", f"a:cm:{uid}")]])
        send(chat_id, "ممنون از پیامت ❤️", [[btn("🏠 منو", "home")]])
        return

    # ── owner states ──
    if is_owner(uid) and state.startswith("o_"):
        if handle_owner_state(uid, chat_id, state, d, message, text, file_id, file_kind):
            return

    # ── loose receipt (file sent without pressing the button) ──
    if file_id:
        rows = data_of(sb.table("orders").select("*").eq("telegram_user_id", uid).eq("status", "awaiting_receipt")
                       .order("created_at", desc=True).limit(1))
        if rows:
            submit_receipt(uid, chat_id, rows[0], file_id, file_kind, text)
            return
        send(chat_id, "سفارشِ منتظر رسید پیدا نشد. اول از منو یک برنامه سفارش بده.", [[btn("🛒 سفارش برنامه", "buy")]])
        return
    if text and not text.startswith("/"):
        send(chat_id, "از دکمه‌های زیر استفاده کن 👇", main_keyboard(uid))


def _ask(chat_id, title, prompt, cancel_cb="cancel"):
    send(chat_id, card(title, prompt), [[btn("✖️ لغو", cancel_cb)]])


def handle_owner_state(uid, chat_id, state, d, message, text, file_id, file_kind) -> bool:
    if state == "o_eta":
        if len(text) < 2:
            send(chat_id, "زمان رو واضح‌تر بنویس؛ مثلاً «۵ روز کاری».")
            return True
        clear_state(uid)
        approve_order(chat_id, int(d["order_id"]), text[:60])
        return True

    if state == "o_search":
        t = norm_digits(text)
        clear_state(uid)
        if t.isdigit():
            customer_detail(chat_id, int(t))
            return True
        t = re.sub(r"[^\w\u0600-\u06FF ]", "", text.lstrip("@")).strip()
        rows = data_of(sb.table("customers").select("telegram_user_id,username,first_name,last_name")
                       .or_(f"username.ilike.%{t}%,first_name.ilike.%{t}%,last_name.ilike.%{t}%").limit(10)) if t else []
        if not rows:
            send(chat_id, "نتیجه‌ای پیدا نشد.", [[btn("🔎 جستجوی دوباره", "a:cs"), btn("👥 شاگردان", "a:cl:0")]])
            return True
        kb = [[btn(short(f"{full_name(c)} · {c['telegram_user_id']}", 40), f"a:cv:{c['telegram_user_id']}")] for c in rows]
        send(chat_id, card("🔎 نتیجه جستجو", f"{len(rows)} مورد"), kb)
        return True

    if state == "o_addcust":
        parts = norm_digits(text).split(None, 1)
        raw = text.split(None, 1)
        if not parts or not parts[0].isdigit():
            send(chat_id, "آیدی عددی رو بفرست؛ مثل: <code>123456789 علی رضایی</code>")
            return True
        new_id = int(parts[0])
        if not get_customer(new_id):
            data_of(sb.table("customers").insert({"telegram_user_id": new_id, "first_name": (raw[1] if len(raw) > 1 else "")[:80],
                                                  "username": "", "last_name": "", "role": "customer", "added_by_owner": True}))
        clear_state(uid)
        send(chat_id, "✅ شاگرد اضافه شد. (برای دریافت پیام، خودش باید یک‌بار /start بزنه.)")
        customer_detail(chat_id, new_id)
        return True

    if state == "o_msg":
        target = int(d["target"])
        try:
            tg("copyMessage", {"chat_id": target, "from_chat_id": chat_id, "message_id": message["message_id"]})
            clear_state(uid)
            send(chat_id, "✅ پیام ارسال شد.", [[btn("👤 شاگرد", f"a:cv:{target}")]])
        except TGError:
            clear_state(uid)
            send(chat_id, "⚠️ ارسال نشد؛ شاگرد ربات رو بلاک کرده یا هنوز /start نزده.", [[btn("👤 شاگرد", f"a:cv:{target}")]])
        return True

    if state == "o_prog":
        if not text and not file_id:
            send(chat_id, "لطفاً متن، عکس یا فایل برنامه رو بفرست.")
            return True
        clear_state(uid)
        save_program(uid, int(d["target"]), d["type"], text, file_id, file_kind, d.get("order_id"))
        return True

    if state == "o_oedit":
        oid, field = int(d["order_id"]), d["field"]
        if field == "price":
            n = norm_digits(text)
            if not n.isdigit() or int(n) < 0:
                send(chat_id, "مبلغ باید عدد (تومان) باشه.")
                return True
            upd = {"price": int(n)}
        else:
            if len(text) < 2:
                send(chat_id, "متن زمان رو بنویس؛ مثلاً «۴ روز».")
                return True
            upd = {"delivery_eta": text[:60]}
        data_of(sb.table("orders").update({**upd, "updated_at": now_iso()}).eq("id", oid))
        clear_state(uid)
        send(chat_id, "✅ ذخیره شد.")
        view_order(chat_id, oid)
        return True

    if state == "o_svc_add":
        step = d.get("step", "name")
        if step == "name":
            if not text:
                send(chat_id, "نام خدمت رو بنویس.")
                return True
            d.update(step="price", name=text[:120])
            set_state(uid, state, d)
            _ask(chat_id, "➕ خدمت جدید", "💰 قیمت رو به تومان بفرست (فقط عدد).")
        elif step == "price":
            n = norm_digits(text)
            if not n.isdigit() or int(n) < 1:
                send(chat_id, "قیمت باید یک عدد مثبت باشه.")
                return True
            d.update(step="cat", price=int(n))
            set_state(uid, state, d)
            send(chat_id, card("➕ خدمت جدید", "🗂 دسته رو انتخاب کن:"),
                 [chunk([btn(v, f"a:sac:{k}") for k, v in CAT_LABELS.items()], 3)[0], [btn("✖️ لغو", "cancel")]])
        elif step == "desc":
            create_service(uid, chat_id, d, "" if text.strip() == "-" else text)
        else:
            send(chat_id, "دسته رو از دکمه‌ها انتخاب کن 👆")
        return True

    if state == "o_svc_edit":
        sid, field = int(d["id"]), d["field"]
        if field == "price":
            n = norm_digits(text)
            if not n.isdigit() or int(n) < 1:
                send(chat_id, "قیمت باید یک عدد مثبت باشه.")
                return True
            upd = {"price": int(n)}
        elif field == "name":
            if not text:
                send(chat_id, "نام نباید خالی باشه.")
                return True
            upd = {"name": text[:120]}
        else:
            upd = {"description": ("" if text.strip() == "-" else text)[:1500]}
        data_of(sb.table("services").update({**upd, "updated_at": now_iso()}).eq("id", sid))
        clear_state(uid)
        send(chat_id, "✅ ذخیره شد.")
        owner_service(chat_id, sid)
        return True

    if state == "o_card":
        field = d["field"]
        key = {"card": "card_number", "holder": "card_holder", "bank": "card_bank"}[field]
        if field == "card":
            num = card_digits(text)
            if len(num) != 16:
                send(chat_id, "شماره کارت باید ۱۶ رقم باشه. دوباره بفرست.")
                return True
            set_setting(key, num)
        elif text.strip() == "-":
            clear_setting(key)
        else:
            set_setting(key, text[:80])
        clear_state(uid)
        send(chat_id, "✅ ذخیره شد.")
        owner_payment(chat_id)
        return True

    if state == "o_chan":
        if add_channel(chat_id, text):
            clear_state(uid)
            send(chat_id, "✅ کانال اضافه شد.")
            owner_channels(chat_id)
        return True

    if state == "o_bc":
        ids = [int(c["telegram_user_id"]) for c in fetch_all("customers") if c.get("role") != "owner"]
        set_state(uid, "o_bc_confirm", {"from_chat": chat_id, "mid": message["message_id"], "n": len(ids)})
        send(chat_id, card("📣 تأیید ارسال همگانی", f"این پیام برای <b>{len(ids)}</b> شاگرد فرستاده می‌شه.", "ارسال کنم؟"),
             [[btn(f"✅ ارسال برای {len(ids)} نفر", "a:bcok")], [btn("✖️ لغو", "cancel")]])
        return True

    if state == "o_pf_add":
        if d.get("step", "title") == "title":
            if not text:
                send(chat_id, "عنوان رو بنویس.")
                return True
            set_state(uid, state, {"step": "body", "title": text[:160]})
            _ask(chat_id, "➕ سابقه جدید", "📝 حالا توضیحات رو بفرست.")
        else:
            if not text:
                send(chat_id, "توضیحات رو بنویس.")
                return True
            data_of(sb.table("portfolio_items").insert({"title": d["title"], "body": text[:2000], "active": True}))
            clear_state(uid)
            send(chat_id, "✅ ثبت شد.")
            owner_portfolio(chat_id)
        return True
    return False


def create_service(uid, chat_id, d, desc):
    data_of(sb.table("services").insert({"category": d["cat"], "name": d["name"], "price": d["price"],
                                         "description": desc[:1500], "active": True}))
    clear_state(uid)
    send(chat_id, "✅ خدمت جدید اضافه شد.")
    owner_services(chat_id)

# ══ Callback handling ═══════════════════════════════════════════════════════
def handle_option(uid, chat_id, arg):
    st = get_state(uid)
    state, d = st.get("state"), st.get("data") or {}
    if state == "form":
        key = d["fields"][d["i"]]
        if arg == "keep":
            advance_form(uid, chat_id, d, None, keep=True)
            return
    elif state == "edit_field":
        key = d["field"]
    else:
        screen(chat_id, "این سؤال منقضی شده.", [[btn("🏠 منو", "home")]])
        return
    opts = FIELDS.get(key, {}).get("opts", [])
    try:
        value = opts[int(arg)]
    except (ValueError, IndexError):
        return
    value, err = validate_value(key, value)
    if err:
        send(chat_id, f"⚠️ {err}")
        return
    if state == "form":
        advance_form(uid, chat_id, d, value)
    else:
        finish_edit_field(uid, chat_id, d, value)


def handle_callback(q: dict):
    user = q.get("from") or {}
    if not user.get("id"):
        return
    uid = int(user["id"])
    msg = q.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id", uid))
    CTX.mid = msg.get("message_id")
    data, cid = q.get("data") or "", q.get("id", "")
    ensure_customer(user)

    if data == "joined":
        miss = missing_channels(uid, force=True)
        if miss:
            answer_callback(cid, "هنوز عضو همه کانال‌ها نشدی ❗", True)
            return
        answer_callback(cid, "✅ عضویتت تأیید شد")
        main_menu(chat_id, uid, user.get("first_name") or "", fresh=True)
        return
    miss = missing_channels(uid)
    if miss:
        answer_callback(cid, "اول باید در کانال‌ها عضو بشی ❗", True)
        show_join_gate(chat_id, miss)
        return
    if data == "a:chl":
        answer_callback(cid, "🔒 این کانال قفل است و هیچ‌کس نمی‌تونه حذفش کنه.", True)
        return
    answer_callback(cid)

    if data.startswith("a:"):
        if is_owner(uid):
            handle_owner_callback(uid, chat_id, data.split(":"))
        else:
            send(chat_id, "⛔ این بخش فقط برای مالک است.")
        return

    name = user.get("first_name") or ""
    if data == "home":
        clear_state(uid)
        main_menu(chat_id, uid, name)
    elif data == "cancel":
        cancel_flow(uid, chat_id)
        main_menu(chat_id, uid, name)
    elif data == "form:cancel":
        cancel_flow(uid, chat_id)
        screen(chat_id, card("✖️ لغو شد", "سفارش نیمه‌کاره حذف شد."), [[btn("🛒 سفارش برنامه", "buy"), btn("🏠 منو", "home")]])
    elif data == "buy":
        show_buy(chat_id)
    elif data.startswith("svc:"):
        service_detail(chat_id, int(data[4:]))
    elif data.startswith("order:"):
        start_order(chat_id, uid, int(data[6:]))
    elif data.startswith("fv:"):
        handle_option(uid, chat_id, data[3:])
    elif data == "my:orders":
        show_my_orders(chat_id, uid)
    elif data.startswith("my:o:"):
        show_my_order(chat_id, uid, int(data[5:]))
    elif data.startswith("pay:"):
        if get_state(uid).get("state") == "receipt":
            clear_state(uid)
        show_payment(chat_id, uid, int(data[4:]))
    elif data.startswith("rcpt:"):
        oid = int(data[5:])
        rows = data_of(sb.table("orders").select("id,status").eq("id", oid).eq("telegram_user_id", uid).limit(1))
        if not rows or rows[0]["status"] != "awaiting_receipt":
            screen(chat_id, "این سفارش منتظر رسید نیست.", [[btn("📦 سفارش‌ها", "my:orders")]])
            return
        set_state(uid, "receipt", {"order_id": oid})
        screen(chat_id, card(f"📤 ارسال رسید سفارش #{oid}", "📸 عکس رسید رو بفرست", "یا شماره پیگیری رو بنویس."),
               [[btn("↩️ برگشت به پرداخت", f"pay:{oid}")]])
    elif data.startswith("od:ask:"):
        ask_delete_my_order(chat_id, uid, int(data[7:]))
    elif data.startswith("od:yes:"):
        delete_my_order(chat_id, uid, int(data[7:]))
    elif data == "my:programs":
        show_my_programs(chat_id, uid)
    elif data == "my:profile":
        clear_state(uid)
        show_profile(chat_id, uid)
    elif data == "my:edit":
        screen(chat_id, card("✏️ ویرایش مشخصات", "کدوم مورد رو عوض کنم؟"), show_edit_menu(chat_id))
    elif data.startswith("pf:"):
        key = data[3:]
        if key in FIELDS:
            ask_edit_field(chat_id, uid, uid, key, False)
    elif data == "my:del":
        screen(chat_id, card("🗑 حذف همه اطلاعات من", "پروفایل، سفارش‌ها، رسیدها و برنامه‌هات برای <b>همیشه</b> پاک می‌شن و برگشت ندارن."),
               [[btn("🗑 بله، همه‌چیز پاک شود", "my:delok")], [btn("↩️ انصراف", "my:profile")]])
    elif data == "my:delok":
        if is_owner(uid):
            screen(chat_id, "حساب مالک قابل حذف نیست.", [[btn("🏠 منو", "home")]])
            return
        purge_customer(uid)
        screen(chat_id, card("✅ همه اطلاعاتت پاک شد", "هر وقت خواستی با /start دوباره شروع کن."), [])
    elif data == "hist":
        show_history(chat_id)
    elif data == "review":
        set_state(uid, "review", {})
        screen(chat_id, card("✍️ ثبت نظر", "تجربه‌ات رو در پیام بعدی بنویس."), [[btn("✖️ لغو", "cancel")]])
    elif data == "feedback":
        set_state(uid, "feedback", {})
        screen(chat_id, card("📩 پیشنهاد و انتقاد", "پیامت رو در پیام بعدی بنویس؛ مستقیم به دست مربی می‌رسه."), [[btn("✖️ لغو", "cancel")]])
    elif data == "support":
        show_support(chat_id)


def handle_owner_callback(uid, chat_id, p: list):
    act = p[1] if len(p) > 1 else ""
    arg = p[2] if len(p) > 2 else ""

    def prompt(state, sdata, title, text, back="a:menu"):
        set_state(uid, state, sdata)
        screen(chat_id, card(title, text), [[btn("✖️ لغو", "cancel")]])

    if act == "menu":
        clear_state(uid)
        owner_menu(chat_id)
    # orders
    elif act == "ol":
        owner_orders(chat_id, arg, int(p[3]))
    elif act == "ov":
        view_order(chat_id, int(arg))
    elif act == "ap":
        approve_order(chat_id, int(arg), ETA_CODES[p[3]])
    elif act == "apx":
        prompt("o_eta", {"order_id": int(arg)}, f"🕒 تأیید سفارش #{arg}", "زمان آماده‌سازی رو بنویس؛ مثلاً «۵ روز کاری».")
    elif act == "rj":
        reject_order(chat_id, int(arg))
    elif act == "rc":
        show_receipt(chat_id, int(arg))
    elif act == "dv":
        rows = data_of(sb.table("orders").select("telegram_user_id,category").eq("id", int(arg)).limit(1))
        if not rows:
            send(chat_id, "سفارش پیدا نشد.")
            return
        ptype = rows[0]["category"] if rows[0]["category"] in PROGRAM_TYPES else "general"
        prompt("o_prog", {"target": rows[0]["telegram_user_id"], "type": ptype, "order_id": int(arg)},
               f"📨 برنامه سفارش #{arg}", "متن، عکس یا PDF برنامه رو بفرست. بلافاصله برای شاگرد ارسال و ذخیره می‌شه.")
    elif act == "os":
        kb = [[btn(status_label(s), f"a:oss:{arg}:{s}")] for s in STATUS_ORDER] + [back_btn(f"a:ov:{arg}", "🔙 سفارش")]
        screen(chat_id, card(f"🔁 وضعیت سفارش #{arg}", "وضعیت جدید رو انتخاب کن:"), kb)
    elif act == "oss":
        status = p[3]
        if status in STATUS_ORDER:
            rows = data_of(sb.table("orders").update({"status": status, "updated_at": now_iso()}).eq("id", int(arg)))
            if rows:
                safe_send(rows[0]["telegram_user_id"], card(f"📍 وضعیت سفارش #{arg}", status_label(status)),
                          [[btn("📦 سفارش‌های من", "my:orders")]])
        view_order(chat_id, int(arg))
    elif act == "oe":
        label = "مبلغ جدید (تومان)" if p[3] == "price" else "زمان تحویل جدید (مثلاً «۴ روز»)"
        prompt("o_oedit", {"order_id": int(arg), "field": p[3]}, f"✏️ سفارش #{arg}", f"{label} رو بفرست.")
    # customers
    elif act == "cl":
        owner_customers(chat_id, int(arg or 0))
    elif act == "cv":
        customer_detail(chat_id, int(arg))
    elif act == "cs":
        prompt("o_search", {}, "🔎 جستجوی شاگرد", "آیدی عددی، یوزرنیم یا بخشی از نام رو بفرست.")
    elif act == "ca":
        prompt("o_addcust", {}, "➕ افزودن شاگرد", "آیدی عددی تلگرام شاگرد (و اختیاری نامش) رو بفرست:\n<code>123456789 علی رضایی</code>")
    elif act == "ce":
        owner_edit_menu(chat_id, int(arg))
    elif act == "cf":
        key = p[3]
        if key in FIELDS or key in OWNER_EXTRA_FIELDS:
            ask_edit_field(chat_id, uid, int(arg), key, True)
    elif act == "co":
        owner_customer_orders(chat_id, int(arg))
    elif act == "cp":
        owner_programs(chat_id, int(arg))
    elif act == "cpt":
        if p[3] in PROGRAM_TYPES:
            owner_program_type(chat_id, int(arg), p[3])
    elif act == "cpe":
        if p[3] in PROGRAM_TYPES:
            prompt("o_prog", {"target": int(arg), "type": p[3]}, f"✏️ {PROGRAM_TYPES[p[3]]}",
                   f"برنامه شاگرد <code>{arg}</code> رو بفرست (متن، عکس یا PDF). جایگزین برنامه قبلی می‌شه و برای شاگرد ارسال می‌شه.")
    elif act == "cm":
        prompt("o_msg", {"target": int(arg)}, "💬 پیام به شاگرد", f"پیامت رو بفرست (هر نوعی: متن، عکس، فایل...) تا برای <code>{arg}</code> ارسال بشه.")
    # services
    elif act == "sl":
        owner_services(chat_id)
    elif act == "sv":
        owner_service(chat_id, int(arg))
    elif act == "sa":
        prompt("o_svc_add", {"step": "name"}, "➕ خدمت جدید", "✏️ نام خدمت رو بفرست.")
    elif act == "sac":
        st = get_state(uid)
        if st.get("state") == "o_svc_add" and arg in CAT_LABELS:
            d = st["data"]
            d.update(step="desc", cat=arg)
            set_state(uid, "o_svc_add", d)
            screen(chat_id, card("➕ خدمت جدید", "📝 توضیحات رو بنویس (یا «-» بفرست تا بدون توضیحات ثبت بشه)."), [[btn("✖️ لغو", "cancel")]])
    elif act == "se":
        names = {"name": "نام جدید", "price": "قیمت جدید (تومان)", "description": "توضیحات جدید (یا «-» برای خالی)"}
        if p[3] in names:
            prompt("o_svc_edit", {"id": int(arg), "field": p[3]}, f"✏️ خدمت #{arg}", f"{names[p[3]]} رو بفرست.")
    elif act == "sc":
        if p[3] in CAT_LABELS:
            data_of(sb.table("services").update({"category": p[3], "updated_at": now_iso()}).eq("id", int(arg)))
        owner_service(chat_id, int(arg))
    elif act == "st":
        rows = data_of(sb.table("services").select("active").eq("id", int(arg)).limit(1))
        if rows:
            data_of(sb.table("services").update({"active": not rows[0]["active"], "updated_at": now_iso()}).eq("id", int(arg)))
        owner_service(chat_id, int(arg))
    # payment
    elif act == "pay":
        clear_state(uid)
        owner_payment(chat_id)
    elif act == "pe":
        if arg == "clear":
            for k in ("card_number", "card_holder", "card_bank"):
                clear_setting(k)
            owner_payment(chat_id)
        else:
            q = {"card": "شماره کارت ۱۶ رقمی رو بفرست.", "holder": "نام صاحب کارت رو بفرست (یا «-» برای حذف).",
                 "bank": "نام بانک رو بفرست (یا «-» برای حذف)."}
            if arg in q:
                prompt("o_card", {"field": arg}, "💳 تنظیمات پرداخت", q[arg])
    # channels
    elif act == "ch":
        clear_state(uid)
        owner_channels(chat_id)
    elif act == "cha":
        prompt("o_chan", {}, "➕ افزودن کانال اجباری",
               "آیدی کانال رو بفرست؛ مثل <code>@mychannel</code>\n⚠️ اول ربات رو <b>ادمین</b> کانال کن.")
    # broadcast
    elif act == "bc":
        prompt("o_bc", {}, "📣 پیام همگانی", "پیامت رو بفرست (متن، عکس، ویدیو... هرچی). قبل از ارسال، تأیید می‌گیرم.")
    elif act == "bcok":
        st = get_state(uid)
        if st.get("state") != "o_bc_confirm":
            screen(chat_id, "این درخواست منقضی شده.", [back_btn()])
            return
        d = st["data"]
        clear_state(uid)
        ids = [int(c["telegram_user_id"]) for c in fetch_all("customers") if c.get("role") != "owner"]
        EXECUTOR.submit(run_broadcast, uid, d["from_chat"], d["mid"], ids)
        screen(chat_id, card("📣 در حال ارسال...", f"برای {len(ids)} نفر؛ پایانش بهت خبر می‌دم."), [back_btn()])
    # portfolio / reviews
    elif act == "pf":
        owner_portfolio(chat_id)
    elif act == "pfa":
        prompt("o_pf_add", {"step": "title"}, "➕ سابقه جدید", "✏️ عنوان رو بفرست.")
    elif act == "rv":
        owner_reviews(chat_id)
    elif act == "rvv":
        owner_review(chat_id, int(arg))
    elif act == "rva":
        rows = data_of(sb.table("testimonials").update({"approved": True}).eq("id", int(arg)))
        if rows:
            safe_send(rows[0]["telegram_user_id"], "⭐ نظرت توسط مربی تأیید شد و در بخش سوابق نمایش داده می‌شه. ممنون ❤️")
            send(chat_id, "✅ نظر منتشر شد.", [[btn("⭐ نظرات", "a:rv")]])
        else:
            send(chat_id, "نظر پیدا نشد (شاید حذف شده).")
    # misc
    elif act == "stats":
        owner_stats(chat_id)
    elif act == "bk":
        owner_backup(chat_id)
    elif act == "dl":
        ask_delete(chat_id, arg, p[3])
    elif act == "dy":
        do_delete(chat_id, arg, p[3])

# ══ Flask endpoints ═════════════════════════════════════════════════════════
@app.get("/")
def index():
    return jsonify({"ok": True, "service": "fitbot", "mode": "webhook"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/ready")
def ready():
    try:
        data_of(sb.table("customers").select("telegram_user_id").limit(1))
        data_of(sb.table("orders").select("id").limit(1))
        data_of(sb.table("required_channels").select("id").limit(1))
        return jsonify({"status": "ready", "database": "ok"}), 200
    except Exception:
        log.exception("Readiness check failed")
        return jsonify({"status": "not_ready", "database": "error"}), 503


def process_update(update: dict):
    uid = 0
    CTX.mid = None
    try:
        if update.get("message"):
            uid = int((update["message"].get("from") or {}).get("id") or 0)
        elif update.get("callback_query"):
            uid = int((update["callback_query"].get("from") or {}).get("id") or 0)
        if uid:
            with USER_LOCKS[uid]:
                if update.get("message"):
                    handle_message(update["message"])
                elif update.get("callback_query"):
                    handle_callback(update["callback_query"])
    except Exception:
        log.exception("Update %s failed for user %s", update.get("update_id"), uid)
        if uid:
            try:
                send(uid, "⚠️ یه خطای موقت پیش اومد. دوباره تلاش کن؛ اگه تکرار شد به پشتیبانی خبر بده.",
                     [[btn("🏠 منو", "home"), btn("💬 پشتیبانی", "support")]])
            except Exception:
                log.debug("could not deliver generic error", exc_info=True)


@app.post("/webhook/<path_token>")
def webhook(path_token: str):
    if not hmac.compare_digest(path_token, HOOK_PATH):
        abort(403)
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        abort(403)
    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    update_id = update.get("update_id")
    if update_id is not None:
        with SEEN_LOCK:
            if int(update_id) in SEEN_UPDATES:
                return jsonify({"ok": True, "duplicate": True}), 200
            SEEN_UPDATES[int(update_id)] = time.time()
            while len(SEEN_UPDATES) > 4000:
                SEEN_UPDATES.popitem(last=False)
    try:
        EXECUTOR.submit(process_update, update)
    except RuntimeError:
        log.exception("Update executor is unavailable")
        return jsonify({"ok": False, "error": "temporarily_unavailable"}), 503
    return jsonify({"ok": True}), 200


def _check_setup_key():
    key = os.getenv("SETUP_KEY", "").strip()
    return bool(key) and hmac.compare_digest(request.args.get("key", ""), key)


def register_commands():
    try:
        tg("setMyCommands", {"commands": [{"command": "start", "description": "شروع و منوی اصلی"},
                                          {"command": "cancel", "description": "لغو کار فعلی"}]}, timeout=10)
        for oid in OWNER_IDS:
            tg("setMyCommands", {"commands": [{"command": "start", "description": "منوی اصلی"},
                                              {"command": "admin", "description": "پنل مدیریت"},
                                              {"command": "cancel", "description": "لغو کار فعلی"}],
                                 "scope": {"type": "chat", "chat_id": oid}}, timeout=10)
    except Exception:
        log.warning("Could not register bot commands", exc_info=True)


def webhook_payload():
    return {"url": f"{PUBLIC_BASE_URL}/webhook/{HOOK_PATH}", "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"], "max_connections": 40, "drop_pending_updates": False}


@app.get("/set-webhook")
def set_webhook():
    if not _check_setup_key():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not PUBLIC_BASE_URL:
        return jsonify({"ok": False, "error": "PUBLIC_BASE_URL is not configured"}), 400
    result = tg("setWebhook", webhook_payload())
    register_commands()
    return jsonify({"ok": True, "telegram_result": result})


@app.get("/webhook-status")
def webhook_status():
    if not _check_setup_key():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "result": tg("getWebhookInfo", {})})


def init_webhook():
    ensure_locked_channels()
    if not AUTO_SET_WEBHOOK:
        log.info("Automatic webhook registration disabled")
        return
    if not PUBLIC_BASE_URL:
        log.error("Cannot auto-register webhook: PUBLIC_BASE_URL is empty")
        return
    try:
        tg("setWebhook", webhook_payload(), timeout=15)
        log.info("Telegram webhook registered for %s", PUBLIC_BASE_URL)
    except Exception:
        log.exception("Automatic webhook registration failed")
    register_commands()


def _keepalive_loop():
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        if PUBLIC_BASE_URL:
            try:
                requests.get(PUBLIC_BASE_URL + "/health", timeout=(3, 8))
            except Exception:
                log.debug("Keepalive ping failed", exc_info=True)


init_webhook()
if KEEPALIVE and PUBLIC_BASE_URL:
    threading.Thread(target=_keepalive_loop, name="fitbot-keepalive", daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
