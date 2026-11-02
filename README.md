# ChartMogul × SFDC Product Dashboard

Nightly pull of ChartMogul customers + subscriptions, joined read-only to Salesforce
Accounts, Contacts and closed-won Opportunities, served as a web dashboard on Railway.
Built for Product Marketing: **which customers have which products, what they pay, what
they're contracted for, and who the active users are (with contact info).**

## Status (2026-11-02)

Railway project `product-usage-dashboard` (Healthie RevOps workspace) exists with Postgres,
`web` and `sync` services. `web` is live at https://web-production-fac10.up.railway.app
(Basic auth). **`sync` is not deployed**
because it is waiting on SFDC + ChartMogul credentials, so the dashboard is empty until then.

| # | Requirement | Status | Source |
|---|---|---|---|
| 1 | Products + product usage | Products ✅ / usage ⚠️ partial | ChartMogul plans; SFDC Account usage fields |
| 2 | Which products each customer uses | ✅ `/products` → `/product?plan=…`, `/customer/…` | ChartMogul subscriptions |
| 3 | Revenue | ✅ MRR + ARR per product and per customer | ChartMogul |
| 4 | Key usage metrics | ⚠️ active users, active clients, providers, health score; no feature-level usage | SFDC Account (Vitally-fed) |
| 5 | Contacts/users actively using the product | ✅ at the account level (`Healthie_User_Active__c`) | SFDC Contact |
| 6 | Names, emails, phone for those users | ✅ on page + CSV export (`/product/users.csv?plan=…`) | SFDC Contact |
| 7 | Contract values | ✅ latest closed-won TCV / Year-1 ACV, contract start/end | SFDC Opportunity |

### Blockers to a live URL
1. **SFDC Connected App** (client-credentials, read-only integration user). Still open with Aaron / Edgar.
2. ~~ChartMogul API key~~: done. The read-only key from `healthie-bottoms-up-model` is set on `sync` (verified 2026-11-02).
3. Once 1 is done, set `SFDC_CLIENT_ID` and `SFDC_CLIENT_SECRET` on `sync`,
   run `railway up -s sync`, and set its cron schedule in the UI.
4. `DASH_USER`/`DASH_PASS` are set on `web`. View them with `railway variables -s web`.
5. **The GitHub repo is public.** No secrets are committed, but consider making it private.

### Data quality, checked in prod 2026-11-02
- `Contact.Healthie_User_Active__c`: populated (~514k active contacts org-wide). Used as the "active user" signal.
- `Contact.Healthie_User_Last_Active__c`: **stale**. Latest value is 2024-06-04, so it is not shown as a recency signal.
- `Account.Vitally_Platform_Feature_Usage__c`: **empty** on every account with a Stripe ID, so there is no feature-level usage.
- `Account.No_Users_Active_last_6M__c`: always 0, not used.
- `Account.Active_Standard_Users_Account__c`: ~9.6k of 59k Stripe-linked accounts. `Healthie_Active_Clients_Account__c`: only ~300.
- Opportunity contract fields: TCV is on ~7k won opps; Year-1 ACV ~1.4k; contract end date ~950.
  "Contract value" = TCV, else Year-1 ACV, else Amount, from the most recent won opp.

**Usage caveat:** "active users on product X" means active Healthie users at accounts
that pay for X. It does not mean those people use feature X. True per-product usage
needs event data from the product DB, warehouse, or Vitally directly. That source is not
connected yet.

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
3. Create **two services from the same repo**, named exactly `web` and `sync`.
   Both start with `python start.py` (from `Procfile` + the `RAILPACK_START_CMD` service
   variable; Railway ignored `railway.json` on CLI uploads). `start.py` picks its role from
   `RAILWAY_SERVICE_NAME` (override with `SERVICE_ROLE`):
   - `web` serves the dashboard. Generate a public domain for it.
   - `sync` runs once and exits. In the dashboard, set Service → Settings → Cron Schedule
     to `0 9 * * *` (09:00 UTC ≈ 4/5am ET, after ChartMogul's own Stripe import settles).
     The CLI's `environment edit --service-config` did not apply this (v4.44), so set it in the UI.
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

## Pages

- `/products`: every plan with active customers, seats, MRR, ARR, active users, CSV link
- `/product?plan=…`: customers on that product (product MRR, total MRR, contract value/end,
  usage, health, owner) + active users with name/email/phone
- `/product/users.csv?plan=…`: full active-user export for that product
- `/customer/{cm_uuid}`: products, usage figures, contracts, contacts with active-user flag
- `/`: customer list with search / product / status / match filters

## JSON API (for other tooling / Claude skills)

- `GET /api/products` — plan-level rollup: customers + MRR per product
- `GET /api/account/{sfdc_account_id}` — full product/pricing/contact picture for one account

The second endpoint is the interesting one long-term: any skill or script that has an
SFDC Account ID can ask this service "what does this account pay for" without touching
ChartMogul or burning SFDC API calls.
