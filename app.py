import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
import anthropic

# ── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SHOPIFY_CLIENT_ID  = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_STORE      = "d0d0ba.myshopify.com"
SHOPIFY_API_VER    = "2024-01"
CAIRO_TZ           = timezone(timedelta(hours=2))

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
SIZE_MAPPINGS = {"6":"(علبة 6 قطعة)","12":"(علبة 12 قطعة)",
                 "24":"(علبة 24 قطعة)","48":"(علبة 48 قطعة)"}

ADDRESS_RULES = """
Street → شارع | Road → طريق | Compound → كمبوند | Villa → فيلا | Building/Bldg → بناية
Floor → دور | Apartment/Apt → شقة | Tower → برج | Gate → بوابة | Block → بلوك | Zone → زون
New Cairo → القاهرة الجديدة | 6th of October / 6 October → 6 أكتوبر | Sheikh Zayed → الشيخ زايد
Maadi → المعادي | Zamalek → الزمالك | Heliopolis → مصر الجديدة | Nasr City → مدينة نصر
Mohandessin → المهندسين | Dokki → الدقي | Giza → الجيزة | Cairo → القاهرة
North / South / East / West → شمال / جنوب / شرق / غرب
El / Al / the → ال (prefix, no space if followed by sun letter)
"""
NAME_EXAMPLES = """
"Rasha El rayes"→"راشا الريس" | "Mohamed"→"محمد" | "Ahmed"→"أحمد"
"Sarah"→"سارة" | "Omar"→"عمر" | "Nour"→"نور" | "Laila"→"ليلى"
"""

# ── Helpers ─────────────────────────────────────────────────────────────────

def get_today_cairo():
    return datetime.now(CAIRO_TZ)

def buunto_tag(dt):
    return f"{EN_DAYS[dt.weekday()]} {EN_MONTHS[dt.month-1]} {dt.day} {dt.year}"

def arabic_date_header(dt):
    return f"📦 أوردرات يوم {AR_DAYS[dt.weekday()]} {dt.day} {AR_MONTHS[dt.month-1]} {dt.year}"

