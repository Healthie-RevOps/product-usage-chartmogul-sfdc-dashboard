# ChartMogul × SFDC Product Dashboard

Nightly pull of ChartMogul customers + subscriptions, joined read-only to Salesforce
Accounts, Contacts and closed-won Opportunities, served as a web dashboard on Railway.
Built for Product Marketing: **which customers have which products, what they pay, what
they're contracted for, and who the active users are (with contact info).**

## Status (2026-11-02)

Railway project `product-usage-dashboard` (Healthie RevOps workspace) exists with Postgres,
`web` and `sync` services. `web` is live at https://product-usage.up.railway.app behind
Google sign-in (@gethealthie.com). `sync` runs nightly at 09:00 UTC with ChartMogul +
SFDC credentials.

| # | Requirement | Status | Source |
|---|---|---|---|
| 1 | Products + product usage | Products ✅ / usage ⚠️ partial | ChartMogul plans; SFDC Account usage fields |
| 2 | Which products each customer uses | ✅ `/products` → `/product?product=…`, `/customer/…` | ChartMogul subscriptions |
| 3 | Revenue | ✅ MRR + ARR per product and per customer | ChartMogul |
| 4 | Key usage metrics | ⚠️ active users, active clients, providers, health score; no feature-level usage | SFDC Account (Vitally-fed) |
| 5 | Contacts/users actively using the product | ✅ at the account level (`Healthie_User_Active__c`) | SFDC Contact |
| 6 | Names, emails, phone for those users | ✅ on page + CSV export (`/product/users.csv?product=…`) | SFDC Contact |
| 7 | Contract values | ✅ latest closed-won TCV / Year-1 ACV, contract start/end | SFDC Opportunity |

### Open items
1. **SFDC auth is interim.** `sync` logs in with the shared `vivek.marthi+netlify@gethealthie.com`
   username/password/token (SOAP login), copied from `healthie-bottoms-up-model`. The code
   only writes the five Accounts-page reference fields (when `SFDC_EDITORS` is set), but that user is not a dedicated integration user. When the
   Connected App exists, set `SFDC_CLIENT_ID`/`SFDC_CLIENT_SECRET` on `sync`. Client
   credentials take priority automatically, then delete the `SFDC_USERNAME`/`PASSWORD`/
   `SECURITY_TOKEN` vars. That user can't see `Product_Adoption_Score__c`. The sync skips
   hidden optional fields with a warning.
2. ~~ChartMogul API key~~: done. The read-only key from `healthie-bottoms-up-model` is set on `sync` (verified 2026-11-02).
3. **Move the `sync` cron to `0 12 * * *`** in the Railway UI (09:00 UTC runs fail on ChartMogul; see Deploy).
4. `DASH_USER`/`DASH_PASS` on `web` now only gate `/api/*` (machine access).
5. **The GitHub repo is public.** No secrets are committed, but consider making it private.

### Data quality, checked in prod 2026-11-02
- `Contact.Healthie_User_Active__c`: populated (~514k active contacts org-wide). Used as the "active user" signal.
- `Contact.Healthie_User_Last_Active__c`: **stale**. Latest value is 2024-06-04, so it is not shown as a recency signal.
- `Account.Vitally_Platform_Feature_Usage__c`: **empty** on every account with a Stripe ID, so there is no feature-level usage.
- `Account.No_Users_Active_last_6M__c`: always 0, not used.
- `Account.Active_Standard_Users_Account__c`: ~9.6k of 59k Stripe-linked accounts. `Healthie_Active_Clients_Account__c`: only ~300.
- Opportunity contract fields: TCV is on ~7k won opps; Year-1 ACV ~1.4k; contract end date ~950.
  "Contract value" = TCV, else Year-1 ACV, else Amount, from the most recent won opp.

### Salesforce opportunities and Enterprise attributes (added 2026-11-08)
Synced to replace a CS teammate's Salesforce reports (Enterprise Customer Attribute
Reporting; Scribe/Bridge/Vis+ open opps; Add-On Closed Lost; Competitor Field Opportunities):
- `sfdc_opportunities`: **every** open opp plus every opp closed since
  `SFDC_OPPS_CLOSED_SINCE` (default `2024-01-01`), any stage, record type, or account,
  including prospects with no ChartMogul match. Raw SFDC values; product detection and
  competitor normalization belong in the app.
