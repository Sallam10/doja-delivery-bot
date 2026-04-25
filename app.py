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

# ── Date helpers ──────────────────────────────────────────────────────────────
def get_today_cairo():
    return datetime.now(CAIRO_TZ)

def get_delivery_date():
    """Returns tomorrow's date in Cairo time — used for fetching next-day delivery orders."""
    return datetime.now(CAIRO_TZ) + timedelta(days=1)

def buunto_tag(dt):
    return "{} {} {} {}".format(EN_DAYS[dt.weekday()], EN_MONTHS[dt.month-1], dt.day, dt.year)

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

def fetch_orders(tag, token):
    q = """
    query($q: String!) {
      orders(first: 50, query: $q) {
        edges { node {
          id name displayFulfillmentStatus displayFinancialStatus phone
          totalPriceSet { shopMoney { amount } }
          shippingAddress { name phone address1 address2 city }
          customer { firstName lastName phone }
          lineItems(first: 10) { edges { node { title quantity variant { title } } } }
        }}
      }
    }"""
    result = shopify_gql(q, {"q": 'tag:"{}"'.format(tag)}, token=token)
    if result.get("errors"):
        raise Exception("GraphQL errors: {}".format(result["errors"]))
    data  = result.get("data") or {}
    nodes = [e["node"] for e in (data.get("orders") or {}).get("edges", [])]
    # Ignore voided and refunded orders
    return [o for o in nodes if o.get("displayFinancialStatus") not in ("VOIDED", "REFUNDED")]

def fetch_untagged_unfulfilled(token, today_tag):
    """Fetch unfulfilled orders that have NO Buunto delivery date tag."""
    import re
    date_pattern = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{4}\b")
    q = """
    query($q: String!) {
      orders(first: 50, query: $q) {
        edges { node {
          id name displayFulfillmentStatus displayFinancialStatus phone
          totalPriceSet { shopMoney { amount } }
          shippingAddress { name phone address1 address2 city }
          customer { firstName lastName phone }
          tags
          lineItems(first: 10) { edges { node { title quantity variant { title } } } }
        }}
      }
    }"""
    # Fetch recent unfulfilled orders
    result = shopify_gql(q, {"q": "fulfillment_status:unfulfilled"}, token=token)
    if result.get("errors"):
        return []
    data  = result.get("data") or {}
    nodes = [e["node"] for e in (data.get("orders") or {}).get("edges", [])]
    # Keep only orders with NO Buunto date tag AND valid payment status
    IGNORED_STATUSES = {"VOIDED", "REFUNDED"}
    untagged = []
    for o in nodes:
        if o.get("displayFinancialStatus") in IGNORED_STATUSES:
            continue
        tags = o.get("tags") or []
        has_date_tag = any(date_pattern.search(t) for t in tags)
        if not has_date_tag:
            o["_no_date_warning"] = True
            untagged.append(o)
    return untagged

def fulfill_order(order, token):
    """Mark order fulfilled using fulfillment_orders REST API."""
    gid      = order.get("id", "")
    order_id = gid.split("/")[-1]
    if not order_id:
        return False
    # Step 1: Get open fulfillment orders
    url  = "https://{}/admin/api/{}/orders/{}/fulfillment_orders.json".format(
        SHOPIFY_STORE, SHOPIFY_API_VER, order_id)
    resp = requests.get(url, headers={"X-Shopify-Access-Token": token}, timeout=15)
    if not resp.ok:
        return False
    fo_list  = resp.json().get("fulfillment_orders", [])
    open_fos = [fo["id"] for fo in fo_list if fo.get("status") == "open"]
    if not open_fos:
        return False
    # Step 2: Create fulfillment
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

