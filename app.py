import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler
import anthropic

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_COOK_CHAT_ID = os.environ.get("TELEGRAM_COOK_CHAT_ID", "")
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_STORE         = "d0d0ba.myshopify.com"
SHOPIFY_API_VER       = "2024-01"
CAIRO_TZ              = ZoneInfo("Africa/Cairo")  # handles DST automatically

AR_DAYS   = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
AR_MONTHS = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
EN_DAYS   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
EN_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
             "Jul","Aug","Sep","Oct","Nov","Dec"]

PRODUCT_MAPPINGS = [
    (r"chocolate\s+chip", "دوجا كوكيز"),
    (r"nutella",          "دوجا نوتلا تن"),
    (r"mini\s+doja",      "دوجا ميني"),
    (r"frozen.*dough",    "دوجا كوكيز مجمدة"),
    (r"doja",             "دوجا كوكيز"),
]
SIZE_MAPPINGS = {
    "6":  "(علبة 6 قطعة)",
    "12": "(علبة 12 قطعة)",
    "24": "(علبة 24 قطعة)",
    "48": "(علبة 48 قطعة)",
}

ADDRESS_RULES = (
    "Street->شارع | Road->طريق | Compound->كمبوند | Villa->فيلا | Building/Bldg->بناية\n"
    "Floor->دور | Apartment/Apt->شقة | Tower->برج | Gate->بوابة | Block->بلوك | Zone->زون\n"
    "New Cairo->القاهرة الجديدة | Sheikh Zayed->الشيخ زايد | Maadi->المعادي\n"
    "Zamalek->الزمالك | Heliopolis->مصر الجديدة | Nasr City->مدينة نصر\n"
    "Mohandessin->المهندسين | Dokki->الدقي | Giza->الجيزة | Cairo->القاهرة\n"
    "North/South/East/West->شمال/جنوب/شرق/غرب | El/Al/the->ال"
)
NAME_EXAMPLES = (
    '"Mohamed"->"محمد" | "Ahmed"->"أحمد" | "Sarah"->"سارة" | '
    '"Omar"->"عمر" | "Nour"->"نور" | "Laila"->"ليلى"'
)

# Buunto date tag pattern for fallback detection
BUUNTO_DATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{4}$"
)

# ── Date helpers ──────────────────────────────────────────────────────────────
def get_today_cairo():
    return datetime.now(CAIRO_TZ)

def get_delivery_date():
    """Returns tomorrow's date in Cairo time."""
    return datetime.now(CAIRO_TZ) + timedelta(days=1)

def buunto_tag(dt):
    """Format: 'Mon Apr 27 2026' — old Buunto tag format."""
    return "{} {} {} {}".format(EN_DAYS[dt.weekday()], EN_MONTHS[dt.month-1], dt.day, dt.year)

def iso_date(dt):
    """Format: '2026-04-27' — new theme note_attribute format."""
    return dt.strftime("%Y-%m-%d")

def arabic_date_header(dt):
    return "📦 أوردرات يوم {} {} {} {}".format(
        AR_DAYS[dt.weekday()], dt.day, AR_MONTHS[dt.month-1], dt.year)