def get_shopify_token():
    """Exchange client credentials for a fresh OAuth access token (valid 24h)."""
    resp = requests.post(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "client_credentials",
            "client_id":     SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def shopify_gql(query, variables=None, token=None):
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VER}/graphql.json"
    resp = requests.post(url, json={"query": query, **({"variables": variables} if variables else {})},
                         headers={"X-Shopify-Access-Token": token,
                                  "Content-Type": "application/json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()

def fetch_orders(tag, token):
    q = """
    query($q: String!) {
      orders(first: 50, query: $q) {
        edges { node {
          id name displayFulfillmentStatus phone
          totalPriceSet { shopMoney { amount } }
          shippingAddress { name phone address1 address2 city }
          customer { firstName lastName phone }
          lineItems(first: 10) { edges { node { title quantity variant { title } } } }
          fulfillmentOrders(first: 5) { edges { node { id status } } }
        }}
      }
    }"""
    result = shopify_gql(q, {"q": f'tag:"{tag}"'}, token=token)
    return [e["node"] for e in result.get("data",{}).get("orders",{}).get("edges",[])]

def fulfill_order(order, token):
    fo_edges = order.get("fulfillmentOrders",{}).get("edges",[])
    open_fo  = next((e["node"]["id"] for e in fo_edges if e["node"]["status"]=="OPEN"), None)
    if not open_fo:
        return False
    mut = """
    mutation fulfillmentCreateV2($f: FulfillmentV2Input!) {
      fulfillmentCreateV2(fulfillment: $f) {
        fulfillment { id status }
        userErrors { field message }
      }
    }"""
    r = shopify_gql(mut, {"f": {"lineItemsByFulfillmentOrder":[{"fulfillmentOrderId": open_fo}]}}, token=token)
    return len(r.get("data",{}).get("fulfillmentCreateV2",{}).get("userErrors",[])) == 0

def has_latin(t):
    return bool(re.search(r"[a-zA-Z]", t or ""))

def transliterate_batch(texts):
    idx = [i for i,t in enumerate(texts) if has_latin(t)]
    if not idx:
        return texts
    numbered = "\n".join(f"{n+1}. {texts[i]}" for n,i in enumerate(idx))
    prompt = f"""You are helping Egyptian delivery drivers read customer names and addresses aloud.
Transliterate ONLY the English/Latin parts to Arabic phonetic spelling.
Keep Arabic text, numbers and punctuation exactly as-is.
{ADDRESS_RULES}
For names: transliterate phonetically to how an Egyptian would say them.
{NAME_EXAMPLES}
Return ONLY a numbered list, one result per line:
{numbered}"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp   = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=1500,
                                    messages=[{"role":"user","content":prompt}])
    lines  = [re.sub(r"^\d+\.\s*","",l).strip()
              for l in resp.content[0].text.strip().split("\n") if l.strip()]
    result = list(texts)
    for n,oi in enumerate(idx):
        if n < len(lines):
            result[oi] = lines[n]
    return result

def fmt_phone(raw):
    if not raw: return "غير متوفر"
    p = re.sub(r"[\s\-\(\)\.]","", raw)
    for prefix in ("+20","0020"):
        if p.startswith(prefix): p = p[len(prefix):]; break
    else:
        if p.startswith("20") and len(p)>10: p = p[2:]
        elif p.startswith("0"): p = p[1:]
    return f"\u200e+20 {p}"

def map_product(title):
    lo = title.lower()
    for pat,ar in PRODUCT_MAPPINGS:
        if re.search(pat, lo): return ar
    return title

def map_size(vt):
    if not vt or vt=="Default Title": return ""
    m = re.search(r"\b(6|12|24|48)\b", vt)
    return SIZE_MAPPINGS.get(m.group(1),"") if m else f"({vt})"

def order_num(name):
    m = re.search(r"[-#](\d+)$", name)
    return m.group(1) if m else name.lstrip("#")

def items_text(order):
    out = ""
    for e in order.get("lineItems",{}).get("edges",[]):
        n = e["node"]
        out += f"\n- {n.get('quantity',1)}x {map_product(n.get('title',''))} {map_size((n.get('variant') or {}).get('title',''))}".rstrip()
    return out

def fmt_pending(order, seq):
    sh = order.get("shippingAddress") or {}
    cu = order.get("customer") or {}
    rn  = sh.get("name") or f"{cu.get('firstName','')} {cu.get('lastName','')}".strip()
    a1,a2,ci = sh.get("address1",""), sh.get("address2",""), sh.get("city","")
    an,aa1,aa2,aci = transliterate_batch([rn,a1,a2,ci])
    addr  = "، ".join(p for p in [aa1,aa2,aci] if p)
    phone = fmt_phone(sh.get("phone") or order.get("phone") or cu.get("phone",""))
    total = str(round(float(order.get("totalPriceSet",{}).get("shopMoney",{}).get("amount","0"))))
    return (f"🛵 أوردر رقم: {str(seq).zfill(3)}-{order_num(order.get('name',''))}\n"
            f"👤 اسم العميل: {an}\n"
            f"📞 رقم التليفون: {phone}\n"
            f"📍 العنوان: {addr}\n"
            f"🛍️ الطلبات:{items_text(order)}\n"
            f"💰 المبلغ المطلوب تحصيله: {total} جنيه")

def fmt_fulfilled(order, seq):
    sh = order.get("shippingAddress") or {}
    cu = order.get("customer") or {}
    rn = sh.get("name") or f"{cu.get('firstName','')} {cu.get('lastName','')}".strip()
    an = transliterate_batch([rn])[0]
    return (f"✅✅✅✅ تم التسليم مسبقا ✅✅✅✅\n"
            f"🛵 أوردر رقم: {str(seq).zfill(3)}-{order_num(order.get('name',''))}\n"
            f"👤 اسم العميل: {an}\n"
            f"🛍️ الطلبات:{items_text(order)}\n"
            f"✅✅✅✅ تم التسليم مسبقا ✅✅✅✅")

def send_tg(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)

def run_cron():
    token   = get_shopify_token()          # fresh OAuth token, valid 24h
    today   = get_today_cairo()
    tag     = buunto_tag(today)
    orders  = fetch_orders(tag, token)
    pending = [o for o in orders if o.get("displayFulfillmentStatus") != "FULFILLED"]
    done    = [o for o in orders if o.get("displayFulfillmentStatus") == "FULFILLED"]

    send_tg(arabic_date_header(today))

    if not orders:
        send_tg("مفيش توصيلات النهارده 🎉")
        return {"tag": tag, "pending": 0, "fulfilled": 0}

    for i,o in enumerate(pending, 1):
        send_tg(fmt_pending(o, i))

    for i,o in enumerate(done, len(pending)+1):
        send_tg(fmt_fulfilled(o, i))

    for o in pending:
        fulfill_order(o, token)

    return {"tag": tag, "pending": len(pending), "fulfilled": len(done)}

# ── Vercel handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/cron":
            try:
                result = run_cron()
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(200, {"status": "Doja Delivery Bot ✅", "trigger": "/api/cron"})

    def do_POST(self):
        self.do_GET()

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def log_message(self, *a):
        pass
