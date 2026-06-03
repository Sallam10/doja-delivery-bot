# Doja Delivery Bot — Continuation Brief
Last updated: June 3, 2026

---

## What This Is
An automated daily delivery notification bot for **Doja Cookies** (d0d0ba.myshopify.com / dojacookies.com). Every night at 11:50 PM Cairo time, it reads tomorrow's Shopify orders and sends structured messages to two Telegram groups — Arabic to the delivery drivers, English to the kitchen. It also marks orders as fulfilled in Shopify.

---

## Stack
- **Language:** Python (single file: app.py)
- **Hosting:** Vercel (free Hobby plan)
- **Scheduler:** cron-job.org (external, reliable — NOT Vercel's built-in cron)
- **Message delivery:** Telegram Bot API
- **Transliteration:** Claude Haiku (claude-haiku-4-5-20251001)
- **Code storage:** GitHub (github.com/Sallam10/doja-delivery-bot)

---

## Where It Lives
| Resource | URL / Value |
|---|---|
| GitHub repo | github.com/Sallam10/doja-delivery-bot |
| Vercel project | vercel.com/ahmedsallam10-4430s-projects/doja-delivery-bot |
| cron-job.org job | console.cron-job.org/jobs/7512323 |
| Cron schedule | 50 23 * * * (11:50 PM Cairo, Africa/Cairo timezone) |

---

## Secret Names (never values — get from Vercel env vars)
- ANTHROPIC_API_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID (توصيل Doja drivers group)
- TELEGRAM_COOK_CHAT_ID (Doja Cook kitchen group)
- TELEGRAM_RAGHDA_CHAT_ID (Raghda's personal Telegram — for pickup alerts)
- SHOPIFY_CLIENT_ID
- SHOPIFY_CLIENT_SECRET

---

## File Map
```
app.py          — all logic (single file, ~600 lines)
vercel.json     — URL rewrites for /api/cron, /api/backup, /api/today, /api/webhook
requirements.txt — anthropic, requests
CONTINUATION.md — this file
```

---

## Manual Trigger URLs
All require bypass token as query param: `?x-vercel-protection-bypass=s4h4oQUBcX6jO7AwiwWbqBbF4XFOZeih`

| Endpoint | When to use |
|---|---|
| /api/cron | Run the full nightly job manually (checks tomorrow's orders) |
| /api/today | Order placed after 11:50 PM, now past midnight — checks today's orders |
| /api/backup | Paid + fulfilled orders missed — sends messages without touching Shopify |
| /api/webhook | Receives Shopify order/created events for same-day pickup alerts |

---

## What's Done

### Core nightly automation
- Runs at 11:50 PM Cairo via cron-job.org
- Fetches tomorrow's Shopify orders via GraphQL
- Classifies each order (delivery / pickup / no-date / fulfilled)
- Sends Arabic messages to توصيل Doja (drivers)
- Sends English messages to Doja Cook (kitchen)
- Marks all pending orders as fulfilled in Shopify

### Order logic rules
1. VOIDED payment → ignored
2. REFUNDED payment → ignored
3. PENDING or PAID + unfulfilled + tomorrow's date + delivery → standard Arabic message
4. PENDING or PAID + unfulfilled + tomorrow's date + pickup → Cook group only (🏪 PICKUP ORDER)
5. PENDING or PAID + unfulfilled + no date → standard + Raghda warning both groups
6. PENDING or PAID + fulfilled + tomorrow's date + delivery → checkmarks message
7. PAID → shows تم الدفع مسبقا instead of collection amount

### Bilingual order date detection (priority order)
1. customAttributes.delivery_date (new custom theme) — key: `delivery_date`, ISO format `2026-06-04`
2. customAttributes.delivery_choice — key: `delivery_choice`, values: `east_cairo_delivery` / `west_cairo_delivery` / `pickup_cairo`
3. Fallback: order.tags Buunto format — `"Wed Apr 30 2026"` (old theme, still works permanently)

### Shopify webhook (same-day pickup alerts)
- Registered in Shopify admin: Settings → Notifications → Webhooks → Order creation
- Fires when any new order is placed
- Code checks: is it pickup? is delivery_date == today?
- If yes → sends 🚨🚨🚨 ALERT to Cook group AND to Raghda's personal Telegram
- If no → ignores (nightly cron handles it)
- NOTE: Shopify admin UI webhooks can have delays — still being validated

### Manual endpoints
- /api/today — for orders placed after 11:50 PM when it's past midnight
- /api/backup — sends paid+fulfilled orders without changing Shopify
- /api/webhook — Shopify webhook receiver (same-day pickup only)

### Phone number format
- Format: +20XXXXXXXXXX with \u200e LTR mark before + so it displays left-to-right in Telegram

### Transliteration fix (May 2026)
- Claude Haiku prompt was causing conversational responses instead of numbered list
- Fixed with clearer prompt + safety check: if response doesn't match numbered list format, return original English text gracefully

---

## Change / Deploy Workflow
```
Edit app.py in Claude's workspace
        ↓ git push to github.com/Sallam10/doja-delivery-bot
Vercel auto-detects push → rebuilds → deploys in ~30 seconds
        ↓
Live bot updated
```
GitHub token needed to push: generate at github.com/settings/tokens (repo scope, no expiry)
Last working token stored in session — may have expired, generate fresh if push fails.

---

## Open Items
- [ ] Validate Shopify webhook actually fires for same-day pickup (test with delivery_date == today)
- [ ] Shopify webhook currently created via admin UI (Settings → Notifications → Webhooks) — consider migrating to API-created webhook for better reliability
- [ ] Add 2 more Shopify stores to the bot (only d0d0ba connected now)
- [ ] DeliveryBot SaaS project in separate repo: github.com/Sallam10/deliverybot (see Apple Note: 🚀 Doja Bot SaaS — Full Product Blueprint)

---

## Gotchas
- **Vercel built-in cron is unreliable** — we removed it. cron-job.org is the only scheduler.
- **Env vars need redeploy** — adding a new Vercel env var only takes effect after the next deployment. Always push an empty commit after adding a new env var.
- **Egypt clock change (UTC+3 summer)** — cron-job.org uses Africa/Cairo timezone which handles this automatically. Code uses ZoneInfo("Africa/Cairo") for the same reason. 11:50 PM was chosen specifically because midnight is risky during clock-change nights.
- **Sacred theme** — dojacookies.com live theme must never be edited directly. This is unrelated to the bot but important context.
- **Shopify GraphQL field names** — note_attributes in REST = customAttributes in GraphQL. Key/value in GraphQL, name/value in REST.
- **Raghda Khater** — internal staff member (order manager). Her orders sometimes appear as untagged unfulfilled — code handles this with the no-date warning. Her personal Telegram ID is stored in TELEGRAM_RAGHDA_CHAT_ID env var.
- **Double-run prevention** — do not trigger manual URL on the same night cron-job.org runs. Will cause duplicate messages.
- **GitHub token** — only needed by Claude to push code. Bot runs on Vercel and doesn't use it. Expired token = can't push code, but bot keeps running fine.