# ── Shopify ───────────────────────────────────────────────────────────────────
def get_shopify_token():
    resp = requests.post(
        "https://{}/admin/oauth/access_token".format(SHOPIFY_STORE),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "client_id": SHOPIFY_CLIENT_ID,
              "client_secret": SHOPIFY_CLIENT_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def shopify_gql(query, variables=None, token=None):
    url  = "https://{}/admin/api/{}/graphql.json".format(SHOPIFY_STORE, SHOPIFY_API_VER)
    resp = requests.post(
        url,
        json={"query": query, **({"variables": variables} if variables else {})},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

def fetch_orders_raw(query_str, token):
    """Fetch orders matching a GraphQL query string. Returns raw nodes."""
    q = """
    query($q: String!) {
      orders(first: 50, query: $q) {
        edges { node {
          id name displayFulfillmentStatus displayFinancialStatus phone tags
          totalPriceSet { shopMoney { amount } }
          shippingAddress { name phone address1 address2 city }
          customer { firstName lastName phone }
          customAttributes { key value }
          lineItems(first: 10) { edges { node { title quantity variant { title } } } }
        }}
      }
    }"""
    result = shopify_gql(q, {"q": query_str}, token=token)
    if result.get("errors"):
        raise Exception("GraphQL errors: {}".format(result["errors"]))
    data  = result.get("data") or {}
    nodes = [e["node"] for e in (data.get("orders") or {}).get("edges", [])]
    # Ignore voided and refunded orders
    return [o for o in nodes if o.get("displayFinancialStatus") not in ("VOIDED", "REFUNDED")]

# ── note_attribute helper ─────────────────────────────────────────────────────
def get_note_attribute(order, key):
    """Read a value from order.customAttributes (GraphQL name for note_attributes)."""
    for attr in order.get("customAttributes", []) or []:
        if attr.get("key") == key:
            return attr.get("value")
    return None

# ── Bilingual order classification ────────────────────────────────────────────
def classify_order(order, target_iso, target_buunto):
    """
    Returns a dict with:
      has_matching_date  — True if this order is for the target delivery date
      fulfillment_type   — "delivery" or "pickup"
      is_no_date_warning — True if no delivery date found at all (Raghda warning)
    Checks new theme customAttributes first, falls back to Buunto tags.
    """
    delivery_date_new = get_note_attribute(order, "delivery_date")

    if delivery_date_new:
        # New theme order
        has_matching_date  = (delivery_date_new == target_iso)
        fulfillment_type   = get_note_attribute(order, "fulfillment_type") or "delivery"
        is_no_date_warning = False
    else:
        # Old theme / Buunto order
        tags_str = order.get("tags") or ""
        # tags can be a list or comma-separated string depending on API version
        if isinstance(tags_str, list):
            tags_list = [t.strip() for t in tags_str]
            tags_str  = ", ".join(tags_list)
        else:
            tags_list = [t.strip() for t in tags_str.split(",")]

        has_matching_date  = (target_buunto in tags_str)
        fulfillment_type   = "delivery"
        has_buunto_date    = any(BUUNTO_DATE_RE.match(t) for t in tags_list)
        is_no_date_warning = not has_buunto_date

    return {
        "has_matching_date":  has_matching_date,
        "fulfillment_type":   fulfillment_type,
        "is_no_date_warning": is_no_date_warning,
    }

def fulfill_order(order, token):
    """Mark order fulfilled using fulfillment_orders REST API."""
    gid      = order.get("id", "")
    order_id = gid.split("/")[-1]
    if not order_id:
        return False
    url  = "https://{}/admin/api/{}/orders/{}/fulfillment_orders.json".format(
        SHOPIFY_STORE, SHOPIFY_API_VER, order_id)
    resp = requests.get(url, headers={"X-Shopify-Access-Token": token}, timeout=15)
    if not resp.ok:
        return False
    fo_list  = resp.json().get("fulfillment_orders", [])
    open_fos = [fo["id"] for fo in fo_list if fo.get("status") == "open"]
    if not open_fos:
        return False
    payload = {"fulfillment": {"line_items_by_fulfillment_order":
               [{"fulfillment_order_id": fid} for fid in open_fos]}}
    r = requests.post(
        "https://{}/admin/api/{}/fulfillments.json".format(SHOPIFY_STORE, SHOPIFY_API_VER),
        json=payload,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=15,
    )
    return r.ok

# ── Transliteration ───────────────────────────────────────────────────────────
def has_latin(t):
    return bool(re.search(r"[a-zA-Z]", t or ""))

def transliterate_batch(texts):
    idx = [i for i, t in enumerate(texts) if has_latin(t)]
    if not idx:
        return texts
    numbered = "\n".join("{0}. {1}".format(n+1, texts[i]) for n, i in enumerate(idx))
    prompt = (
        "You are helping Egyptian delivery drivers read names and addresses aloud.\n"
        "Transliterate ONLY the English/Latin parts to Arabic phonetic spelling.\n"
        "Keep Arabic text, numbers and punctuation exactly as-is.\n"
        + ADDRESS_RULES + "\nFor names: " + NAME_EXAMPLES +
        "\nReturn ONLY a numbered list, one result per line:\n" + numbered
    )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}])
    lines  = [re.sub(r"^\d+\.\s*", "", l).strip()
              for l in resp.content[0].text.strip().split("\n") if l.strip()]
    result = list(texts)
    for n, oi in enumerate(idx):
        if n < len(lines):
            result[oi] = lines[n]
    return result

