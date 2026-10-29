import os, json, logging, secrets, requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from supabase import create_client, Client

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("fit-coach-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
OWNER_ID = int(os.environ["OWNER_ID"])
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = Flask(__name__)

MAIN_KEYBOARD = [
    [{"text": "🥗 برنامه تخصصی تغذیه", "callback_data": "cat:nutrition"},
     {"text": "🏋️ برنامه تمرینی باشگاه", "callback_data": "cat:gym"}],
    [{"text": "🏠 برنامه تمرینی منزل", "callback_data": "cat:home"},
     {"text": "💬 مشاوره با مربی", "callback_data": "consult"}],
    [{"text": "🏆 سوابق و رضایت مشتریان", "callback_data": "portfolio"},
     {"text": "📩 پیشنهادات و انتقادات", "callback_data": "feedback"}],
    [{"text": "📦 سفارش‌های من", "callback_data": "myorders"}]
]

def tg(method, payload=None, files=None, timeout=20):
    r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, files=files, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data.get("result")

def send(chat_id, text, keyboard=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg("sendMessage", payload)

def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
    except Exception:
        log.exception("answerCallbackQuery failed")

def is_owner(user_id):
    return int(user_id) == OWNER_ID

def upsert_state(user_id, state, data=None):
    sb.table("user_states").upsert({
        "telegram_user_id": int(user_id), "state": state,
        "data": data or {}, "updated_at": datetime.now(timezone.utc).isoformat()
    }, on_conflict="telegram_user_id").execute()

def get_state(user_id):
    result = sb.table("user_states").select("*").eq("telegram_user_id", int(user_id)).limit(1).execute()
    return result.data[0] if result.data else {"state": "idle", "data": {}}

def clear_state(user_id):
    sb.table("user_states").upsert({
        "telegram_user_id": int(user_id), "state": "idle", "data": {},
        "updated_at": datetime.now(timezone.utc).isoformat()
    }, on_conflict="telegram_user_id").execute()

def get_or_create_customer(user):
    uid = int(user["id"])
    data = {
        "telegram_user_id": uid,
        "username": user.get("username"),
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", "")
    }
    sb.table("customers").upsert(data, on_conflict="telegram_user_id").execute()

def main_menu(chat_id):
    send(chat_id, "سلام 👋 به ربات خدمات مربیگری خوش اومدی.\nاز منوی زیر خدمت موردنظرت رو انتخاب کن:", MAIN_KEYBOARD)

def services_for(category):
    return sb.table("services").select("*").eq("category", category).eq("active", True).order("created_at").execute().data

def category_label(category):
    return {"nutrition":"برنامه تخصصی تغذیه", "gym":"برنامه تمرینی باشگاه", "home":"برنامه تمرینی منزل"}.get(category, category)

def show_services(chat_id, category):
    services = services_for(category)
    if not services:
        send(chat_id, f"فعلاً موردی در بخش «{category_label(category)}» ثبت نشده.\nبرای هماهنگی می‌تونی از بخش مشاوره با مربی پیام بدی.", [[{"text":"بازگشت به منو", "callback_data":"home"}]])
        return
    keyboard = [[{"text": f"{s['name']} — {s['price']} تومان", "callback_data": f"service:{s['id']}"}] for s in services]
    keyboard.append([{"text":"🔙 بازگشت", "callback_data":"home"}])
    send(chat_id, f"یکی از خدمات «{category_label(category)}» رو انتخاب کن:", keyboard)

def service_detail(chat_id, service_id):
    rows = sb.table("services").select("*").eq("id", service_id).eq("active", True).limit(1).execute().data
    if not rows:
        send(chat_id, "این خدمت در دسترس نیست.", [[{"text":"بازگشت", "callback_data":"home"}]])
        return
    s = rows[0]
    msg = f"📌 {s['name']}\n\n{(s.get('description') or 'توضیحی ثبت نشده.')}\n\n💰 قیمت: {s['price']} تومان\n\nبرای ثبت سفارش روی دکمه زیر بزن."
    send(chat_id, msg, [[{"text":"📝 ثبت سفارش", "callback_data":f"order:{s['id']}"}],
                        [{"text":"🔙 بازگشت به خدمات", "callback_data":f"cat:{s['category']}"}]])

def start_order(user, service_id):
    uid = int(user["id"])
    rows = sb.table("services").select("*").eq("id", service_id).eq("active", True).limit(1).execute().data
    if not rows:
        send(uid, "این خدمت پیدا نشد یا غیرفعال شده است.")
        return
    s = rows[0]
    order = sb.table("orders").insert({
        "telegram_user_id": uid, "service_id": s["id"], "service_name": s["name"],
        "category": s["category"], "price": int(s["price"]), "status": "collecting_info",
        "customer_info": {}, "payment_status": "unpaid"
    }).execute().data[0]
    upsert_state(uid, "order_form", {"order_id": order["id"], "step": 0, "info": {}})
    send(uid, f"سفارش شماره #{order['id']} ایجاد شد.\nبرای آماده‌سازی برنامه اختصاصی، لطفاً چند سؤال کوتاه رو جواب بده.\n\nهر زمان خواستی لغو کنی بنویس «لغو».")
    ask_form_question(uid, 0)

FORM_QUESTIONS = [
    ("سن", "سن‌ت چند ساله؟ (فقط عدد)"),
    ("قد", "قدت چند سانتی‌متره؟ (فقط عدد)"),
    ("وزن", "وزنت چند کیلوگرمه؟ (فقط عدد)"),
    ("جنسیت", "جنسیتت چیه؟ (زن / مرد / ترجیح می‌دم نگویم)"),
    ("گروه خونی", "گروه خونیت چیه؟ اگر نمی‌دونی بنویس «نمی‌دانم»."),
    ("فعالیت روزانه", "میزان فعالیت روزانه‌ات چطوره؟ (کم / متوسط / زیاد)"),
    ("هدف", "هدفت چیه؟ (مثلاً کاهش وزن، عضله‌سازی، افزایش قدرت، تناسب اندام)"),
    ("محدودیت‌ها و توضیحات", "محدودیت غذایی، حساسیت، آسیب‌دیدگی یا توضیح مهمی داری؟ اگر نه بنویس «ندارم».")
]

def ask_form_question(chat_id, step):
    if step < len(FORM_QUESTIONS):
        label, question = FORM_QUESTIONS[step]
        send(chat_id, f"سؤال {step+1} از {len(FORM_QUESTIONS)} — {label}\n{question}")
    else:
        send(chat_id, "اطلاعاتت ثبت شد. حالا رسید پرداخت رو به صورت عکس یا فایل ارسال کن.\n\nبعد از بررسی و تأیید مربی، سفارش نهایی می‌شه. اگر هنوز پرداخت نکردی، فعلاً بنویس «لغو» تا بعداً ادامه بدی.")

def create_order_summary(order, user):
    info = order.get("customer_info") or {}
    uname = ("@" + user["username"]) if user.get("username") else "ندارد"
    return (f"🆕 سفارش جدید #{order['id']}\n"
            f"خدمت: {order['service_name']}\nقیمت: {order['price']} تومان\n"
            f"مشتری: {user.get('first_name','')} {user.get('last_name','')}\n"
            f"آیدی: {uname}\nآیدی عددی: {order['telegram_user_id']}\n"
            f"وضعیت پرداخت: {order.get('payment_status','unpaid')}\n\n"
            "مشخصات:\n" + "\n".join(f"• {k}: {v}" for k,v in info.items()))

def handle_message(message):
    user = message.get("from", {})
    if not user or not message.get("chat"):
        return
    uid = int(user["id"])
    chat_id = int(message["chat"]["id"])
    get_or_create_customer(user)
    text = (message.get("text") or message.get("caption") or "").strip()

    if text.startswith("/start"):
        clear_state(uid); main_menu(chat_id); return
    if text.startswith("/cancel") or text == "لغو":
        state = get_state(uid)
        if state.get("state") == "order_form":
            oid = (state.get("data") or {}).get("order_id")
            if oid:
                sb.table("orders").update({"status":"cancelled"}).eq("id", oid).eq("telegram_user_id", uid).execute()
        clear_state(uid)
        send(chat_id, "فرآیند فعلی لغو شد.", [[{"text":"منوی اصلی", "callback_data":"home"}]])
        return

    if is_owner(uid) and text.startswith("/admin"):
        owner_menu(chat_id); return
    if is_owner(uid) and text.startswith("/addservice "):
        # Format: /addservice category|name|price|description
        try:
            parts = text[len("/addservice "):].split("|", 3)
            if len(parts) != 4: raise ValueError()
            category, name, price, desc = [p.strip() for p in parts]
            if category not in ("nutrition","gym","home") or not price.isdigit():
                raise ValueError()
            sb.table("services").insert({"category":category,"name":name,"price":int(price),"description":desc,"active":True}).execute()
            send(chat_id, "✅ خدمت اضافه شد.")
        except Exception:
            send(chat_id, "فرمت درست:\n/addservice nutrition|نام خدمت|قیمت عددی|توضیحات\nدسته‌ها: nutrition یا gym یا home")
        return
    if is_owner(uid) and text.startswith("/addportfolio "):
        try:
            parts = text[len("/addportfolio "):].split("|", 1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                raise ValueError()
            sb.table("portfolio_items").insert({
                "title": parts[0].strip()[:160],
                "body": parts[1].strip()[:2000],
                "active": True
            }).execute()
            send(chat_id, "✅ سابقه/نمونه‌کار به بخش سوابق مربی اضافه شد.")
        except Exception:
            send(chat_id, "فرمت درست:\n/addportfolio عنوان سابقه|توضیحات")
        return
    if is_owner(uid) and text.startswith("/delportfolio "):
        try:
            item_id = int(text.split(maxsplit=1)[1])
            sb.table("portfolio_items").update({"active":False}).eq("id",item_id).execute()
            send(chat_id, f"سابقه #{item_id} غیرفعال شد.")
        except Exception:
            send(chat_id, "فرمت: /delportfolio شناسه")
        return
    if is_owner(uid) and text.startswith("/delservice "):
        try:
            sid = int(text.split(maxsplit=1)[1])
            sb.table("services").update({"active":False}).eq("id",sid).execute()
            send(chat_id, f"خدمت #{sid} غیرفعال شد.")
        except Exception:
            send(chat_id, "فرمت: /delservice شناسه_خدمت")
        return
    if is_owner(uid) and text.startswith("/deliver "):
        try:
            oid = int(text.split(maxsplit=1)[1])
            rows = sb.table("orders").select("*").eq("id",oid).limit(1).execute().data
            if not rows: raise ValueError()
            upsert_state(uid, "deliver_program", {"order_id":oid})
            send(chat_id, f"متن برنامه سفارش #{oid} رو در پیام بعدی بفرست. می‌تونی فایل برنامه رو هم بعدش جداگانه برای مشتری ارسال کنی.")
        except Exception:
            send(chat_id, "فرمت: /deliver شناسه_سفارش")
        return

    state_row = get_state(uid)
    state, data = state_row.get("state","idle"), state_row.get("data") or {}
    if state == "order_form":
        step = int(data.get("step", 0))
        if step < len(FORM_QUESTIONS):
            if text:
                label, _ = FORM_QUESTIONS[step]
                info = data.get("info") or {}
                info[label] = text[:1000]
                step += 1
                data.update({"step":step,"info":info})
                upsert_state(uid, "order_form", data)
                if step < len(FORM_QUESTIONS):
                    ask_form_question(chat_id, step)
                else:
                    oid = data["order_id"]
                    sb.table("orders").update({"customer_info":info,"status":"awaiting_receipt"}).eq("id",oid).eq("telegram_user_id",uid).execute()
                    send(chat_id, "✅ مشخصاتت ثبت شد.\nحالا رسید پرداخت رو به شکل عکس یا فایل ارسال کن. مربی بعد از بررسی، پرداخت رو تأیید می‌کنه.")
            else:
                send(chat_id, "لطفاً پاسخ این سؤال رو به صورت متن ارسال کن.")
            return
    elif state == "deliver_program" and is_owner(uid):
        oid = data.get("order_id")
        if text:
            rows = sb.table("orders").select("*").eq("id",oid).limit(1).execute().data
            if rows:
                order = rows[0]
                send(order["telegram_user_id"], f"🎉 برنامه اختصاصی سفارش #{oid} آماده است!\n\n{text}\n\nممنون از اعتمادت 🌷")
                sb.table("orders").update({"status":"delivered","program_text":text,"delivered_at":datetime.now(timezone.utc).isoformat()}).eq("id",oid).execute()
                send(chat_id, f"✅ برنامه متنی سفارش #{oid} برای مشتری ارسال شد.")
            clear_state(uid); return
    elif state == "review":
        if text:
            sb.table("testimonials").insert({
                "telegram_user_id": uid,
                "display_name": (user.get("first_name") or "مشتری")[:80],
                "review_text": text[:1200],
                "approved": False
            }).execute()
            send(chat_id, "ممنون از ثبت نظرت ❤️ نظر برای بررسی به مربی ارسال شد و بعد از تأیید نمایش داده می‌شه.")
            send(OWNER_ID, f"⭐ نظر جدید از کاربر {uid} (@{user.get('username','ندارد')}):\n{text[:1200]}",
                 [[{"text":"✅ انتشار نظر","callback_data":f"reviewapprove:{uid}"},
                   {"text":"🗑 رد نظر","callback_data":f"reviewreject:{uid}"}]])
        else:
            send(chat_id, "لطفاً نظر رو به صورت متن بفرست.")
        clear_state(uid)
        return
    elif state == "feedback":
        sb.table("feedback").insert({"telegram_user_id":uid,"message":text or "[پیام غیرمتنی]"}).execute()
        send(OWNER_ID, f"📩 بازخورد جدید از کاربر {uid} (@{user.get('username','ندارد')}):\n{text or '[پیام غیرمتنی]'}")
        clear_state(uid); send(chat_id, "ممنون از اینکه نظرت رو فرستادی ❤️", [[{"text":"منوی اصلی","callback_data":"home"}]]); return

    # A receipt is accepted only when an order is waiting for it.
    if message.get("photo") or message.get("document"):
        pending = sb.table("orders").select("*").eq("telegram_user_id",uid).eq("status","awaiting_receipt").order("created_at", desc=True).limit(1).execute().data
        if pending:
            order = pending[0]
            file_id = (message.get("photo") or [message.get("document")])[-1].get("file_id")
            sb.table("orders").update({"status":"pending_review","payment_status":"receipt_submitted","receipt_file_id":file_id}).eq("id",order["id"]).execute()
            send(chat_id, f"🧾 رسید سفارش #{order['id']} دریافت شد و برای بررسی به مربی ارسال شد.")
            summary = create_order_summary({**order, "status":"pending_review"}, user)
            keyboard = [[{"text":"✅ تأیید پرداخت و سفارش","callback_data":f"approve:{order['id']}"},
                         {"text":"❌ رد رسید","callback_data":f"reject:{order['id']}"}]]
            send(OWNER_ID, summary, keyboard)
            try:
                if message.get("photo"):
                    tg("sendPhoto", {"chat_id":OWNER_ID,"photo":file_id,"caption":f"رسید سفارش #{order['id']} از کاربر {uid}"})
                else:
                    tg("sendDocument", {"chat_id":OWNER_ID,"document":file_id,"caption":f"رسید سفارش #{order['id']} از کاربر {uid}"})
            except Exception:
                log.exception("Could not forward receipt")
            clear_state(uid)
            return
        send(chat_id, "رسید برای سفارش فعالی پیدا نشد. ابتدا از منو یک سفارش ثبت کن.", [[{"text":"منوی اصلی","callback_data":"home"}]])
        return

    if text and not text.startswith("/"):
        send(chat_id, "برای استفاده از خدمات از منوی زیر انتخاب کن.", MAIN_KEYBOARD)

def owner_menu(chat_id):
    keyboard = [
        [{"text":"🕒 سفارش‌های در انتظار بررسی","callback_data":"owner:pending"}],
        [{"text":"📋 همه سفارش‌ها","callback_data":"owner:orders"}],
        [{"text":"➕ راهنمای افزودن خدمت","callback_data":"owner:addhelp"}],
        [{"text":"🔙 منوی اصلی","callback_data":"home"}]
    ]
    send(chat_id, "پنل مدیریت مربی", keyboard)

def handle_callback(q):
    user = q.get("from", {})
    uid = int(user.get("id",0))
    chat_id = int(q.get("message",{}).get("chat",{}).get("id",uid))
    data = q.get("data","")
    answer_callback(q.get("id",""))
    get_or_create_customer(user)

    if data == "home":
        clear_state(uid); main_menu(chat_id); return
    if data.startswith("cat:"):
        show_services(chat_id, data.split(":",1)[1]); return
    if data.startswith("service:"):
        service_detail(chat_id, int(data.split(":",1)[1])); return
    if data.startswith("order:"):
        start_order(user, int(data.split(":",1)[1])); return
    if data == "consult":
        username = os.getenv("COACH_USERNAME", "").strip().lstrip("@")
        if username:
            send(chat_id, f"برای مشاوره مستقیم به مربی پیام بده:\nhttps://t.me/{username}")
        else:
            send(chat_id, "برای اتصال مستقیم به مربی، آیدی مربی هنوز تنظیم نشده. لطفاً کمی بعد دوباره تلاش کن.")
        return
    if data == "portfolio":
        items = sb.table("portfolio_items").select("*").eq("active", True).order("created_at", desc=True).limit(15).execute().data
        reviews = sb.table("testimonials").select("*").eq("approved", True).order("created_at", desc=True).limit(15).execute().data
        parts = ["🏆 سوابق کاری مربی"]
        if items:
            for item in items:
                parts.append(f"\n• {item['title']}\n{item['body']}")
        else:
            parts.append("\nهنوز سابقه یا نمونه‌کاری ثبت نشده است.")
        parts.append("\n\n⭐ رضایت مشتریان")
        if reviews:
            for review in reviews:
                parts.append(f"\n• {review.get('display_name') or 'مشتری'}: {review['review_text']}")
        else:
            parts.append("\nهنوز نظری منتشر نشده است.")
        send(chat_id, "".join(parts), [
            [{"text":"✍️ ثبت نظر و رضایت","callback_data":"review"}],
            [{"text":"🔙 بازگشت","callback_data":"home"}]
        ])
        return
    if data == "review":
        upsert_state(uid, "review", {})
        send(chat_id, "نظر و تجربه‌ات از خدمات مربی رو در پیام بعدی بنویس. نظر بعد از بررسی مربی برای بقیه نمایش داده می‌شه.")
        return
    if data == "feedback":
        upsert_state(uid, "feedback", {})
        send(chat_id, "پیام، پیشنهاد یا انتقادت رو در پیام بعدی بنویس."); return
    if data == "myorders":
        orders = sb.table("orders").select("id,service_name,price,status,payment_status,created_at").eq("telegram_user_id",uid).order("created_at",desc=True).limit(10).execute().data
        if not orders:
            send(chat_id, "هنوز سفارشی ثبت نکردی.", [[{"text":"منوی اصلی","callback_data":"home"}]]); return
        lines = []
        for o in orders:
            lines.append(f"#{o['id']} — {o['service_name']} — {o['price']} تومان\nوضعیت: {o['status']} | پرداخت: {o['payment_status']}")
        send(chat_id, "📦 سفارش‌های تو:\n\n" + "\n\n".join(lines), [[{"text":"منوی اصلی","callback_data":"home"}]]); return

    if data.startswith("owner:") and is_owner(uid):
        action = data.split(":",1)[1]
        if action == "addhelp":
            send(chat_id, "افزودن خدمت:\n/addservice nutrition|نام خدمت|قیمت عددی|توضیحات\nدسته‌ها: nutrition ، gym ، home\nغیرفعال کردن خدمت: /delservice شناسه\nارسال برنامه متنی: /deliver شناسه_سفارش\n\nافزودن سابقه/نمونه‌کار:\n/addportfolio عنوان|توضیحات\nغیرفعال کردن سابقه: /delportfolio شناسه\n\nنظرهای مشتریان از بخش «سوابق و رضایت مشتریان» ارسال می‌شن و برای انتشار نیاز به تأیید مالک دارن.")
            return
        query = sb.table("orders").select("*").order("created_at",desc=True).limit(20)
        if action == "pending":
            orders = query.in_("status", ["pending_review","awaiting_receipt"]).execute().data
        else:
            orders = query.execute().data
        if not orders:
            send(chat_id, "سفارشی برای نمایش وجود نداره.", [[{"text":"بازگشت پنل","callback_data":"owner:menu"}]]); return
        for o in orders:
            msg = f"سفارش #{o['id']} — {o['service_name']}\nقیمت: {o['price']} تومان\nکاربر: {o['telegram_user_id']}\nوضعیت: {o['status']}\nپرداخت: {o.get('payment_status','unpaid')}"
            kb = []
            if o["status"] == "pending_review":
                kb = [[{"text":"✅ تأیید پرداخت","callback_data":f"approve:{o['id']}"},
                       {"text":"❌ رد رسید","callback_data":f"reject:{o['id']}"}]]
            kb.append([{"text":"📨 ارسال برنامه","callback_data":f"deliverhint:{o['id']}"}])
            send(chat_id,msg,kb)
        return
    if data == "owner:menu" and is_owner(uid):
        owner_menu(chat_id); return
    if data.startswith("reviewapprove:") and is_owner(uid):
        review_uid = int(data.split(":",1)[1])
        rows = sb.table("testimonials").select("*").eq("telegram_user_id",review_uid).eq("approved",False).order("created_at",desc=True).limit(1).execute().data
        if rows:
            sb.table("testimonials").update({"approved":True}).eq("id",rows[0]["id"]).execute()
            send(chat_id, "✅ نظر منتشر شد.")
            send(review_uid, "⭐ نظر شما توسط مربی تأیید شد و در بخش رضایت مشتریان نمایش داده می‌شه.")
        else:
            send(chat_id, "نظر در انتظار بررسی پیدا نشد.")
        return
    if data.startswith("reviewreject:") and is_owner(uid):
        review_uid = int(data.split(":",1)[1])
        rows = sb.table("testimonials").select("*").eq("telegram_user_id",review_uid).eq("approved",False).order("created_at",desc=True).limit(1).execute().data
        if rows:
            sb.table("testimonials").delete().eq("id",rows[0]["id"]).execute()
        send(chat_id, "نظر منتشر نشد.")
        return
    if data.startswith("approve:") and is_owner(uid):
        oid = int(data.split(":")[1])
        rows = sb.table("orders").select("*").eq("id",oid).limit(1).execute().data
        if not rows:
            send(chat_id,"سفارش پیدا نشد."); return
        order = rows[0]
        sb.table("orders").update({"status":"approved","payment_status":"approved","approved_at":datetime.now(timezone.utc).isoformat()}).eq("id",oid).execute()
        send(order["telegram_user_id"], f"✅ پرداخت و سفارش #{oid} تأیید شد.\n\nسفارش شما معمولاً طی ۳ تا ۴ روز کاری آماده می‌شه. ممنون از خرید و اعتمادت ❤️\nپس از آماده شدن برنامه، همین‌جا برات ارسال می‌شه.")
        send(chat_id, f"سفارش #{oid} تأیید شد. برای ارسال برنامه از /deliver {oid} استفاده کن.")
        return
    if data.startswith("reject:") and is_owner(uid):
        oid = int(data.split(":")[1])
        rows = sb.table("orders").select("*").eq("id",oid).limit(1).execute().data
        if rows:
            order = rows[0]
            sb.table("orders").update({"status":"awaiting_receipt","payment_status":"rejected"}).eq("id",oid).execute()
            send(order["telegram_user_id"], f"⚠️ رسید سفارش #{oid} قابل تأیید نبود. لطفاً با مربی تماس بگیر و رسید صحیح رو ارسال کن.")
            send(chat_id, f"رسید سفارش #{oid} رد شد.")
        return
    if data.startswith("deliverhint:") and is_owner(uid):
        oid = int(data.split(":")[1])
        send(chat_id, f"برای ارسال برنامه سفارش #{oid} این دستور رو بفرست:\n/deliver {oid}")
        return
    if data.startswith("approve:") or data.startswith("reject:"):
        send(chat_id,"این عملیات فقط برای مربی مجازه."); return

@app.get("/")
def index():
    return jsonify({"ok": True, "service": "fitness-coach-telegram-bot"})

@app.get("/health")
def health():
    # Liveness endpoint; deliberately fast and independent of Supabase.
    return jsonify({"status":"ok"}), 200

@app.get("/ready")
def ready():
    try:
        sb.table("customers").select("telegram_user_id").limit(1).execute()
        return jsonify({"status":"ready","database":"ok"}), 200
    except Exception:
        log.exception("Readiness check failed")
        return jsonify({"status":"not_ready","database":"error"}), 503

@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return jsonify({"ok":False}), 403
    update = request.get_json(silent=True) or {}
    try:
        if update.get("message"):
            handle_message(update["message"])
        elif update.get("callback_query"):
            handle_callback(update["callback_query"])
    except Exception:
        log.exception("Failed processing Telegram update")
        # Return 200 to prevent Telegram from retrying a partially processed update indefinitely.
    return jsonify({"ok":True}), 200

@app.get("/set-webhook")
def set_webhook():
    # Protect this endpoint with a one-time setup key.
    setup_key = os.getenv("SETUP_KEY", "")
    if not setup_key or request.args.get("key") != setup_key:
        return jsonify({"ok":False,"error":"forbidden"}), 403
    if not PUBLIC_BASE_URL:
        return jsonify({"ok":False,"error":"PUBLIC_BASE_URL is not configured"}), 400
    result = tg("setWebhook", {
        "url": f"{PUBLIC_BASE_URL}/webhook",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message","callback_query"],
        "max_connections": 10
    })
    return jsonify({"ok":True,"telegram_result":result})

def setup_commands():
    try:
        tg("setMyCommands", {"commands":[
            {"command":"start","description":"شروع و منوی اصلی"},
            {"command":"admin","description":"پنل مدیریت مربی"}
        ]})
    except Exception:
        log.exception("Could not set bot commands")

if __name__ == "__main__":
    setup_commands()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
