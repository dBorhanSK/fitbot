# -*- coding: utf-8 -*-
"""FitBot | Persian fitness-coach client, orders and program management.
Flask webhook + Telegram Bot API + Supabase. Owner access is restricted by OWNER_ID.
"""
from __future__ import annotations

import hashlib
import hmac
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

# ── Configuration ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fitbot")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()
OWNER_ID = int(os.environ["OWNER_ID"])
SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
SETUP_KEY = os.getenv("SETUP_KEY", "").strip()
COACH_USERNAME = os.getenv("COACH_USERNAME", "").strip().lstrip("@")
KEEPALIVE = os.getenv("KEEPALIVE", "1").lower() not in ("0", "false", "no", "off")
KEEPALIVE_SECONDS = max(300, int(os.getenv("KEEPALIVE_SECONDS", "480")))
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "1").lower() not in ("0", "false", "no", "off")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# The opaque path segment complements Telegram's secret header, as in NimaBot.
HOOK_PATH = hashlib.sha256((BOT_TOKEN + ":" + WEBHOOK_SECRET).encode()).hexdigest()[:40]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = Flask(__name__)

# Use a pooled HTTP session and short, bounded network timeouts for Telegram.
HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=Retry(
    total=2, backoff_factor=0.15, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]), respect_retry_after_header=True,
)))

# Webhook requests are acknowledged immediately; updates are processed outside Flask.
# Updates from the same user are serialized to avoid racing the conversation state.
WORKERS = max(2, min(12, int(os.getenv("UPDATE_WORKERS", "6"))))
EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fitbot-update")
SEEN_UPDATES: OrderedDict[int, float] = OrderedDict()
SEEN_LOCK = threading.Lock()
USER_LOCKS: dict[int, threading.Lock] = defaultdict(threading.Lock)

# ── Labels and questions ────────────────────────────────────────────────────
CATEGORIES = {
    "nutrition": "🥗 برنامه تخصصی تغذیه",
    "gym": "🏋️ برنامه تمرینی باشگاه",
    "home": "🏠 برنامه تمرینی منزل",
}
CATEGORY_NAMES = {"nutrition": "برنامه تغذیه", "gym": "تمرین باشگاه", "home": "تمرین منزل", "general": "برنامه عمومی"}
MAIN_KEYBOARD = [
    [{"text": CATEGORIES["nutrition"], "callback_data": "cat:nutrition"},
     {"text": CATEGORIES["gym"], "callback_data": "cat:gym"}],
    [{"text": CATEGORIES["home"], "callback_data": "cat:home"},
     {"text": "💬 مشاوره با مربی", "callback_data": "consult"}],
    [{"text": "🏆 سوابق و رضایت شاگردان", "callback_data": "portfolio"},
     {"text": "📩 پیشنهادات و انتقادات", "callback_data": "feedback"}],
    [{"text": "📦 سفارش‌های من", "callback_data": "myorders"},
     {"text": "📚 برنامه‌های من", "callback_data": "myprograms"}],
    [{"text": "👤 پروفایل من", "callback_data": "myprofile"}],
]

FORM_QUESTIONS = [
    ("age", "سن", "چند سالته؟ (فقط عدد)"),
    ("height_cm", "قد", "قدت چند سانتی‌متره؟ (فقط عدد)"),
    ("weight_kg", "وزن", "وزنت چند کیلوگرمه؟ (عدد)"),
    ("blood_type", "گروه خونی", "گروه خونیت چیه؟ اگر نمی‌دونی بنویس «نمی‌دانم»."),
    ("activity_level", "فعالیت روزانه", "میزان فعالیت روزانه‌ات چطوره؟ (کم / متوسط / زیاد)"),
    ("gender", "جنسیت", "جنسیتت چیه؟ (زن / مرد / ترجیح می‌دم نگویم)"),
    ("goal", "هدف", "هدفت چیه؟ (مثلاً کاهش وزن، عضله‌سازی یا تناسب اندام)"),
    ("notes", "محدودیت‌ها و توضیحات", "حساسیت غذایی، آسیب‌دیدگی یا نکته مهمی داری؟ اگر نه بنویس «ندارم»."),
]

STATUS_LABELS = {
    "collecting_info": "در حال تکمیل مشخصات",
    "approved": "پرداخت تأیید شده",
    "awaiting_receipt": "در انتظار رسید پرداخت",
    "pending_review": "رسید در انتظار بررسی مربی",
    "approved": "تأییدشده",
    "in_progress": "در حال آماده‌سازی برنامه",
    "delivered": "برنامه تحویل داده شده",
    "cancelled": "لغوشده",
}

# ── Telegram helpers ────────────────────────────────────────────────────────
def tg(method: str, payload: dict | None = None, timeout: int = 9):
    response = HTTP.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=(3, timeout))
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description', data)}")
    return data.get("result")


def send(chat_id: int, text: str, keyboard: list | None = None):
    payload = {"chat_id": int(chat_id), "text": str(text)[:4096]}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)


def answer_callback(callback_id: str, text: str = ""):
    if not callback_id:
        return
    try:
        tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]}, timeout=5)
    except Exception:
        log.debug("answerCallbackQuery failed", exc_info=True)


def send_file_id(chat_id: int, file_id: str, kind: str, caption: str = ""):
    if not file_id:
        return
    method = "sendPhoto" if kind == "photo" else "sendDocument"
    key = "photo" if kind == "photo" else "document"
    payload = {"chat_id": int(chat_id), key: file_id}
    if caption:
        payload["caption"] = caption[:900]
    return tg(method, payload)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def norm_digits(value: str) -> str:
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(value).translate(trans).replace("٬", "").replace(",", "").strip()


def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def category_label(category: str) -> str:
    return {"nutrition": "برنامه تغذیه", "gym": "تمرین باشگاه", "home": "تمرین منزل"}.get(category, category)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status or "", status or "نامشخص")

# ── Database operations ────────────────────────────────────────────────────
def data_of(query):
    result = query.execute()
    return result.data or []


def get_customer(uid: int):
    rows = data_of(sb.table("customers").select("*").eq("telegram_user_id", int(uid)).limit(1))
    return rows[0] if rows else None


def ensure_customer(tg_user: dict):
    uid = int(tg_user["id"])
    row = {
        "telegram_user_id": uid,
        "username": tg_user.get("username") or "",
        "first_name": tg_user.get("first_name") or "",
        "last_name": tg_user.get("last_name") or "",
        "updated_at": now_iso(),
    }
    if is_owner(uid):
        row["role"] = "owner"
    rows = data_of(sb.table("customers").upsert(row, on_conflict="telegram_user_id").select("*"))
    return rows[0] if rows else row


def save_profile(uid: int, info: dict):
    fields = {
        "age": info.get("age"),
        "height_cm": info.get("height_cm"),
        "weight_kg": info.get("weight_kg"),
        "blood_type": info.get("blood_type"),
        "activity_level": info.get("activity_level"),
        "gender": info.get("gender"),
        "goal": info.get("goal"),
        "notes": info.get("notes"),
        "updated_at": now_iso(),
    }
    # A submitted form is validated before this point. Preserve explicitly supplied text.
    data_of(sb.table("customers").update(fields).eq("telegram_user_id", int(uid)))


def get_state(uid: int):
    rows = data_of(sb.table("user_states").select("state,data").eq("telegram_user_id", int(uid)).limit(1))
    return rows[0] if rows else {"state": "idle", "data": {}}


def set_state(uid: int, state: str, data: dict | None = None):
    row = {"telegram_user_id": int(uid), "state": state, "data": data or {}, "updated_at": now_iso()}
    data_of(sb.table("user_states").upsert(row, on_conflict="telegram_user_id"))