# ── Shared helpers ────────────────────────────────────────────────────────────
def fmt_phone(raw):
    if not raw:
        return "غير متوفر"
    p = re.sub(r"[\s\-\(\)\.]", "", raw)
    if p.startswith("+20"):        p = p[3:]
    elif p.startswith("0020"):     p = p[4:]
    elif p.startswith("20") and len(p) > 10: p = p[2:]
    elif p.startswith("0"):        p = p[1:]
    return "+20{}".format(p)

def map_product(title):
    lo = title.lower()
    for pat, ar in PRODUCT_MAPPINGS:
        if re.search(pat, lo):
            return ar
    return title

def map_size(vt):
    if not vt or vt == "Default Title":
        return ""
    m = re.search(r"\b(6|12|24|48)\b", vt)
    return SIZE_MAPPINGS.get(m.group(1), "") if m else "({})".format(vt)

def order_num(name):
    m = re.search(r"[-#](\d+)$", name)
    return m.group(1) if m else name.lstrip("#")

def get_customer_name(order):
    sh = order.get("shippingAddress") or {}
    cu = order.get("customer") or {}
    return sh.get("name") or (cu.get("firstName", "") + " " + cu.get("lastName", "")).strip()

# ── Arabic items list ─────────────────────────────────────────────────────────
def arabic_items(order):
    out = ""
    for e in order.get("lineItems", {}).get("edges", []):
        n   = e["node"]
        ar  = map_product(n.get("title", ""))
        sz  = map_size((n.get("variant") or {}).get("title", ""))
        out += "\n- {}x {} {}".format(n.get("quantity", 1), ar, sz).rstrip()
    return out

# ── Delivery group messages (Arabic) ─────────────────────────────────────────
def fmt_pending(order, seq):
    sh    = order.get("shippingAddress") or {}
    cu    = order.get("customer") or {}
    rn    = get_customer_name(order)
    a1    = sh.get("address1", "")
    a2    = sh.get("address2", "")
    ci    = sh.get("city", "")
    an, aa1, aa2, aci = transliterate_batch([rn, a1, a2, ci])
    addr  = "، ".join(p for p in [aa1, aa2, aci] if p)
    phone = fmt_phone(sh.get("phone") or order.get("phone") or cu.get("phone", ""))
    total = str(round(float(
        order.get("totalPriceSet", {}).get("shopMoney", {}).get("amount", "0"))))
    warning = ""
    if order.get("_no_date_warning"):
        warning = "\n‼️‼️‼️‼️ لا يوجد تاريخ تسليم الرجوع لمدام رغدة ‼️‼️‼️‼️"
    if order.get("displayFinancialStatus") == "PAID":
        payment_line = "💰 تم الدفع مسبقا"
    else:
        payment_line = "💰 المبلغ المطلوب تحصيله: {} جنيه".format(total)
    return (
        "🛵 أوردر رقم: {}-{}\n"
        "👤 اسم العميل: {}{}\n"
        "📞 رقم التليفون: {}\n"
        "📍 العنوان: {}\n"
        "🛍️ الطلبات:{}\n"
        "{}"
    ).format(str(seq).zfill(3), order_num(order.get("name", "")),
             an, warning, phone, addr, arabic_items(order), payment_line)

