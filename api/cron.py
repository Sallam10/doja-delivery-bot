import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
import anthropic

# ── Config ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SHOPIFY_TOKEN      = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_STORE      = "d0d0ba.myshopify.com"
SHOPIFY_API_VER    = "2024-01"

# ── Cairo timezone (always UTC+2, Egypt stopped DST in 2011) ──────────────
CAIRO_TZ = timezone(timedelta(hours=2))

# ── Arabic day / month names ──────────────────────────────────────────────
AR_DAYS   = ["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
AR_MONTHS = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]

# ── English abbreviations for Buunto tag format ───────────────────────────
EN_DAYS   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
EN_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
             "Jul","Aug","Sep","Oct","Nov","Dec"]

# ── Product mappings ───────────────────────────────────────────────────────
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

# ── Transliteration prompt ─────────────────────────────────────────────────
ADDRESS_RULES = """
Common Egyptian address mappings to ALWAYS apply:
Street → شارع | Road → طريق | Compound → كمبوند | Villa → فيلا | Building/Bldg → بناية
Floor → دور | Apartment/Apt → شقة | Tower → برج | Gate → بوابة | Block → بلوك | Zone → زون
New Cairo → القاهرة الجديدة | 6th of October / 6 October → 6 أكتوبر | Sheikh Zayed → الشيخ زايد
Maadi → المعادي | Zamalek → الزمالك | Heliopolis → مصر الجديدة | Nasr City → مدينة نصر
Mohandessin → المهندسين | Dokki → الدقي | Giza → الجيزة | Cairo → القاهرة
North / South / East / West → شمال / جنوب / شرق / غرب
El / Al / the → ال (prefix, no space if followed by sun letter)
"""

NAME_EXAMPLES = """
Examples: "Rasha El rayes" → "راشا الريس" | "Youssef Katamish" → "يوسف قطامش"
"Mohamed" → "محمد" | "Ahmed" → "أحمد" | "Sarah" → "سارة" | "Omar" → "عمر"
"Nour" → "نور" | "Laila" → "ليلى" | "Karim" → "كريم" | "Hana" → "هنا"
"""


# ──────────────────────────────────────────────────────────────────────────
# Date helpers
# ──────────────────────────────────────────────────────────────────────────

def get_today_cairo():
    return datetime.now(CAIRO_TZ)

def buunto_tag(dt):
    """Format: 'Tue Apr 22 2025' — exactly what Buunto writes on orders"""
    return f"{EN_DAYS[dt.weekday()]} {EN_MONTHS[dt.month-1]} {dt.day} {dt.year}"

def arabic_date_header(dt):
    ar_day   = AR_DAYS[dt.weekday()]
    ar_month = AR_MONTHS[dt.month - 1]
    return f"📦 أوردرات يوم {ar_day} {dt.day} {ar_month} {dt.year}"


# ──────────────────────────────────────────────────────────────────────────
# Shopify API
# ──────────────────────────────────────────────────────────────────────────

def shopify_graphql(query, variables=None):
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VER}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_orders_by_tag(tag):
    """Fetch all orders tagged with today's Buunto tag via GraphQL."""
    query = """
    query($queryStr: String!) {
      orders(first: 50, query: $queryStr) {
        edges {
          node {
            id
            name
            displayFulfillmentStatus
            phone
            totalPriceSet { shopMoney { amount } }
            shippingAddress { name phone address1 address2 city }
            customer { firstName lastName phone }
            lineItems(first: 10) {
              edges {
                node {
                  title
                  quantity
                  variant { title }
                }
              }
            }
            fulfillmentOrders(first: 5) {
              edges {
                node { id status }
              }
            }
          }
        }
      }
    }
    """
    escaped = tag.replace('"', '\\"')
    result  = shopify_graphql(query, {"queryStr": f'tag:"{escaped}"'})
    edges   = result.get("data", {}).get("orders", {}).get("edges", [])
    return [e["node"] for e in edges]


