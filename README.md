# ChartMogul × SFDC Product Dashboard

Nightly pull of ChartMogul customers + subscriptions, joined read-only to Salesforce
Accounts and Contacts, served as a web dashboard on Railway. Answers: **which customers
have which products, at what price, and who owns them in SFDC.**

## Architecture

```
ChartMogul API ─┐
                ├─→ Railway cron (python -m sync.run_sync) ─→ Railway Postgres ─→ FastAPI web
SFDC REST API ──┘         (daily)                                                 (dashboard + JSON API)
```

Design decisions (deliberate — don't undo casually):

- **SFDC is read-only here.** The org is at storage capacity and product/pricing detail
  is not commercial state that belongs in SFDC. We join externally; nothing is written back.
- **Match waterfall:** ChartMogul `external_id` (Stripe `cus_...`) → `Account.Stripe Customer ID`,
  fallback Healthie Org ID (as a ChartMogul custom attribute) → `Account.Healthie Org ID`.
  Unmatched customers are surfaced at `/?match=unmatched`, never dropped.
- **Full refresh daily.** Volumes are small enough that truncate-and-reload beats
  incremental sync complexity. Revisit if the daily run exceeds ~20 min.

## Deploy on Railway

1. Push this repo to GitHub, create a Railway project from it.
2. Add a **Postgres** database to the project (`DATABASE_URL` is injected automatically).
3. Create **two services from the same repo**:
   - `web` — uses `railway.json` as-is (uvicorn start command, `/health` healthcheck).
   - `sync` — set start command `python -m sync.run_sync` and a cron schedule
     (Service → Settings → Cron Schedule), e.g. `0 9 * * *` (09:00 UTC ≈ 4/5am ET,
     after ChartMogul's own Stripe import settles).
4. Set env vars on **both** services (see `.env.example`).
5. Trigger the sync service once manually, then open the web service URL.
6. Web service → Settings → Networking → generate the public domain. Consider
   restricting access (Railway has no built-in auth — see Auth below).

## Dependencies before first successful run

| Dependency | Owner | Detail |
|---|---|---|
| Connected App, client-credentials flow | Aaron / Edgar | Read-only integration user as "Run As"; scopes: `api`. Do **not** reuse the Claude MCP permission set — this needs its own read-only profile. |
| Field API names | You (one describe call) | `sf sobject describe -s Account \| grep -i "healthie_org\|stripe_customer"` — update `SFDC_ACCOUNT_*_FIELD` env vars if the defaults are wrong. |
| ChartMogul API key | You | Profile → API keys. Read-only key if available. |
| Org ID custom attribute in ChartMogul | Existing workstream | The already-planned segment/org-id attribute push improves the fallback match rate. Not blocking — Stripe ID covers most. |

## Auth for the dashboard

Railway exposes a public URL with no auth. Options, in order of effort:
1. Keep the URL unguessable + no indexing (fine for a pilot, not for PHI-adjacent data —
   note this is billing/product data only, no patient data flows here).
2. Add HTTP Basic auth middleware in `app/main.py` (10 lines, `DASH_USER`/`DASH_PASS` env vars).
3. Put it behind Google OAuth restricted to @gethealthie.com (fastapi + authlib).

Recommendation: do (2) before sharing the link beyond RevOps.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python -m sync.run_sync          # populates local.db (SQLite fallback)
uvicorn app.main:app --reload
```

## JSON API (for other tooling / Claude skills)

- `GET /api/products` — plan-level rollup: customers + MRR per product
- `GET /api/account/{sfdc_account_id}` — full product/pricing/contact picture for one account

The second endpoint is the interesting one long-term: any skill or script that has an
SFDC Account ID can ask this service "what does this account pay for" without touching
ChartMogul or burning SFDC API calls.