def clear_state(uid: int):
    set_state(uid, "idle", {})


def services_for(category: str | None = None, active_only: bool = True):
    q = sb.table("services").select("*")
    if category:
        q = q.eq("category", category)
    if active_only:
        q = q.eq("active", True)
    return data_of(q.order("created_at", desc=False).limit(60))


def ensure_default_services():
    # Only seed defaults when the services table is empty; never overwrite owner changes.
    if data_of(sb.table("services").select("id").limit(1)):
        return
    defaults = [
        {"category": "nutrition", "name": "برنامه تخصصی تغذیه", "price": 1_000_000, "description": "برنامه غذایی شخصی‌سازی‌شده بر اساس مشخصات و هدف شما.", "active": True},
        {"category": "home", "name": "برنامه تمرینی منزل", "price": 800_000, "description": "برنامه تمرین در منزل متناسب با شرایط شما.", "active": True},
        {"category": "gym", "name": "برنامه تمرینی باشگاه", "price": 800_000, "description": "برنامه تمرین باشگاه بر اساس سطح و هدفت.", "active": True},
    ]
    data_of(sb.table("services").insert(defaults))


def profile_summary(customer: dict | None):
    if not customer:
        return "پروفایل ثبت نشده است."
    pairs = [
        ("سن", customer.get("age")), ("قد", f"{customer.get('height_cm')} سانتی‌متر" if customer.get("height_cm") else None),
        ("وزن", f"{customer.get('weight_kg')} کیلوگرم" if customer.get("weight_kg") else None),
        ("گروه خونی", customer.get("blood_type")), ("فعالیت روزانه", customer.get("activity_level")),
        ("جنسیت", customer.get("gender")), ("هدف", customer.get("goal")), ("محدودیت‌ها", customer.get("notes")),
    ]
    found = [f"• {label}: {value}" for label, value in pairs if value not in (None, "", "ندارم")]
    return "\n".join(found) if found else "مشخصات بدنی هنوز تکمیل نشده است."


def create_order_summary(order: dict, customer: dict | None):
    name = " ".join(filter(None, [(customer or {}).get("first_name"), (customer or {}).get("last_name")])) or "ثبت نشده"
    username = f"@{customer['username']}" if (customer or {}).get("username") else "ندارد"
    return (
        f"🆕 سفارش جدید #{order['id']}\n"
        f"خدمت: {order.get('service_name', '—')}\nقیمت: {int(order.get('price') or 0):,} تومان\n"
        f"مشتری: {name}\nنام کاربری: {username}\nآیدی عددی: {order['telegram_user_id']}\n"
        f"وضعیت: {status_label(order.get('status'))}\n\nمشخصات شاگرد:\n{profile_summary(customer)}"
    )

# ── Customer-facing screens ────────────────────────────────────────────────
def main_menu(chat_id: int):
    send(chat_id, "سلام 👋 به Darya FitBot خوش اومدی!\n\nاز اینجا می‌تونی برنامه بخری، مشخصاتت رو ثبت کنی و برنامه‌های اختصاصی‌ات رو دریافت کنی.", MAIN_KEYBOARD)


def show_services(chat_id: int, category: str):
    if category not in CATEGORIES:
        send(chat_id, "این دسته‌بندی معتبر نیست.", [[{"text": "منوی اصلی", "callback_data": "home"}]])
        return
    ensure_default_services()
    services = services_for(category)
    if not services:
        send(chat_id, f"در بخش «{category_label(category)}» فعلاً خدمتی فعال نیست.", [[{"text": "🔙 منوی اصلی", "callback_data": "home"}]])
        return
    keyboard = [[{"text": f"{s['name']} — {int(s['price']):,} تومان", "callback_data": f"service:{s['id']}"}] for s in services]
    keyboard.append([{"text": "🔙 بازگشت", "callback_data": "home"}])
    send(chat_id, f"یکی از خدمات «{category_label(category)}» رو انتخاب کن:", keyboard)


def service_detail(chat_id: int, service_id: int):
    rows = data_of(sb.table("services").select("*").eq("id", service_id).eq("active", True).limit(1))
    if not rows:
        send(chat_id, "این خدمت دیگر در دسترس نیست.", [[{"text": "بازگشت به منو", "callback_data": "home"}]])
        return
    s = rows[0]
    text = f"✨ {s['name']}\n\n{s.get('description') or 'توضیحی ثبت نشده است.'}\n\n💳 هزینه: {int(s['price']):,} تومان\n\nبرای شروع سفارش روی دکمه زیر بزن."
    send(chat_id, text, [[{"text": "📝 ثبت سفارش", "callback_data": f"order:{s['id']}"}],
                         [{"text": "🔙 بازگشت به خدمات", "callback_data": f"cat:{s['category']}"}]])


def start_order(tg_user: dict, service_id: int):
    uid = int(tg_user["id"])
    rows = data_of(sb.table("services").select("*").eq("id", service_id).eq("active", True).limit(1))
    if not rows:
        send(uid, "این خدمت پیدا نشد یا غیرفعال شده است.", [[{"text": "منوی اصلی", "callback_data": "home"}]])
        return
    service = rows[0]
    created_orders = data_of(sb.table("orders").insert({
        "telegram_user_id": uid, "service_id": service["id"], "service_name": service["name"],
        "category": service["category"], "price": int(service["price"]), "status": "collecting_info",
        "customer_info": {}, "payment_status": "unpaid",
    }).select("*"))
    if not created_orders:
        send(uid, "ثبت سفارش انجام نشد. لطفاً دوباره تلاش کن یا به مربی پیام بده.")
        return
    order = created_orders[0]
    set_state(uid, "order_form", {"order_id": order["id"], "step": 0, "info": {}})
    send(uid, f"سفارش #{order['id']} ثبت شد.\nبرای اینکه برنامه دقیق‌تری آماده بشه، چند سؤال کوتاه می‌پرسم.\n\nدر هر مرحله می‌تونی «لغو» رو بفرستی.")
    ask_form_question(uid, 0)


def ask_form_question(chat_id: int, step: int):
    if step < len(FORM_QUESTIONS):
        _, label, question = FORM_QUESTIONS[step]
        send(chat_id, f"📝 مشخصات {step + 1} از {len(FORM_QUESTIONS)}\n{label}\n{question}")
    else:
        send(chat_id, "✅ مشخصاتت ثبت شد.\nحالا رسید پرداخت رو به صورت عکس یا فایل بفرست. پس از تأیید مربی، زمان آماده‌شدن برنامه هم بهت اعلام می‌شه.",
             [[{"text": "📦 سفارش‌های من", "callback_data": "myorders"}]])


def validate_answer(field: str, raw: str):
    value = raw.strip()[:1000]
    if not value:
        return None, "لطفاً یک پاسخ وارد کن."
    if field == "age":
        try: n = int(norm_digits(value))
        except ValueError: return None, "سن باید عدد باشد. مثال: ۲۵"
        if not 1 <= n <= 120: return None, "سن واردشده معتبر نیست. دوباره تلاش کن."
        return n, None
    if field == "height_cm":
        try: n = int(norm_digits(value))
        except ValueError: return None, "قد را با عدد و به سانتی‌متر بفرست. مثال: ۱۷۵"
        if not 50 <= n <= 260: return None, "قد باید بین ۵۰ تا ۲۶۰ سانتی‌متر باشد."
        return n, None
    if field == "weight_kg":
        try: n = float(norm_digits(value))
        except ValueError: return None, "وزن را به صورت عدد بفرست. مثال: ۷۲ یا ۷۲.۵"
        if not 15 <= n <= 400: return None, "وزن واردشده خارج از محدوده قابل قبول است."
        return n, None
    return value, None