# ── Delivery group messages (Arabic) ─────────────────────────────────────────
def arabic_items(order):
    out = ""
    for e in order.get("lineItems", {}).get("edges", []):
        n   = e["node"]
        ar  = map_product(n.get("title", ""))
        sz  = map_size((n.get("variant") or {}).get("title", ""))
        out += "\n- {}x {} {}".format(n.get("quantity", 1), ar, sz).rstrip()
    return out

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
    # Warning line for orders with no Buunto delivery date tag
    warning = ""
    if order.get("_no_date_warning"):
        warning = "\n‼️‼️‼️‼️ لا يوجد تاريخ تسليم الرجوع لمدام رغدة ‼️‼️‼️‼️"
    # Payment line
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
def fmt_cook(order, seq):
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
    return "Order #{}-{}\nCustomer: {}{}\nItems:\n{}".format(seq_str, num, name, warning, items_str)

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
def run_backup():
    """
    Backup message: manually triggered.
    Scans today's PAID + FULFILLED orders and sends
    the standard message to both groups with تم الدفع مسبقا.
    Does NOT change any fulfillment status in Shopify.
    """
    token   = get_shopify_token()
    today   = get_today_cairo()
    tag     = buunto_tag(today)
    orders  = fetch_orders(tag, token)

    # Only PAID + FULFILLED orders
    backup_orders = [
        o for o in orders
        if o.get("displayFulfillmentStatus") == "FULFILLED"
        and o.get("displayFinancialStatus") == "PAID"
    ]

    if not backup_orders:
        send_tg("لا يوجد أوردرات مدفوعة ومسلمة اليوم")
        send_cook("No paid & fulfilled orders found for today.")
        return {"tag": tag, "backup_sent": 0}

    # Send date header
    send_tg(arabic_date_header(today))
    send_cook("📦 Doja Cook — Backup Messages for {}".format(today.strftime("%a %d %b %Y")))

    # Send each order as standard message — fmt_pending always shows
    # تم الدفع مسبقا when displayFinancialStatus == PAID
    for i, o in enumerate(backup_orders, 1):
        send_tg(fmt_pending(o, i))
        send_cook(fmt_cook(o, i))

    return {"tag": tag, "backup_sent": len(backup_orders)}

def run_cron():
    token         = get_shopify_token()
    today         = get_today_cairo()       # actual current date (for header display)
    delivery_date = get_delivery_date()     # tomorrow — the date drivers deliver
    tag           = buunto_tag(delivery_date)
    orders        = fetch_orders(tag, token)
    pending  = [o for o in orders if o.get("displayFulfillmentStatus") != "FULFILLED"]
    done     = [o for o in orders if o.get("displayFulfillmentStatus") == "FULFILLED"]

    # Also fetch unfulfilled orders with no delivery date tag
    untagged = fetch_untagged_unfulfilled(token, tag)
    # Avoid duplicates (in case an order somehow appears in both)
    tagged_ids = {o["id"] for o in orders}
    untagged   = [o for o in untagged if o["id"] not in tagged_ids]

    all_pending = pending + untagged
    total_orders = orders or all_pending

    # Delivery group (Arabic)
    send_tg(arabic_date_header(delivery_date))
    if not total_orders:
        send_tg("مفيش توصيلات النهارده 🎉")
    else:
        for i, o in enumerate(all_pending, 1):
            send_tg(fmt_pending(o, i))
        for i, o in enumerate(done, len(all_pending) + 1):
            send_tg(fmt_fulfilled(o, i))

    # Cook group (English)
    send_cook("📦 Doja Cook — Orders for {}".format(
        delivery_date.strftime("%a %d %b %Y")))
    if not total_orders:
        send_cook("No orders today 🎉")
    else:
        for i, o in enumerate(all_pending, 1):
            send_cook(fmt_cook(o, i))

    # Mark all pending as fulfilled in Shopify
    for o in all_pending:
        fulfill_order(o, token)

    return {"delivery_date": tag, "pending": len(pending), "untagged": len(untagged), "fulfilled": len(done)}

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
            self._respond(200, {"status": "Doja Delivery Bot running", "trigger": "/api/cron", "backup": "/api/backup"})

    def do_POST(self):
        self.do_GET()

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def log_message(self, *a):
        pass