- `sfdc_accounts` gains the Enterprise fields (plan, status, SFDC MRR, CSM, implementation
  status, renewal date, segment, …) and the `Stripe_Has_*` add-on flags. Enterprise-plan
  accounts are synced even without a ChartMogul match, and their contacts come along.
- `sfdc_contacts.email_opt_out` (`HasOptedOutOfEmail`): exclude these from any campaign list.
- Data quality: `Expansion_Product__c` is blank on most expansion deals (the SFDC report
  matches products by opp name instead), and most closed-lost add-on deals have no
  `Add_On_Close_Lost_Reason__c`. Competitor info is split across `Competitor__c`,
  `Secondary_Competitor__c`, `Competitor_Free_Text__c`, `Incumbent__c`, `Competition__c`.

**Usage caveat:** "active users on product X" means active Healthie users at accounts
that pay for X. It does not mean those people use feature X. True per-product usage
needs event data from the product DB, warehouse, or Vitally directly. That source is not
connected yet.

## Healthie product data (Metabase)

`sync/metabase.py` pulls **org-level counts** from two saved Metabase questions in the
Revenue Operations collection:
- **"Provider counts by Org"** (card 11966, `DBT_L30_MART.DIM_PROVIDERS`, test and
  Healthie-employee accounts excluded): providers, active providers, admins, last provider activity.
- **"Client counts by Org"** (card 11967, `DBT_L30_MART.DIM_CLIENTS`, test users excluded):
  clients (patients) and active clients. Counts only.

It joins to SFDC accounts on `Healthie_Org_ID__c` and shows up in the Explorer (Providers
column), the customer page, the customers CSV, and `/api/account/…`.

- **Aggregates only.** Healthie data includes patient PHI. Each question has a column
  allowlist, and the sync refuses to load anything if Metabase returns another column.
- **Saved questions only, no SQL.** The gateway in front of Metabase blocks ad-hoc SQL
  from outside the UI. Edit the query in Metabase, not here.
- **Auth:** `METABASE_API_KEY`, a read-only key that only needs access to the collection
  holding the two cards. Set it on **both** Railway services: `sync` uses it for the
  nightly run, and `web` for manual refreshes. `METABASE_SESSION` (your browser's
  `metabase.SESSION` cookie) still works as a fallback for **local testing only**. Never
  set a session cookie on Railway.
- Runs at the end of the nightly sync (non-fatal, own transaction). Without credentials
  it is skipped and existing data is kept. Refresh on its own: `python -m sync.metabase`
  (locally, `.env` is loaded automatically).
- **Refresh production on demand** without waiting for the nightly run (needs
  `railway link` to this project and an SSH key registered via `railway ssh keys add`):
  ```bash
  railway ssh --service web -- python -m sync.metabase
  ```
  The ChartMogul/SFDC sync doesn't clear these counts, and the site picks up a refresh on
  the next page load.
- "Active" = `PROVIDER_IS_ACTIVE` (not deactivated), not recent usage. ~95% of orgs have
  at least one. Use last provider activity for recency.
- Providers at sub-organizations count under the sub-org's own ID, not the parent's.

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
- **Product-first sync.** ChartMogul customers are listed with a server-side status filter
  (`CM_CUSTOMER_STATUSES`, default `Active,Cancelled`, so product history is kept).
  Subscriptions are fetched in parallel (`CM_WORKERS`, default 10), then SFDC is queried
  only for those customers' Stripe/Org IDs, not all ~212k Stripe-linked accounts.
  `CM_PRODUCT_FILTER` (comma-separated product names, optional) restricts the sync to
  specific products.
- **Products = plan families.** ChartMogul has 877 plans, mostly per-customer variants
  (`Standard Seats - Weis Markets Inc.`). The text before ` - ` is the product (~258 of them).
  A subscription is *current* if ChartMogul says `active`. A customer is a *former*
  customer of a product if they held it and nothing on it is live now.