def fulfill_order(order):
    """Mark a pending order as fulfilled via Shopify GraphQL mutation."""
    fo_edges = order.get("fulfillmentOrders", {}).get("edges", [])
    open_fo  = next(
        (e["node"]["id"] for e in fo_edges if e["node"]["status"] == "OPEN"),
        None
    )
    if not open_fo:
        return False

    mutation = """
    mutation fulfillmentCreateV2($fulfillment: FulfillmentV2Input!) {
      fulfillmentCreateV2(fulfillment: $fulfillment) {
        fulfillment { id status }
        userErrors { field message }
      }
    }
    """
    variables = {
        "fulfillment": {
            "lineItemsByFulfillmentOrder": [
                {"fulfillmentOrderId": open_fo}
            ]
        }
    }
    result = shopify_graphql(mutation, variables)
    errors = result.get("data", {}).get("fulfillmentCreateV2", {}).get("userErrors", [])
    return len(errors) == 0


# ──────────────────────────────────────────────────────────────────────────
# Transliteration
# ──────────────────────────────────────────────────────────────────────────

def has_latin(text):
    return bool(re.search(r"[a-zA-Z]", text or ""))


def transliterate_batch(texts):
    latin_indices = [i for i, t in enumerate(texts) if has_latin(t)]
    if not latin_indices:
        return texts

    numbered = "\n".join(f"{n+1}. {texts[i]}" for n, i in enumerate(latin_indices))
    prompt = f"""You are helping Egyptian delivery drivers read customer names and addresses aloud.
Transliterate ONLY the English/Latin parts of each item to Arabic phonetic spelling.
Keep all Arabic text exactly as-is. Keep numbers and punctuation (commas, dashes, dots, /) as-is.

{ADDRESS_RULES}

For names: transliterate phonetically to how an Egyptian would say them.
{NAME_EXAMPLES}

Return ONLY a numbered list — same numbers, one result per line, nothing else:
{numbered}"""

    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    lines = [
        re.sub(r"^\d+\.\s*", "", line).strip()
        for line in response.content[0].text.strip().split("\n")
        if line.strip()
    ]
    result = list(texts)
    for n, orig_idx in enumerate(latin_indices):
        if n < len(lines):
            result[orig_idx] = lines[n]
    return result


# ──────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────

def format_phone(raw):
    if not raw:
        return "غير متوفر"
    phone = re.sub(r"[\s\-\(\)\.]", "", raw)
    if phone.startswith("+20"):
        phone = phone[3:]
    elif phone.startswith("0020"):
        phone = phone[4:]
    elif phone.startswith("20") and len(phone) > 10:
        phone = phone[2:]
    elif phone.startswith("0"):
        phone = phone[1:]
    return f"\u200e+20 {phone}"


def map_product(title):
    lower = title.lower()
    for pattern, arabic in PRODUCT_MAPPINGS:
        if re.search(pattern, lower):
            return arabic
    return title


def map_size(variant_title):
    if not variant_title or variant_title == "Default Title":
        return ""
    m = re.search(r"\b(6|12|24|48)\b", variant_title)
    if m:
        return SIZE_MAPPINGS.get(m.group(1), "")
    return f"({variant_title})"


def extract_order_number(name):
    """'#1023' → '1023', '#D0D0BA-1023' → '1023'"""
    m = re.search(r"[-#](\d+)$", name)
    return m.group(1) if m else name.lstrip("#")


def build_items_text(order):
    items_text = ""
    for edge in order.get("lineItems", {}).get("edges", []):
        item     = edge["node"]
        qty      = item.get("quantity", 1)
        ar_name  = map_product(item.get("title", ""))
        ar_size  = map_size((item.get("variant") or {}).get("title", ""))
        items_text += f"\n- {qty}x {ar_name} {ar_size}".rstrip()
    return items_text