def show_profile(chat_id: int, uid: int):
    customer = get_customer(uid)
    username = f"@{customer['username']}" if customer and customer.get("username") else "ثبت نشده"
    send(chat_id, f"👤 پروفایل شاگرد\nآیدی عددی: {uid}\nنام کاربری: {username}\n\n{profile_summary(customer)}",
         [[{"text": "🔄 تکمیل/ویرایش مشخصات", "callback_data": "profile:edit"}],
          [{"text": "📦 سفارش‌های من", "callback_data": "myorders"}],
          [{"text": "🔙 منوی اصلی", "callback_data": "home"}]])


def show_customer_programs(uid: int):
    programs = data_of(sb.table("programs").select("program_type,title,body,file_id,file_kind,updated_at")
                       .eq("customer_user_id", int(uid)).eq("active", True).order("updated_at", desc=True).limit(20))
    if not programs:
        send(uid, "📚 هنوز برنامه‌ای برای حسابت ثبت نشده. بعد از تأیید سفارش و آماده‌شدن برنامه، از همین بخش می‌تونی ببینیش.",
             [[{"text": "🛍 مشاهده خدمات", "callback_data": "cat:nutrition"}], [{"text": "🔙 منوی اصلی", "callback_data": "home"}]])
        return
    send(uid, "📚 برنامه‌های اختصاصی تو\nبرنامه‌ها بر اساس آخرین به‌روزرسانی مرتب شده‌اند.")
    for program in programs:
        title = program.get("title") or CATEGORY_NAMES.get(program.get("program_type"), "برنامه اختصاصی")
        body = program.get("body") or "فایل برنامه پیوست شده است."
        send(uid, f"✨ {title}\nآخرین به‌روزرسانی: {str(program.get('updated_at') or '')[:10]}\n\n{body[:3500]}")
        if program.get("file_id") and program.get("file_kind"):
            send_file_id(uid, program["file_id"], program["file_kind"], caption=title)
    send(uid, "برای دیدن نسخه جدیدتر هر برنامه، هر زمان از «برنامه‌های من» وارد شو.", [[{"text": "🔙 منوی اصلی", "callback_data": "home"}]])


def show_my_orders(uid: int):
    orders = data_of(sb.table("orders").select("id,service_name,price,status,payment_status,delivery_eta,created_at")
                     .eq("telegram_user_id", int(uid)).order("created_at", desc=True).limit(10))
    if not orders:
        send(uid, "هنوز سفارشی ثبت نکردی.", [[{"text": "مشاهده خدمات", "callback_data": "cat:nutrition"}], [{"text": "منوی اصلی", "callback_data": "home"}]])
        return
    lines = []
    for order in orders:
        eta = f"\nزمان تخمینی: {order['delivery_eta']}" if order.get("delivery_eta") else ""
        lines.append(f"#{order['id']} · {order['service_name']}\nمبلغ: {int(order.get('price') or 0):,} تومان\nوضعیت: {status_label(order.get('status'))}{eta}")
    send(uid, "📦 سفارش‌های تو\n\n" + "\n\n────────────\n\n".join(lines), [[{"text": "🔙 منوی اصلی", "callback_data": "home"}]])

# ── Owner panel ────────────────────────────────────────────────────────────
def owner_menu(chat_id: int):
    keyboard = [
        [{"text": "🧾 رسیدهای در انتظار بررسی", "callback_data": "owner:pending"}],
        [{"text": "📋 همه سفارش‌ها", "callback_data": "owner:orders"}],
        [{"text": "👥 فهرست شاگردان", "callback_data": "owner:customers"}, {"text": "🔎 جستجوی آیدی", "callback_data": "owner:customer_search"}],
        [{"text": "🛍 مدیریت خدمات و قیمت‌ها", "callback_data": "owner:services"}],
        [{"text": "➕ افزودن سابقه/نمونه‌کار", "callback_data": "owner:portfolio_add"}],
        [{"text": "🔙 منوی اصلی", "callback_data": "home"}],
    ]
    send(chat_id, "✨ پنل مربی FitBot\n\nاز اینجا شاگردها، رسیدها، سفارش‌ها، زمان آماده‌سازی و برنامه‌ها رو مدیریت کن.", keyboard)


def order_card(chat_id: int, order: dict):
    customer = get_customer(int(order["telegram_user_id"]))
    summary = create_order_summary(order, customer)
    eta = f"\nزمان تحویل: {order['delivery_eta']}" if order.get("delivery_eta") else ""
    summary += f"\n\nوضعیت پرداخت: {order.get('payment_status', 'unpaid')}{eta}"
    keyboard = []
    if order.get("status") == "pending_review":
        keyboard += [[{"text": "✅ تأیید + ۳ روز", "callback_data": f"approvequick:{order['id']}:۳ روز"},
                      {"text": "✅ تأیید + ۴ روز", "callback_data": f"approvequick:{order['id']}:۴ روز"}],
                     [{"text": "✅ تأیید + یک هفته", "callback_data": f"approvequick:{order['id']}:یک هفته"},
                      {"text": "🕒 زمان دلخواه", "callback_data": f"approveeta:{order['id']}"}],
                     [{"text": "❌ رد رسید", "callback_data": f"reject:{order['id']}"}]]
    if order.get("status") in ("in_progress", "approved", "delivered"):
        keyboard.append([{"text": "📝 تحویل/ویرایش برنامه سفارش", "callback_data": f"deliverhint:{order['id']}"}])
    keyboard.append([{"text": f"👤 مشاهده شاگرد {order['telegram_user_id']}", "callback_data": f"cust:view:{order['telegram_user_id']}"}])
    keyboard.append([{"text": "🔙 پنل مربی", "callback_data": "owner:menu"}])
    send(chat_id, summary, keyboard)


def owner_orders(chat_id: int, pending: bool):
    q = sb.table("orders").select("id,telegram_user_id,service_name,price,status,created_at").order("created_at", desc=True).limit(20)
    if pending:
        q = q.eq("status", "pending_review")
    orders = data_of(q)
    if not orders:
        send(chat_id, "در این بخش سفارشی وجود نداره.", [[{"text": "بازگشت به پنل", "callback_data": "owner:menu"}]])
        return
    title = "🧾 سفارش‌های در انتظار بررسی" if pending else "📋 آخرین سفارش‌ها"
    lines, keyboard = [title, "برای مشاهده‌ی مشخصات و اقدامات هر سفارش روی آن بزن:"], []
    for order in orders:
        lines.append(f"• #{order['id']} · {order['service_name']} · {int(order.get('price') or 0):,} تومان · {status_label(order.get('status'))} · آیدی {order['telegram_user_id']}")
        keyboard.append([{"text": f"#{order['id']} — {order['service_name'][:35]}", "callback_data": f"orderview:{order['id']}"}])
    keyboard.append([{"text": "🔙 پنل مربی", "callback_data": "owner:menu"}])
    send(chat_id, "\n".join(lines), keyboard)


def owner_customers(chat_id: int):
    customers = data_of(sb.table("customers").select("telegram_user_id,username,first_name,last_name,age,goal,updated_at")
                        .order("updated_at", desc=True).limit(20))
    if not customers:
        send(chat_id, "هنوز شاگردی ثبت نشده است.", [[{"text": "بازگشت", "callback_data": "owner:menu"}]])
        return
    keyboard, lines = [], ["👥 فهرست شاگردان", "برای دیدن مشخصات، سفارش‌ها و برنامه‌های هر شاگرد انتخابش کن:"]
    for customer in customers:
        uid = customer["telegram_user_id"]
        name = " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])) or "بدون نام"
        username = f" @{customer['username']}" if customer.get("username") else ""
        profile = f" · {customer.get('age')} سال" if customer.get("age") else " · پروفایل ناقص"
        lines.append(f"• {name}{username} — آیدی {uid}{profile}")
        keyboard.append([{"text": f"{name[:25]} · {uid}", "callback_data": f"cust:view:{uid}"}])
    keyboard.append([{"text": "🔙 پنل مربی", "callback_data": "owner:menu"}])
    send(chat_id, "\n".join(lines), keyboard)