- **Full refresh daily, one transaction.** The dashboard serves the previous snapshot until
  the new one commits. ChartMogul/SFDC calls retry with backoff (~5 min tolerance), because
  ChartMogul's API returned 500s and timeouts for stretches on 2026-11-02.
- **Schema changes:** `init_db()` adds missing columns in place (tables are reloaded nightly).

## Deploy on Railway

1. Push this repo to GitHub, create a Railway project from it.
2. Add a **Postgres** database to the project (`DATABASE_URL` is injected automatically).
3. Create **two services from the same repo**, named exactly `web` and `sync`.
   Both start with `python start.py` (from `Procfile` + the `RAILPACK_START_CMD` service
   variable; Railway ignored `railway.json` on CLI uploads). `start.py` picks its role from
   `RAILWAY_SERVICE_NAME` (override with `SERVICE_ROLE`):
   - `web` serves the dashboard. Generate a public domain for it.
   - `sync` runs once and exits. In the dashboard, set Service → Settings → Cron Schedule
     to `0 12 * * *` (12:00 UTC ≈ 7/8am ET). Not earlier: ChartMogul's customer list
     timed out every night at 09:00 UTC in early Nov 2026 while working fine later in the
     day. If ChartMogul is still down, the sync waits 30 min and retries once, and the
     Metabase step runs either way.
     The CLI's `environment edit --service-config` did not apply this (v4.44), so set it in the UI.
4. Set env vars on **both** services (see `.env.example`).
5. Trigger the sync service once manually (Run now), then open the web service URL.
6. Web service → Settings → Networking → generate the public domain. Consider
   restricting access (Railway has no built-in auth — see Auth below).

### What the sync does, and how long it takes

Three stages, each committed on its own, so a slow or failing ChartMogul never holds back
the Salesforce data (Enterprise, Win / loss, Data quality) and vice versa:

| Stage | Nightly (incremental) | Weekly full refresh |
|---|---|---|
| ChartMogul: customers, subscriptions, activities, product MRR | ~35s: only customers in ChartMogul's activity feed since the last refresh (~30-50 a day of ~15k), product MRR for the last 3 months | ~40 min: lists all ~15k customers and fetches each one's history |
| Salesforce: accounts, contacts, contracts, opportunities | a few minutes | same |
| Metabase: provider / client counts | ~40s | same |

- Full refresh on Sundays (`SYNC_FULL_WEEKDAY`), with `SYNC_FULL=1`, or when there's no
  previous ChartMogul refresh. It catches customer changes with no billing activity (e.g. a
  new Healthie Org ID); everything that moves MRR is picked up the next night.
- If ChartMogul fails transiently, Salesforce and Metabase still run, then ChartMogul is
  retried once after 30 min. A run where one stage failed is `partial`: the dashboard shows
  what did refresh and the top bar says "last sync partly failed".

### Deploys: automatic from `main`, after CI passes

Until 2026-11-08 both services ran manual `railway up` uploads, so production silently
fell behind `main`. Set up auto-deploy once, for **each** service (`web` and `sync`):