def fmt_fulfilled(order, seq):
    an = transliterate_batch([get_customer_name(order)])[0]
    return (
        "✅✅✅✅ تم التسليم مسبقا ✅✅✅✅\n"
        "🛵 أوردر رقم: {}-{}\n"
        "👤 اسم العميل: {}\n"
        "🛍️ الطلبات:{}\n"
        "✅✅✅✅ تم التسليم مسبقا ✅✅✅✅"
    ).format(str(seq).zfill(3), order_num(order.get("name", "")),
             an, arabic_items(order))

# ── Cook group messages (English) ─────────────────────────────────────────────
def fmt_cook(order, seq, fulfillment_type="delivery"):
    name    = get_customer_name(order)
    seq_str = str(seq).zfill(3)
    num     = order_num(order.get("name", ""))
    lines   = []
    for e in order.get("lineItems", {}).get("edges", []):
        item    = e["node"]
        qty     = item.get("quantity", 1)
        title   = item.get("title", "")
        variant = (item.get("variant") or {}).get("title", "")
        if variant and variant != "Default Title":
            lines.append("- {}x {} ({})".format(qty, title, variant))
        else:
            lines.append("- {}x {}".format(qty, title))
    items_str = "\n".join(lines)
    warning = ""
    if order.get("_no_date_warning"):
        warning = "\n\u203c\ufe0f\u203c\ufe0f\u203c\ufe0f\u203c\ufe0f NO DELIVERY DATE \u2014 CHECK WITH RAGHDA \u203c\ufe0f\u203c\ufe0f\u203c\ufe0f\u203c\ufe0f"
    prefix = "🚚 DELIVERY ORDER" if fulfillment_type == "delivery" else "🏪 PICKUP ORDER"
    return "{}\n\nOrder #{}-{}\nCustomer: {}{}\nItems:\n{}".format(
        prefix, seq_str, num, name, warning, items_str)

# ── Telegram senders ──────────────────────────────────────────────────────────
def send_tg(msg):
    requests.post(
        "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
        timeout=10,
    )

def send_cook(msg):
    requests.post(
        "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
        json={"chat_id": TELEGRAM_COOK_CHAT_ID, "text": msg},
        timeout=10,
    )