def format_pending_message(order, seq):
    shipping  = order.get("shippingAddress") or {}
    customer  = order.get("customer") or {}
    raw_name  = shipping.get("name") or f"{customer.get('firstName','')} {customer.get('lastName','')}".strip()
    raw_addr1 = shipping.get("address1", "")
    raw_addr2 = shipping.get("address2", "")
    raw_city  = shipping.get("city", "")
    phone_raw = shipping.get("phone") or order.get("phone") or customer.get("phone") or ""

    ar_name, ar_addr1, ar_addr2, ar_city = transliterate_batch([raw_name, raw_addr1, raw_addr2, raw_city])

    addr_parts = [p for p in [ar_addr1, ar_addr2, ar_city] if p]
    address    = "، ".join(addr_parts)
    phone      = format_phone(phone_raw)

    total_raw  = order.get("totalPriceSet", {}).get("shopMoney", {}).get("amount", "0")
    total      = str(round(float(total_raw)))
    order_num  = extract_order_number(order.get("name", ""))
    seq_str    = str(seq).zfill(3)
    items_text = build_items_text(order)

    return (
        f"🛵 أوردر رقم: {seq_str}-{order_num}\n"
        f"👤 اسم العميل: {ar_name}\n"
        f"📞 رقم التليفون: {phone}\n"
        f"📍 العنوان: {address}\n"
        f"🛍️ الطلبات:{items_text}\n"
        f"💰 المبلغ المطلوب تحصيله: {total} جنيه"
    )


def format_fulfilled_message(order, seq):
    shipping  = order.get("shippingAddress") or {}
    customer  = order.get("customer") or {}
    raw_name  = shipping.get("name") or f"{customer.get('firstName','')} {customer.get('lastName','')}".strip()
    ar_name   = transliterate_batch([raw_name])[0]
    order_num = extract_order_number(order.get("name", ""))
    seq_str   = str(seq).zfill(3)
    items_text = build_items_text(order)

    return (
        f"✅✅✅✅ تم التسليم مسبقا ✅✅✅✅\n"
        f"🛵 أوردر رقم: {seq_str}-{order_num}\n"
        f"👤 اسم العميل: {ar_name}\n"
        f"🛍️ الطلبات:{items_text}\n"
        f"✅✅✅✅ تم التسليم مسبقا ✅✅✅✅"
    )


# ──────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }, timeout=10)
    return resp.ok


# ──────────────────────────────────────────────────────────────────────────
# Main job
# ──────────────────────────────────────────────────────────────────────────

def run_daily_job():
    today   = get_today_cairo()
    tag     = buunto_tag(today)
    header  = arabic_date_header(today)

    # 1. Send date header
    send_telegram(header)

    # 2. Fetch today's orders
    orders    = fetch_orders_by_tag(tag)
    pending   = [o for o in orders if o.get("displayFulfillmentStatus") != "FULFILLED"]
    fulfilled = [o for o in orders if o.get("displayFulfillmentStatus") == "FULFILLED"]

    # 3. No orders today
    if not orders:
        send_telegram("مفيش توصيلات النهارده 🎉")
        return {"pending": 0, "fulfilled": 0}

    # 4. Send each pending order as individual message
    for i, order in enumerate(pending, 1):
        msg = format_pending_message(order, i)
        send_telegram(msg)

    # 5. Send each fulfilled order as individual message (continuing sequence)
    offset = len(pending)
    for i, order in enumerate(fulfilled, offset + 1):
        msg = format_fulfilled_message(order, i)
        send_telegram(msg)

    # 6. Mark all pending orders as fulfilled in Shopify
    for order in pending:
        fulfill_order(order)

    return {"pending": len(pending), "fulfilled": len(fulfilled), "tag": tag}


# ──────────────────────────────────────────────────────────────────────────
# Vercel handler (GET = cron trigger, POST = manual trigger)
# ──────────────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        try:
            result = run_daily_job()
            self._respond(200, result)
        except Exception as e:
            print(f"Cron error: {e}")
            self._respond(500, {"error": str(e)})

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        pass