def customer_detail(chat_id: int, target_uid: int):
    customer = get_customer(target_uid)
    if not customer:
        send(chat_id, f"شاگردی با آیدی {target_uid} در دیتابیس پیدا نشد. باید حداقل یک‌بار /start را در ربات زده باشد.", [[{"text": "بازگشت", "callback_data": "owner:customers"}]])
        return
    name = " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])) or "بدون نام"
    username = f"@{customer['username']}" if customer.get("username") else "ندارد"
    orders = data_of(sb.table("orders").select("id,service_name,status,price,delivery_eta,created_at")
                     .eq("telegram_user_id", target_uid).order("created_at", desc=True).limit(5))
    order_text = "\n".join(f"• #{o['id']} {o['service_name']} — {status_label(o.get('status'))}" for o in orders) or "هنوز سفارشی ندارد."
    text = f"👤 حساب شاگرد\nنام: {name}\nنام کاربری: {username}\nآیدی عددی: {target_uid}\n\n📋 مشخصات\n{profile_summary(customer)}\n\n📦 سفارش‌های اخیر\n{order_text}"
    keyboard = [
        [{"text": "📚 مشاهده/ویرایش برنامه‌ها", "callback_data": f"cust:programs:{target_uid}"}],
        [{"text": "✏️ ویرایش مشخصات", "callback_data": f"cust:editprofile:{target_uid}"}],
        [{"text": "📦 همه سفارش‌ها", "callback_data": f"cust:orders:{target_uid}"}],
        [{"text": "🔙 فهرست شاگردان", "callback_data": "owner:customers"}],
    ]
    send(chat_id, text, keyboard)


def customer_program_menu(chat_id: int, target_uid: int):
    programs = data_of(sb.table("programs").select("program_type,title,body,file_id,active,updated_at")
                       .eq("customer_user_id", target_uid).eq("active", True))
    current = {p["program_type"]: p for p in programs}
    lines = [f"📚 برنامه‌های شاگرد {target_uid}"]
    for typ in ("nutrition", "gym", "home", "general"):
        p = current.get(typ)
        if p:
            lines.append(f"\n✅ {CATEGORY_NAMES[typ]}: {p.get('title') or 'ثبت‌شده'} — به‌روزرسانی {str(p.get('updated_at') or '')[:10]}")
        else:
            lines.append(f"\n▫️ {CATEGORY_NAMES[typ]}: هنوز ثبت نشده")
    keyboard = [
        [{"text": "🥗 ثبت/ویرایش تغذیه", "callback_data": f"cust:program:{target_uid}:nutrition"}],
        [{"text": "🏋️ ثبت/ویرایش باشگاه", "callback_data": f"cust:program:{target_uid}:gym"}],
        [{"text": "🏠 ثبت/ویرایش منزل", "callback_data": f"cust:program:{target_uid}:home"}],
        [{"text": "📌 برنامه عمومی", "callback_data": f"cust:program:{target_uid}:general"}],
        [{"text": "👤 بازگشت به حساب شاگرد", "callback_data": f"cust:view:{target_uid}"}],
    ]
    send(chat_id, "\n".join(lines), keyboard)


def set_owner_profile_state(owner_id: int, target_uid: int):
    set_state(owner_id, "owner_profile_edit", {"customer_id": int(target_uid)})
    send(owner_id, f"مشخصات شاگرد {target_uid} رو در یک پیام به این قالب بفرست:\nسن|قد سانتی‌متر|وزن کیلوگرم|گروه خونی|فعالیت روزانه|جنسیت|هدف|محدودیت‌ها\n\nمثال: ۲۵|۱۷۵|۷۲|O+|متوسط|مرد|عضله‌سازی|بدون محدودیت\nبرای فیلد ناموجود از «-» استفاده کن.")


def set_owner_program_state(owner_id: int, target_uid: int, program_type: str):
    set_state(owner_id, "owner_program_edit", {"customer_id": int(target_uid), "program_type": program_type})
    send(owner_id, f"برای شاگرد {target_uid} — بخش «{CATEGORY_NAMES.get(program_type, program_type)}» — محتوای برنامه رو در پیام بعدی بفرست.\n\nمی‌تونی متن، عکس یا فایل PDF ارسال کنی. اگر فایل می‌فرستی، کپشن هم اختیاریه. این محتوا جایگزین برنامه قبلی همین بخش می‌شه.")


def save_program_and_deliver(owner_id: int, state_data: dict, body: str, file_id: str | None = None, file_kind: str | None = None):
    target_uid = int(state_data["customer_id"])
    program_type = state_data["program_type"]
    title = CATEGORY_NAMES.get(program_type, "برنامه اختصاصی")
    row = {
        "customer_user_id": target_uid,
        "program_type": program_type,
        "title": title,
        "body": body[:10000],
        "file_id": file_id,
        "file_kind": file_kind,
        "active": True,
        "updated_at": now_iso(),
        "updated_by": int(owner_id),
    }
    data_of(sb.table("programs").upsert(row, on_conflict="customer_user_id,program_type"))
    send(target_uid, f"🎉 مربی برنامه‌ات رو در بخش «{title}» به‌روزرسانی کرد.\n\n{body[:3000] or 'فایل برنامه در پیام جداگانه ارسال شده است.'}")
    if file_id and file_kind:
        send_file_id(target_uid, file_id, file_kind, caption=title)
    # Mark the most recent approved/in-progress order in the same category delivered.
    orders = data_of(sb.table("orders").select("id").eq("telegram_user_id", target_uid)
                     .eq("category", program_type).in_("status", ["approved", "in_progress"]).order("created_at", desc=True).limit(1))
    if orders:
        data_of(sb.table("orders").update({"status": "delivered", "program_text": body[:10000], "delivered_at": now_iso()}).eq("id", orders[0]["id"]))
    send(owner_id, f"✅ برنامه {title} برای شاگرد {target_uid} ذخیره و ارسال شد.", [[{"text": "📚 مدیریت برنامه‌های همین شاگرد", "callback_data": f"cust:programs:{target_uid}"}],
                                                                             [{"text": "👤 حساب شاگرد", "callback_data": f"cust:view:{target_uid}"}]])


def approve_order(owner_id: int, order_id: int, eta: str):
    rows = data_of(sb.table("orders").select("*").eq("id", int(order_id)).limit(1))
    if not rows:
        send(owner_id, "سفارش پیدا نشد.")
        return
    order = rows[0]
    if order.get("status") != "pending_review":
        send(owner_id, "این سفارش دیگر در وضعیت بررسی رسید نیست.")
        return
    data_of(sb.table("orders").update({
        "status": "in_progress", "payment_status": "approved", "delivery_eta": eta,
        "approved_at": now_iso(), "updated_at": now_iso(),
    }).eq("id", int(order_id)).eq("status", "pending_review"))
    send(int(order["telegram_user_id"]), f"✅ پرداخت سفارش #{order_id} تأیید شد.\n\n⏳ زمان آماده‌شدن «{order['service_name']}»: {eta}.\nمربی برنامه رو در همین ربات برات ارسال می‌کنه. ممنون از اعتمادت ❤️",
         [[{"text": "📦 پیگیری سفارش", "callback_data": "myorders"}], [{"text": "👤 پروفایل من", "callback_data": "myprofile"}]])
    send(owner_id, f"✅ پرداخت سفارش #{order_id} تأیید شد. زمان اعلام‌شده: {eta}.", [[{"text": "📨 ارسال/ویرایش برنامه سفارش", "callback_data": f"deliverhint:{order_id}"}],
                                                                               [{"text": f"👤 حساب شاگرد {order['telegram_user_id']}", "callback_data": f"cust:view:{order['telegram_user_id']}"}]])