# ── Main cron job ─────────────────────────────────────────────────────────────
def run_cron():
    token         = get_shopify_token()
    today         = get_today_cairo()
    delivery_date = get_delivery_date()           # tomorrow
    target_buunto = buunto_tag(delivery_date)     # "Mon Apr 28 2026"
    target_iso    = iso_date(delivery_date)       # "2026-04-28"

    # Fetch all unfulfilled orders — we'll classify them ourselves
    all_unfulfilled = fetch_orders_raw("fulfillment_status:unfulfilled", token)
    # Also fetch fulfilled orders that have today's buunto tag OR tomorrow's ISO date
    # (for the checkmarks message)
    all_fulfilled_tagged = fetch_orders_raw(
        'tag:"{}" fulfillment_status:fulfilled'.format(target_buunto), token)
    # Also check fulfilled orders with new theme note_attribute date
    # (these won't have Buunto tags so we fetch recent fulfilled and classify)
    recent_fulfilled = fetch_orders_raw("fulfillment_status:fulfilled", token)

    # Classify each unfulfilled order
    delivery_pending  = []  # delivery orders for tomorrow
    pickup_pending    = []  # pickup orders for tomorrow
    no_date_orders    = []  # unfulfilled with no date at all (Raghda warning)
    seen_ids          = set()

    for order in all_unfulfilled:
        c = classify_order(order, target_iso, target_buunto)
        if c["is_no_date_warning"]:
            order["_no_date_warning"] = True
            if order["id"] not in seen_ids:
                no_date_orders.append(order)
                seen_ids.add(order["id"])
        elif c["has_matching_date"]:
            order["_fulfillment_type"] = c["fulfillment_type"]
            if order["id"] not in seen_ids:
                if c["fulfillment_type"] == "pickup":
                    pickup_pending.append(order)
                else:
                    delivery_pending.append(order)
                seen_ids.add(order["id"])

    # Classify fulfilled orders (checkmarks — delivery only, skip pickup)
    done = []
    for order in recent_fulfilled:
        c = classify_order(order, target_iso, target_buunto)
        if c["has_matching_date"] and c["fulfillment_type"] == "delivery":
            if order["id"] not in seen_ids:
                done.append(order)
                seen_ids.add(order["id"])

    all_pending  = delivery_pending + no_date_orders  # pickup handled separately
    total_orders = all_pending + pickup_pending + done

    # ── Delivery group (Arabic) — delivery orders only ──────────────────────
    send_tg(arabic_date_header(delivery_date))
    if not total_orders:
        send_tg("مفيش توصيلات النهارده 🎉")
    else:
        for i, o in enumerate(all_pending, 1):
            send_tg(fmt_pending(o, i))
        for i, o in enumerate(done, len(all_pending) + 1):
            send_tg(fmt_fulfilled(o, i))
        # Pickup orders: skip delivery group entirely

    # ── Cook group (English) — ALL orders including pickup ───────────────────
    send_cook("📦 Doja Cook — Orders for {}".format(
        delivery_date.strftime("%a %d %b %Y")))
    if not total_orders:
        send_cook("No orders today 🎉")
    else:
        seq = 1
        for o in delivery_pending:
            send_cook(fmt_cook(o, seq, "delivery"))
            seq += 1
        for o in pickup_pending:
            send_cook(fmt_cook(o, seq, "pickup"))
            seq += 1
        for o in no_date_orders:
            send_cook(fmt_cook(o, seq, "delivery"))
            seq += 1

    # ── Mark all pending delivery+pickup+no-date as fulfilled in Shopify ─────
    for o in delivery_pending + pickup_pending + no_date_orders:
        fulfill_order(o, token)

    return {
        "delivery_date":    target_iso,
        "delivery_pending": len(delivery_pending),
        "pickup_pending":   len(pickup_pending),
        "no_date":          len(no_date_orders),
        "fulfilled":        len(done),
    }

# ── Backup endpoint ───────────────────────────────────────────────────────────
def run_backup():
    """
    Manual only. Scans TODAY's PAID + FULFILLED orders and sends
    standard messages to both groups with تم الدفع مسبقا.
    Does NOT change fulfillment status in Shopify.
    """
    token         = get_shopify_token()
    today         = get_today_cairo()
    today_buunto  = buunto_tag(today)
    today_iso     = iso_date(today)

    all_orders = fetch_orders_raw("fulfillment_status:fulfilled", token)

    backup_orders = []
    for o in all_orders:
        if o.get("displayFinancialStatus") != "PAID":
            continue
        c = classify_order(o, today_iso, today_buunto)
        if c["has_matching_date"]:
            o["_fulfillment_type"] = c["fulfillment_type"]
            backup_orders.append(o)

    if not backup_orders:
        send_tg("لا يوجد أوردرات مدفوعة ومسلمة اليوم")
        send_cook("No paid & fulfilled orders found for today.")
        return {"today": today_iso, "backup_sent": 0}

    send_tg(arabic_date_header(today))
    send_cook("📦 Doja Cook — Backup Messages for {}".format(today.strftime("%a %d %b %Y")))

    for i, o in enumerate(backup_orders, 1):
        ft = o.get("_fulfillment_type", "delivery")
        send_tg(fmt_pending(o, i))           # delivery group always gets Arabic
        send_cook(fmt_cook(o, i, ft))        # cook gets delivery or pickup prefix

    return {"today": today_iso, "backup_sent": len(backup_orders)}

# ── Vercel handler ────────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/cron":
            try:
                result = run_cron()
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        elif path == "/api/backup":
            try:
                result = run_backup()
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(200, {"status": "Doja Delivery Bot running",
                                "trigger": "/api/cron", "backup": "/api/backup"})

    def do_POST(self):
        self.do_GET()

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def log_message(self, *a):
        pass