1. Railway → service → **Settings → Source → Connect Repo** →
   `Healthie-RevOps/product-usage-chartmogul-sfdc-dashboard`, branch **`main`**. (If the repo
   isn't listed, a GitHub org admin must grant the Railway GitHub app access to it.)
2. Same page: turn on **Wait for CI**. Deploys of `main` then wait for the GitHub Actions
   smoke tests (`.github/workflows/ci.yml`) and are skipped if they fail.
3. Keep the start command as is (`RAILPACK_START_CMD` / `python start.py`).

Notes:
- Deploying `sync` does **not** run it; it runs on its cron schedule. To load new data right
  away, use **Run now** on the `sync` service.
- Don't redeploy `sync` while it's running: Railway stops the old deployment and the run is
  lost. The next run marks it "Interrupted" and the dashboard keeps the previous data.
- Manual fallback, from the repo folder only:
  `railway up . --path-as-root --service <web|sync> --detach`. A plain `railway up` archives
  the directory `railway link` was run in, which on one machine was the home folder.

## Dependencies before first successful run

| Dependency | Owner | Detail |
|---|---|---|
| Connected App, client-credentials flow | Aaron / Edgar | Read-only integration user as "Run As"; scopes: `api`. Do **not** reuse the Claude MCP permission set — this needs its own read-only profile. |
| Field API names | You (one describe call) | `sf sobject describe -s Account \| grep -i "healthie_org\|stripe_customer"` — update `SFDC_ACCOUNT_*_FIELD` env vars if the defaults are wrong. |
| ChartMogul API key | You | Profile → API keys. Read-only key if available. |
| Org ID custom attribute in ChartMogul | Existing workstream | The already-planned segment/org-id attribute push improves the fallback match rate. Not blocking — Stripe ID covers most. |

## Auth for the dashboard

**Google sign-in, @gethealthie.com only.** This is the same `app/auth.py` used by
healthie-events-calendar and medallion-credentialing:
- The browser signs in with Google Identity Services. The server verifies the ID token
  (signature, audience, verified email, `hd` = gethealthie.com) and keeps the user in a
  signed session cookie (`SESSION_SECRET`, 14 days).
- It uses the shared Healthie internal-apps OAuth client. The client ID is public, and
  there is no client secret.
- It **fails closed**: without `SESSION_SECRET` every page returns 503.
  `ALLOW_INSECURE_NO_AUTH=1` bypasses sign-in for local dev only.
- **The deployed origin must be listed under Authorized JavaScript origins** on that OAuth
  client in Google Cloud Console (`https://product-usage.up.railway.app`). If the Railway
  domain changes, add the new one too.
- `/api/*` also accepts HTTP Basic (`DASH_USER`/`DASH_PASS`) so scripts and Claude skills
  can call the JSON API. Pages and CSV exports require Google sign-in.

## Local dev

```bash
pip install -r requirements-dev.txt   # includes requirements.txt + test deps
cp .env.example .env   # fill in keys
python -m sync.run_sync          # populates local.db (SQLite fallback)
ALLOW_INSECURE_NO_AUTH=1 uvicorn app.main:app --reload   # skip Google sign-in locally
```

## Pages

- `/products`: every product (plan family) with current/former customers, seats, MRR, ARR, active users, CSV links
- `/product?product=…&view=current|former|all`: customers on that product (product MRR, total MRR, contract value/end,
  usage, health, owner) + active users with name/email/phone
- `/product/users.csv?product=…&view=…`: full active-user export for that product
- `/` (Home): "What do you want to do?" Four task cards with a live number each, plus where the data
  comes from and where it can go. Old explorer links with filters (`/?product=…`) redirect to `/explorer`.
- `/explorer` (Products): customers by product with search / status / match filters, charts, contacts.
- `/customer/{cm_uuid}`: products, usage figures, contracts, contacts with active-user flag
- **Layout convention:** every task page is a sequence of numbered steps (choose → find / review →
  act), each with a one-line instruction, ending in a **"Use this list"** step that says what each
  download contains and where it goes (e.g. HubSpot import). Keep new pages to this pattern.
- `/enterprise`: Enterprise add-on adoption. One row per Enterprise SFDC account, one column per
  add-on (AI Scribe, Bridge, Visualize+, API, E-Rx, full/semi white label, sub-orgs, Zoom,
  HubSpot, Shopify), each cell **Has it / In pipeline / Lost / Not pitched**. Replaces the SFDC
  reports Enterprise Customer Attribute Reporting, Scribe/Bridge/Vis+ Open Opportunities, and
  Add-On Closed Lost. Logic in `app/enterprise.py`:
  - *Has it*: SFDC `Stripe_Has_*` flag, or a live ChartMogul subscription to the add-on's
    product (Bridge and Visualize+ have no SFDC flag), or a deal won in the last 120 days.
  - Deal -> add-on: `Expansion_Product__c` when set, else patterns over the opp name/product text.
  - CSVs: accounts with every add-on state, and **providers & admins** at those accounts (active
    Healthie users with a Standard seat or owner/admin role, with an email, not opted out).

- `/accounts` (Accounts): Enterprise account data completeness (CSM, segment, renewal, kickoff,
  Org ID, reference / case study / logo fields) with "missing" and "Yes" filters and a CSV.
  **Writes to Salesforce:** people listed in `SFDC_EDITORS` (web service) can edit five reference
  fields inline: Referenceable, Reference Date, Contracted to Case Study, Case Study Complete,
  Contracted to Use Logo. Each save re-reads the field from Salesforce first and writes nothing if it
  changed since the last sync; every attempt (saved / conflict / error) is logged in `account_edits`
  with the person's email, since Salesforce records the change as the app's login. These are the
  only writes the app makes (`sync/sfdc.py` `ACCOUNT_EDITABLE_FIELDS`). Logic in `app/accounts.py`.
- `/enterprise`: each add-on column header has a status filter (has it / in pipeline / lost / not
  pitched / doesn't have it); click the add-on name to sort by its status.
- `/retention` (Retention): stickiness by plan. Enterprise, Group + API, Group and Private practice
  (`app/segments.py`) compared on customers, MRR, new in 12 months, median tenure, 12-month logo
  retention (cohort = on that plan 12 months ago; retained = still paying, any plan), churn in 90 days,
  NRR / GRR (from ChartMogul activity MRR), share using Healthie (Metabase active providers) and
  "paying, not using" (no active providers or no provider login in 30 days). Trend of 12-month
  retention over the last year; at-risk and churned-90-days lists + CSVs. Logic in `app/stickiness.py`.
- **Plans** (`app/segments.py`): decided by what ChartMogul bills (Group+ Plan or Group + API Access =
  "Group + API"; Plus / Essentials / Small Practice / Core / Starter / Beginnings / Student = "Private
  practice"). Salesforce `Current_Plan__c` is only a fallback: it's stale for private practice (3,886
  paying accounts still say "Plus Trial" as of 2026-11-08). Exception: accounts Salesforce calls
  Enterprise stay Enterprise, so the Enterprise views match the SFDC Enterprise report. Add-ons and
  Accounts have a plan selector (Enterprise by default); Enterprise MRR is Salesforce MRR, other plans
  use ChartMogul MRR.
- `/deals` (Win / loss): every synced opportunity with win rate, Year 1 ACV, and loss reasons
  **by competitor**, plus a loss-reason breakdown and a deals table + CSV. Defaults to new-sales
  deals closing this year (the SFDC "Competitor Field Opportunities" report's scope). Logic in
  `app/deals.py`: one competitor per deal (`Competitor__c`, else `Competitor_Free_Text__c`, else
  the first name in `Competition__c`), placeholders like "Other / Unknown" count as not recorded,
  spelling variants merge; loss reasons drop the `R:`/`NR:` prefix into a separate
  regrettable / non-regrettable field.

- **Take the tour** (top bar, any page): a guided walkthrough of every section (`app/static/tour.js`),
  offered once on a person's first visit. Pages explain when their data hasn't loaded yet (last good
  sync, a sync in progress, next run).
- `/quality` (Data quality): a Salesforce fix list with an owner per fix. Deal rules: closed
  new-sales deal with no competitor answer, lost deal with no loss reason, lost add-on deal with
  no add-on loss reason, Expansion Deal with no Expansion Product (with a suggestion from the
  deal name), won deal with no Year 1 ACV, open deal past its close date. Account rule: Enterprise
  `Stripe_Has_*` flag that disagrees with live ChartMogul billing (owner = CSM). Counts by rule
  and by owner; the CSV is one row per fix with a Salesforce link. Logic in `app/quality.py`.

## JSON API (for other tooling / Claude skills)

- `GET /api/products` — plan-level rollup: customers + MRR per product
- `GET /api/account/{sfdc_account_id}` — full product/pricing/contact picture for one account

The second endpoint is the interesting one long-term: any skill or script that has an
SFDC Account ID can ask this service "what does this account pay for" without touching
ChartMogul or burning SFDC API calls.