# ── Message handling ────────────────────────────────────────────────────────
def handle_message(message: dict):
    tg_user = message.get("from") or {}
    if not tg_user.get("id") or not message.get("chat"):
        return
    uid = int(tg_user["id"])
    chat_id = int(message["chat"]["id"])
    ensure_customer(tg_user)
    text = (message.get("text") or message.get("caption") or "").strip()
    photo_list = message.get("photo") or []
    document = message.get("document") or None
    file_id = photo_list[-1].get("file_id") if photo_list else (document or {}).get("file_id")
    file_kind = "photo" if photo_list else ("document" if document else None)

    if text.startswith("/start"):
        clear_state(uid)
        main_menu(chat_id)
        return
    if text.startswith("/cancel") or text == "لغو":
        state_row = get_state(uid)
        data = state_row.get("data") or {}
        if state_row.get("state") == "order_form" and data.get("order_id"):
            data_of(sb.table("orders").update({"status": "cancelled"}).eq("id", data["order_id"]).eq("telegram_user_id", uid))
        clear_state(uid)
        send(chat_id, "فرآیند فعلی لغو شد.", [[{"text": "منوی اصلی", "callback_data": "home"}]])
        return

    # Owner commands are handled before stateful inputs.
    if is_owner(uid) and text.startswith("/admin"):
        clear_state(uid); owner_menu(chat_id); return
    if is_owner(uid) and text.startswith("/customer"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and norm_digits(parts[1]).isdigit():
            customer_detail(chat_id, int(norm_digits(parts[1])))
        else:
            set_state(uid, "owner_customer_search", {})
            send(chat_id, "آیدی عددی تلگرام شاگرد رو بفرست.")
        return
    if is_owner(uid) and text.startswith("/addservice"):
        set_state(uid, "owner_service_add", {})
        send(chat_id, "خدمت جدید رو با قالب زیر بفرست:\ncategory|نام|قیمت به تومان|توضیحات\n\nدسته‌ها: nutrition یا gym یا home")
        return
    if is_owner(uid) and text.startswith("/delservice "):
        try:
            sid = int(norm_digits(text.split(maxsplit=1)[1]))
            data_of(sb.table("services").update({"active": False}).eq("id", sid))
            send(chat_id, f"✅ خدمت #{sid} غیرفعال شد.")
        except Exception:
            send(chat_id, "فرمت دستور: /delservice 3")
        return
    if is_owner(uid) and text.startswith("/delportfolio "):
        try:
            item_id = int(norm_digits(text.split(maxsplit=1)[1]))
            data_of(sb.table("portfolio_items").update({"active": False}).eq("id", item_id))
            send(chat_id, f"✅ نمونه‌کار #{item_id} غیرفعال شد.")
        except Exception:
            send(chat_id, "فرمت دستور: /delportfolio 2")
        return
    if is_owner(uid) and text.startswith("/addportfolio"):
        set_state(uid, "owner_portfolio_add", {})
        send(chat_id, "سابقه/نمونه‌کار رو به این قالب بفرست:\nعنوان|توضیحات")
        return
    if is_owner(uid) and text.startswith("/deliver "):
        try:
            order_id = int(norm_digits(text.split(maxsplit=1)[1]))
            rows = data_of(sb.table("orders").select("*").eq("id", order_id).limit(1))
            if not rows:
                raise ValueError()
            set_state(uid, "owner_deliver_order", {"order_id": order_id, "customer_id": rows[0]["telegram_user_id"]})
            send(chat_id, f"متن یا فایل برنامه سفارش #{order_id} رو در پیام بعدی بفرست.")
        except Exception:
            send(chat_id, "فرمت دستور: /deliver شناسه_سفارش")
        return

    state_row = get_state(uid)
    state = state_row.get("state", "idle")
    data = state_row.get("data") or {}

    if state == "customer_profile_form":
        handle_profile_form(uid, chat_id, text, data)
        return

    if state == "order_form":
        step = int(data.get("step", 0))
        if step >= len(FORM_QUESTIONS):
            clear_state(uid)
        else:
            field, label, _ = FORM_QUESTIONS[step]
            answer, error = validate_answer(field, text)
            if error:
                send(chat_id, error)
                ask_form_question(chat_id, step)
                return
            info = data.get("info") or {}
            info[field] = answer
            step += 1
            data.update({"step": step, "info": info})
            if step < len(FORM_QUESTIONS):
                set_state(uid, "order_form", data)
                ask_form_question(chat_id, step)
            else:
                order_id = int(data["order_id"])
                save_profile(uid, info)
                data_of(sb.table("orders").update({"customer_info": info, "status": "awaiting_receipt", "updated_at": now_iso()})
                        .eq("id", order_id).eq("telegram_user_id", uid))
                clear_state(uid)
                ask_form_question(chat_id, step)
            return

    if state == "owner_set_eta" and is_owner(uid):
        eta = text[:80]
        if len(eta) < 2:
            send(chat_id, "زمان رو واضح‌تر بنویس؛ مثلاً «۴ روز کاری» یا «یک هفته».")
            return
        approve_order(uid, int(data["order_id"]), eta)
        clear_state(uid)
        return

    if state == "owner_customer_search" and is_owner(uid):
        value = norm_digits(text)
        if not value.isdigit():
            send(chat_id, "آیدی باید عددی باشه. دوباره آیدی شاگرد رو بفرست.")
            return
        clear_state(uid)
        customer_detail(chat_id, int(value))
        return

    if state == "owner_profile_edit" and is_owner(uid):
        parts = [p.strip() for p in text.split("|")]
        if len(parts) != 8:
            send(chat_id, "قالب باید دقیقاً ۸ بخش داشته باشه. دوباره به شکل سن|قد|وزن|گروه خونی|فعالیت|جنسیت|هدف|محدودیت‌ها بفرست.")
            return
        target_uid = int(data["customer_id"])
        age, height, weight = [norm_digits(p) for p in parts[:3]]
        try:
            fields = {
                "age": int(age) if age and age != "-" else None,
                "height_cm": int(height) if height and height != "-" else None,
                "weight_kg": float(weight) if weight and weight != "-" else None,
                "blood_type": None if parts[3] == "-" else parts[3],
                "activity_level": None if parts[4] == "-" else parts[4],
                "gender": None if parts[5] == "-" else parts[5],
                "goal": None if parts[6] == "-" else parts[6],
                "notes": None if parts[7] == "-" else parts[7],
                "updated_at": now_iso(),
            }
            data_of(sb.table("customers").update(fields).eq("telegram_user_id", target_uid))
            clear_state(uid)
            send(chat_id, "✅ مشخصات شاگرد به‌روزرسانی شد.", [[{"text": "👤 مشاهده حساب شاگرد", "callback_data": f"cust:view:{target_uid}"}]])
        except Exception:
            log.exception("Owner profile edit failed")
            send(chat_id, "ذخیره مشخصات انجام نشد؛ مقادیر سن، قد و وزن رو بررسی کن.")
        return

    if state == "owner_program_edit" and is_owner(uid):
        if not text and not file_id:
            send(chat_id, "لطفاً متن، عکس یا فایل برنامه رو بفرست.")
            return
        save_program_and_deliver(uid, data, text, file_id, file_kind)
        clear_state(uid)
        return

    if state == "owner_service_edit" and is_owner(uid):
        parts = text.split("|", 2)
        if len(parts) != 3:
            send(chat_id, "قالب ویرایش: نام جدید|قیمت جدید به تومان|توضیحات")
            return
        name, raw_price, description = [p.strip() for p in parts]
        price_text = norm_digits(raw_price)
        if not name or not price_text.isdigit() or int(price_text) < 1:
            send(chat_id, "نام نباید خالی باشه و قیمت باید عدد مثبت باشه.")
            return
        sid = int(data["service_id"])
        data_of(sb.table("services").update({"name": name[:120], "price": int(price_text), "description": description[:1500], "updated_at": now_iso()}).eq("id", sid))
        clear_state(uid)
        send(chat_id, f"✅ خدمت #{sid} و قیمت آن به‌روزرسانی شد.", [[{"text": "🛍 مدیریت خدمات", "callback_data": "owner:services"}], [{"text": "پنل مربی", "callback_data": "owner:menu"}]])
        return

    if state == "owner_service_add" and is_owner(uid):
        parts = text.split("|", 3)
        if len(parts) != 4:
            send(chat_id, "قالب صحیح: nutrition|نام خدمت|قیمت|توضیحات")
            return
        category, name, raw_price, description = [p.strip() for p in parts]
        price_text = norm_digits(raw_price)
        if category not in CATEGORIES or not name or not price_text.isdigit() or int(price_text) < 1:
            send(chat_id, "دسته باید nutrition یا gym یا home باشه و قیمت باید عدد مثبت باشه.")
            return
        data_of(sb.table("services").insert({"category": category, "name": name[:120], "price": int(price_text), "description": description[:1500], "active": True}))
        clear_state(uid)
        send(chat_id, "✅ خدمت جدید اضافه شد.", [[{"text": "🛍 مدیریت خدمات", "callback_data": "owner:services"}], [{"text": "پنل مربی", "callback_data": "owner:menu"}]])
        return

    if state == "owner_deliver_order" and is_owner(uid):
        if not text and not file_id:
            send(chat_id, "لطفاً متن برنامه یا فایل رو بفرست.")
            return
        order_id = int(data["order_id"])
        target_uid = int(data["customer_id"])
        order_rows = data_of(sb.table("orders").select("category,service_name").eq("id", order_id).limit(1))
        order_row = order_rows[0] if order_rows else {"category": "general", "service_name": "برنامه اختصاصی"}
        program_type = order_row.get("category") if order_row.get("category") in CATEGORY_NAMES else "general"
        title = order_row.get("service_name") or CATEGORY_NAMES.get(program_type, "برنامه اختصاصی")
        data_of(sb.table("programs").upsert({
            "customer_user_id": target_uid, "program_type": program_type, "title": title,
            "body": text[:10000], "file_id": file_id, "file_kind": file_kind,
            "active": True, "updated_by": uid, "updated_at": now_iso(),
        }, on_conflict="customer_user_id,program_type"))
        send(target_uid, f"🎉 برنامه سفارش #{order_id} از طرف مربی ارسال شد.\n\n{text[:3000] if text else 'فایل برنامه در پیام جداگانه ارسال می‌شود.'}")
        if file_id and file_kind:
            send_file_id(target_uid, file_id, file_kind, caption=title)
        data_of(sb.table("orders").update({"status": "delivered", "program_text": text[:10000], "program_file_id": file_id,
                                            "delivered_at": now_iso(), "updated_at": now_iso()}).eq("id", order_id))
        clear_state(uid)
        send(chat_id, f"✅ برنامه سفارش #{order_id} ارسال و در بخش «برنامه‌های من» ذخیره شد.")
        return

    if state == "owner_portfolio_add" and is_owner(uid):
        parts = text.split("|", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            send(chat_id, "قالب: عنوان|توضیحات")
            return
        data_of(sb.table("portfolio_items").insert({"title": parts[0].strip()[:160], "body": parts[1].strip()[:2000], "active": True}))
        clear_state(uid)
        send(chat_id, "✅ نمونه‌کار ثبت شد.", [[{"text": "پنل مربی", "callback_data": "owner:menu"}]])
        return

    if state == "review":
        if not text:
            send(chat_id, "لطفاً نظرت رو به صورت متن بفرست.")
            return
        data_of(sb.table("testimonials").insert({"telegram_user_id": uid, "display_name": (tg_user.get("first_name") or "شاگرد")[:80], "review_text": text[:1200], "approved": False}))
        clear_state(uid)
        send(chat_id, "ممنون از نظرت ❤️ بعد از بررسی مربی منتشر می‌شه.")
        send(OWNER_ID, f"⭐ نظر جدید از شاگرد {uid}:\n{text[:1200]}", [[{"text": "✅ انتشار نظر", "callback_data": f"reviewapprove:{uid}"}, {"text": "🗑 رد نظر", "callback_data": f"reviewreject:{uid}"}]])
        return
    if state == "feedback":
        data_of(sb.table("feedback").insert({"telegram_user_id": uid, "message": text or "[پیام غیرمتنی]"}))
        clear_state(uid)
        send(OWNER_ID, f"📩 پیام جدید از شاگرد {uid}:\n{text or '[پیام غیرمتنی]'}")
        send(chat_id, "ممنون از پیامت ❤️", [[{"text": "منوی اصلی", "callback_data": "home"}]])
        return

    # A receipt can only be associated with an order awaiting payment receipt.
    if file_id:
        pending = data_of(sb.table("orders").select("*").eq("telegram_user_id", uid).eq("status", "awaiting_receipt")
                          .order("created_at", desc=True).limit(1))
        if pending:
            order = pending[0]
            data_of(sb.table("orders").update({"status": "pending_review", "payment_status": "receipt_submitted",
                                                "receipt_file_id": file_id, "receipt_file_kind": file_kind,
                                                "updated_at": now_iso()}).eq("id", order["id"]))
            send(chat_id, f"🧾 رسید سفارش #{order['id']} دریافت شد و برای مربی فرستاده شد.")
            refreshed_customer = get_customer(uid)
            keyboard = [[{"text": "مشاهده و بررسی سفارش", "callback_data": f"orderview:{order['id']}"}],
                        [{"text": "✅ تأیید + تعیین زمان", "callback_data": f"approveeta:{order['id']}"}]]
            send(OWNER_ID, create_order_summary({**order, "status": "pending_review"}, refreshed_customer), keyboard)
            try:
                send_file_id(OWNER_ID, file_id, file_kind or "document", caption=f"رسید سفارش #{order['id']} از آیدی {uid}")
            except Exception:
                log.exception("Could not send receipt to owner")
            return
        send(chat_id, "برای این فایل سفارش در انتظار رسید پیدا نشد. ابتدا از منو یک خدمت انتخاب کن.", [[{"text": "منوی اصلی", "callback_data": "home"}]])
        return

    if text and not text.startswith("/"):
        send(chat_id, "برای استفاده از خدمات، یکی از گزینه‌های منو رو انتخاب کن.", MAIN_KEYBOARD)

# ── Callback handling ───────────────────────────────────────────────────────
def handle_callback(query: dict):
    tg_user = query.get("from") or {}
    uid = int(tg_user.get("id", 0))
    chat_id = int((query.get("message") or {}).get("chat", {}).get("id", uid))
    data = query.get("data") or ""
    answer_callback(query.get("id", ""))
    if tg_user.get("id"):
        ensure_customer(tg_user)

    if data == "home":
        clear_state(uid); main_menu(chat_id); return
    if data.startswith("cat:"):
        show_services(chat_id, data.split(":", 1)[1]); return
    if data.startswith("service:"):
        service_detail(chat_id, int(data.split(":", 1)[1])); return
    if data.startswith("order:"):
        start_order(tg_user, int(data.split(":", 1)[1])); return
    if data == "consult":
        if COACH_USERNAME:
            send(chat_id, f"💬 برای مشاوره مستقیم به مربی پیام بده:\nhttps://t.me/{COACH_USERNAME}")
        else:
            send(chat_id, "آیدی عمومی مربی در تنظیمات ثبت نشده است.")
        return
    if data == "myorders":
        show_my_orders(uid); return
    if data == "myprograms":
        show_customer_programs(uid); return
    if data == "myprofile":
        show_profile(chat_id, uid); return
    if data == "profile:edit":
        set_state(uid, "customer_profile_form", {"step": 0, "info": {}})
        send(chat_id, "برای تکمیل پروفایل، همین سؤال‌ها رو به‌ترتیب جواب بده.\nبرای لغو /cancel رو بفرست.")
        ask_form_question(chat_id, 0)
        return
    if data == "portfolio":
        items = data_of(sb.table("portfolio_items").select("title,body").eq("active", True).order("created_at", desc=True).limit(10))
        reviews = data_of(sb.table("testimonials").select("display_name,review_text").eq("approved", True).order("created_at", desc=True).limit(10))
        parts = ["🏆 سوابق مربی"]
        parts += [f"\n• {item['title']}\n{item['body']}" for item in items] if items else ["\nهنوز نمونه‌کاری ثبت نشده است."]
        parts.append("\n\n⭐ نظرات شاگردان")
        parts += [f"\n• {r.get('display_name') or 'شاگرد'}: {r['review_text']}" for r in reviews] if reviews else ["\nهنوز نظری منتشر نشده است."]
        send(chat_id, "".join(parts), [[{"text": "✍️ ثبت نظر", "callback_data": "review"}], [{"text": "🔙 منوی اصلی", "callback_data": "home"}]])
        return
    if data == "review":
        set_state(uid, "review", {})
        send(chat_id, "نظر و تجربه‌ات رو در پیام بعدی بنویس.")
        return
    if data == "feedback":
        set_state(uid, "feedback", {})
        send(chat_id, "پیام یا پیشنهادت رو در پیام بعدی بنویس.")
        return

    if data.startswith("owner:") and is_owner(uid):
        action = data.split(":", 1)[1]
        if action in ("menu",): owner_menu(chat_id); return
        if action == "pending": owner_orders(chat_id, True); return
        if action == "orders": owner_orders(chat_id, False); return
        if action == "customers": owner_customers(chat_id); return
        if action == "customer_search":
            set_state(uid, "owner_customer_search", {})
            send(chat_id, "آیدی عددی تلگرام شاگرد رو بفرست.")
            return
        if action == "services":
            ensure_default_services()
            rows = services_for(active_only=False)
            if not rows:
                send(chat_id, "خدمتی ثبت نشده است.", [[{"text": "➕ افزودن خدمت", "callback_data": "owner:service_add"}], [{"text": "پنل مربی", "callback_data": "owner:menu"}]])
                return
            for service in rows:
                state_text = "فعال ✅" if service.get("active") else "غیرفعال ⏸"
                send(chat_id, f"#{service['id']} · {service['name']}\nدسته: {service['category']} · قیمت: {int(service['price']):,} تومان\n{state_text}\n{service.get('description') or ''}",
                     [[{"text": "✏️ ویرایش خدمت/قیمت", "callback_data": f"ownsvc:edit:{service['id']}"}],
                      [{"text": "غیرفعال‌سازی" if service.get("active") else "فعال‌سازی", "callback_data": f"ownsvc:toggle:{service['id']}"}]])
            send(chat_id, "برای افزودن خدمت جدید از دکمه زیر استفاده کن.", [[{"text": "➕ افزودن خدمت", "callback_data": "owner:service_add"}], [{"text": "🔙 پنل مربی", "callback_data": "owner:menu"}]])
            return
        if action == "service_add":
            set_state(uid, "owner_service_add", {})
            send(chat_id, "خدمت رو به قالب category|نام|قیمت|توضیحات بفرست. دسته‌ها: nutrition، gym، home")
            return
        if action == "portfolio_add":
            set_state(uid, "owner_portfolio_add", {})
            send(chat_id, "قالب نمونه‌کار: عنوان|توضیحات")
            return

    if data.startswith("ownsvc:edit:") and is_owner(uid):
        sid = int(data.rsplit(":", 1)[1])
        rows = data_of(sb.table("services").select("id,name,price,description").eq("id", sid).limit(1))
        if not rows:
            send(chat_id, "خدمت پیدا نشد.")
            return
        service = rows[0]
        set_state(uid, "owner_service_edit", {"service_id": sid})
        send(chat_id, f"ویرایش خدمت #{sid}\nنام فعلی: {service['name']}\nقیمت فعلی: {int(service['price']):,} تومان\n\nقالب رو بفرست:\nنام جدید|قیمت جدید|توضیحات جدید")
        return

    if data.startswith("ownsvc:toggle:") and is_owner(uid):
        sid = int(data.rsplit(":", 1)[1])
        rows = data_of(sb.table("services").select("active,name").eq("id", sid).limit(1))
        if rows:
            active = not bool(rows[0].get("active"))
            data_of(sb.table("services").update({"active": active}).eq("id", sid))
            send(chat_id, f"✅ وضعیت خدمت «{rows[0]['name']}» تغییر کرد.")
        return

    if data.startswith("cust:view:") and is_owner(uid):
        customer_detail(chat_id, int(data.split(":")[2])); return
    if data.startswith("cust:programs:") and is_owner(uid):
        customer_program_menu(chat_id, int(data.split(":")[2])); return
    if data.startswith("cust:editprofile:") and is_owner(uid):
        set_owner_profile_state(uid, int(data.split(":")[2])); return
    if data.startswith("cust:program:") and is_owner(uid):
        _, _, target_id, program_type = data.split(":", 3)
        if program_type not in CATEGORY_NAMES: return
        set_owner_program_state(uid, int(target_id), program_type); return
    if data.startswith("cust:orders:") and is_owner(uid):
        target_uid = int(data.split(":")[2])
        orders = data_of(sb.table("orders").select("*").eq("telegram_user_id", target_uid).order("created_at", desc=True).limit(15))
        if not orders:
            send(chat_id, "این شاگرد هنوز سفارشی ندارد.", [[{"text": "بازگشت", "callback_data": f"cust:view:{target_uid}"}]])
            return
        for order in orders: order_card(chat_id, order)
        return

    if data.startswith("orderview:") and is_owner(uid):
        oid = int(data.split(":")[1])
        rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
        if rows: order_card(chat_id, rows[0])
        else: send(chat_id, "سفارش پیدا نشد.")
        return
    if data.startswith("approvequick:") and is_owner(uid):
        _, oid, eta = data.split(":", 2)
        approve_order(uid, int(oid), eta)
        return
    if data.startswith("approveeta:") and is_owner(uid):
        oid = int(data.split(":")[1])
        set_state(uid, "owner_set_eta", {"order_id": oid})
        send(chat_id, f"برای تأیید رسید سفارش #{oid}، زمان آماده‌سازی رو در پیام بعدی بنویس؛ مثلاً «۴ روز کاری» یا «یک هفته».")
        return
    if data.startswith("reject:") and is_owner(uid):
        oid = int(data.split(":")[1])
        rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
        if not rows:
            send(chat_id, "سفارش پیدا نشد."); return
        order = rows[0]
        data_of(sb.table("orders").update({"status": "awaiting_receipt", "payment_status": "rejected", "updated_at": now_iso()}).eq("id", oid))
        send(int(order["telegram_user_id"]), f"⚠️ رسید سفارش #{oid} قابل تأیید نبود. لطفاً رسید صحیح رو دوباره بفرست.")
        send(chat_id, f"رسید سفارش #{oid} رد شد.")
        return
    if data.startswith("deliverhint:") and is_owner(uid):
        oid = int(data.split(":")[1])
        rows = data_of(sb.table("orders").select("*").eq("id", oid).limit(1))
        if not rows:
            send(chat_id, "سفارش پیدا نشد."); return
        set_state(uid, "owner_deliver_order", {"order_id": oid, "customer_id": rows[0]["telegram_user_id"]})
        send(chat_id, f"متن یا فایل برنامه سفارش #{oid} رو در پیام بعدی بفرست.")
        return
    if data.startswith("reviewapprove:") and is_owner(uid):
        review_uid = int(data.split(":")[1])
        rows = data_of(sb.table("testimonials").select("id").eq("telegram_user_id", review_uid).eq("approved", False).order("created_at", desc=True).limit(1))
        if rows:
            data_of(sb.table("testimonials").update({"approved": True}).eq("id", rows[0]["id"]))
            send(chat_id, "✅ نظر منتشر شد.")
            send(review_uid, "⭐ نظر شما توسط مربی تأیید شد و در بخش سوابق نمایش داده می‌شه.")
        else: send(chat_id, "نظر در انتظار بررسی پیدا نشد.")
        return
    if data.startswith("reviewreject:") and is_owner(uid):
        review_uid = int(data.split(":")[1])
        rows = data_of(sb.table("testimonials").select("id").eq("telegram_user_id", review_uid).eq("approved", False).order("created_at", desc=True).limit(1))
        if rows: data_of(sb.table("testimonials").delete().eq("id", rows[0]["id"]))
        send(chat_id, "نظر منتشر نشد.")
        return

    if data.startswith(("approvequick:", "approveeta:", "reject:")):
        send(chat_id, "این عملیات فقط برای مربی مجازه.")

# Profile edit state is handled separately from the order form.
def handle_profile_form(uid: int, chat_id: int, text: str, state_data: dict):
    step = int(state_data.get("step", 0))
    if step >= len(FORM_QUESTIONS):
        clear_state(uid); show_profile(chat_id, uid); return
    field, label, _ = FORM_QUESTIONS[step]
    answer, error = validate_answer(field, text)
    if error:
        send(chat_id, error); ask_form_question(chat_id, step); return
    info = state_data.get("info") or {}
    info[field] = answer
    step += 1
    if step < len(FORM_QUESTIONS):
        set_state(uid, "customer_profile_form", {"step": step, "info": info})
        ask_form_question(chat_id, step)
    else:
        save_profile(uid, info)
        clear_state(uid)
        send(chat_id, "✅ پروفایلت به‌روزرسانی شد.", [[{"text": "👤 مشاهده پروفایل", "callback_data": "myprofile"}], [{"text": "منوی اصلی", "callback_data": "home"}]])

# ── Flask endpoints ─────────────────────────────────────────────────────────
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
        return jsonify({"status": "ready", "database": "ok"}), 200
    except Exception:
        log.exception("Readiness check failed")
        return jsonify({"status": "not_ready", "database": "error"}), 503


def process_update(update: dict):
    uid = 0
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
        # Do not leak DB/token details to the customer. A later message can continue the flow.
        if uid:
            try:
                send(uid, "یه خطای موقت رخ داد. لطفاً دوباره تلاش کن؛ اگر تکرار شد به مربی خبر بده.")
            except Exception:
                log.debug("Could not deliver generic error to user", exc_info=True)


@app.post("/webhook/<path_token>")
def webhook(path_token: str):
    if not hmac.compare_digest(path_token, HOOK_PATH):
        abort(403)
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(header_secret, WEBHOOK_SECRET):
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


@app.get("/set-webhook")
def set_webhook():
    setup_key = os.getenv("SETUP_KEY", "").strip()
    provided = request.args.get("key", "")
    if not setup_key or not hmac.compare_digest(provided, setup_key):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not PUBLIC_BASE_URL:
        return jsonify({"ok": False, "error": "PUBLIC_BASE_URL is not configured"}), 400
    result = tg("setWebhook", {
        "url": f"{PUBLIC_BASE_URL}/webhook/{HOOK_PATH}",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "max_connections": 40,
        "drop_pending_updates": False,
    })
    try:
        tg("setMyCommands", {"commands": [
            {"command": "start", "description": "شروع و منوی اصلی"},
            {"command": "admin", "description": "پنل مدیریت مربی"},
            {"command": "customer", "description": "مشاهده شاگرد بر اساس آیدی (مالک)"},
        ]})
    except Exception:
        log.warning("Could not register bot commands", exc_info=True)
    return jsonify({"ok": True, "telegram_result": result, "webhook_path_configured": True})


@app.get("/webhook-status")
def webhook_status():
    # Requires SETUP_KEY; do not expose getWebhookInfo publicly.
    setup_key = os.getenv("SETUP_KEY", "").strip()
    provided = request.args.get("key", "")
    if not setup_key or not hmac.compare_digest(provided, setup_key):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    result = tg("getWebhookInfo", {})
    # The actual URL is returned only to the key-holder; no bot token is exposed.
    return jsonify({"ok": True, "result": result})


def init_webhook():
    """Register the Telegram webhook automatically whenever the app process starts.

    Unlike the manual /set-webhook endpoint, this runs during module startup under
    Gunicorn too. It does not delete the webhook or drop pending updates first.
    Keep a single Gunicorn worker so startup doesn't race multiple setWebhook calls.
    """
    if not AUTO_SET_WEBHOOK:
        log.info("Automatic webhook registration disabled by AUTO_SET_WEBHOOK")
        return
    if not PUBLIC_BASE_URL:
        log.error("Cannot auto-register Telegram webhook: PUBLIC_BASE_URL is empty")
        return

    url = f"{PUBLIC_BASE_URL}/webhook/{HOOK_PATH}"
    try:
        result = tg("setWebhook", {
            "url": url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "max_connections": 40,
            "drop_pending_updates": False,
        }, timeout=15)
        if result is True:
            log.info("Telegram webhook registered automatically for %s", PUBLIC_BASE_URL)
        else:
            log.warning("Telegram setWebhook returned an unexpected result: %r", result)
    except Exception:
        # Keep the Flask service bootable; Telegram retries webhook updates and
        # the existing authenticated /set-webhook endpoint remains available.
        log.exception("Automatic Telegram webhook registration failed")

    try:
        tg("setMyCommands", {"commands": [
            {"command": "start", "description": "شروع و منوی اصلی"},
            {"command": "admin", "description": "پنل مدیریت مربی"},
            {"command": "customer", "description": "مشاهده شاگرد بر اساس آیدی (مالک)"},
        ]}, timeout=10)
    except Exception:
        log.warning("Could not register bot commands automatically", exc_info=True)


def _keepalive_loop():
    # Best-effort self-ping only. Render Free can still suspend the service.
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        if not PUBLIC_BASE_URL:
            continue
        try:
            requests.get(PUBLIC_BASE_URL + "/health", timeout=(3, 8))
            log.debug("Keepalive ping sent")
        except Exception:
            log.debug("Keepalive ping failed", exc_info=True)


# Mirror NimaBot's startup behavior: no manual browser link needed after a restart/deploy.
# With Gunicorn, use one worker; message processing itself already uses UPDATE_WORKERS.
init_webhook()
if KEEPALIVE and PUBLIC_BASE_URL:
    threading.Thread(target=_keepalive_loop, name="fitbot-keepalive", daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
