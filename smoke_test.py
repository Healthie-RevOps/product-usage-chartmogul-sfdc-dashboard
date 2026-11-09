"""Smoke test: SQLite DB, seeded rows, every route, exports, history, auth on and off.

    python smoke_test.py
"""
import base64
import csv
import io
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(), "smoke.db"
).replace("\\", "/")
# Keep a developer's real .env out of the test (dotenv never overrides existing vars).
os.environ["METABASE_API_KEY"] = ""
os.environ["METABASE_SESSION"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402
from app.db import (  # noqa: E402
    Activity, Customer, HealthieOrgUsage, ProductMrr, SfdcAccount, SfdcContact, SfdcContract, Subscription,
    SyncRun, init_db, session_scope,
)

init_db()

ACME_LOGIN = date.today() - timedelta(days=7)   # relative, so usage checks don't age out
ACME, ORPHAN, GONE, MULTI = "cus_uuid_matched", "cus_uuid_unmatched", "cus_uuid_gone", "cus_uuid_multi"

with session_scope() as s:
    s.add(SfdcAccount(id="001000000000001AAA", name="Acme Health",
                      healthie_org_id="12345", stripe_customer_id="cus_matched",
                      owner_name="Vivek Marthi", account_type="Customer",
                      active_standard_users=8, active_clients=420, num_providers=10,
                      health_score="Green"))
    s.add(SfdcContact(id="003000000000001AAA", account_id="001000000000001AAA",
                      name="Jane Doe", email="jane@acme.test", title="COO",
                      phone="555-0100", user_active=True, user_type="Standard",
                      is_org_owner=True))
    s.add(SfdcContact(id="003000000000002AAA", account_id="001000000000001AAA",
                      name="Former User", email="gone@acme.test", user_active=False))
    s.add(SfdcContract(id="006000000000001AAA", account_id="001000000000001AAA",
                       name="Acme - New Business", close_date=date(2024, 1, 1),
                       amount=30000, total_contract_value=60000, year1_acv=30000,
                       contract_start=date(2024, 1, 1), contract_end=date(2026, 1, 1),
                       term_months=24))
    s.add(Customer(cm_uuid=ACME, name="Acme Health", email="ap@acme.test",
                   status="Active", customer_since=datetime(2024, 1, 1), mrr_cents=250000,
                   currency="USD", stripe_customer_id="cus_matched", healthie_org_id="12345",
                   sfdc_account_id="001000000000001AAA", match_method="stripe_id",
                   cm_url="https://app.chartmogul.com/#/customers/x"))
    s.add(Subscription(cm_uuid="sub_uuid_1", customer_uuid=ACME,
                       plan_name="Group Plan - Acme", product="Group Plan",
                       is_current=True, external_id="sub_stripe_1", status="active",
                       quantity=10, mrr_cents=250000, currency="USD", billing_cycle="month",
                       start_date=datetime(2024, 1, 1)))
    s.add(Subscription(cm_uuid="sub_uuid_1b", customer_uuid=ACME,
                       plan_name="AI Scribe", product="AI Scribe", is_current=False,
                       status="cancelled", quantity=1, mrr_cents=0,
                       start_date=datetime(2025, 3, 1), end_date=datetime(2025, 9, 1)))
    s.add(Activity(customer_uuid=ACME, date=datetime(2024, 1, 1), type="new_biz",
                   mrr_cents=200000, movement_cents=200000, product="Group Plan",
                   plan_name="Group Plan - Acme", description="subscribed to <b>Group</b> Plan"))
    s.add(Activity(customer_uuid=ACME, date=datetime(2025, 3, 1), type="expansion",
                   mrr_cents=260000, movement_cents=60000, product="AI Scribe",
                   plan_name="AI Scribe", description="added AI Scribe"))
    s.add(Activity(customer_uuid=ACME, date=datetime(2025, 9, 1), type="contraction",
                   mrr_cents=250000, movement_cents=-10000, product="AI Scribe",
                   plan_name="AI Scribe", description="removed AI Scribe"))
    s.add(Customer(cm_uuid=ORPHAN, name="Orphan Clinic",
                   email="billing@orphan.test", status="Active", mrr_cents=50000,
                   currency="USD", stripe_customer_id="cus_orphan", match_method="unmatched"))
    s.add(Subscription(cm_uuid="sub_uuid_3", customer_uuid=ORPHAN, plan_name="Plus Plan",
                       product="Plus Plan", is_current=True, status="active", quantity=1,
                       mrr_cents=50000, start_date=datetime(2023, 5, 1)))
    s.add(SfdcAccount(id="001000000000002AAA", name="Gone Clinic",
                      stripe_customer_id="cus_gone", owner_name="Rep"))
    s.add(SfdcContact(id="003000000000003AAA", account_id="001000000000002AAA",
                      name="Old Admin", email="admin@gone.test", user_active=True,
                      is_org_admin=True))
    s.add(Customer(cm_uuid=GONE, name="Gone Clinic", status="Cancelled",
                   mrr_cents=0, stripe_customer_id="cus_gone",
                   sfdc_account_id="001000000000002AAA", match_method="stripe_id"))
    s.add(Subscription(cm_uuid="sub_uuid_2", customer_uuid=GONE,
                       plan_name="Group Plan - Gone", product="Group Plan", is_current=False,
                       status="cancelled", quantity=3, mrr_cents=0,
                       start_date=datetime(2022, 1, 1), end_date=datetime(2024, 6, 30)))
    # a customer holding two selected products (multi-select dedupe)
    s.add(SfdcAccount(id="001000000000003AAA", name="Multi Co", stripe_customer_id="cus_multi"))
    s.add(SfdcContact(id="003000000000004AAA", account_id="001000000000003AAA",
                      name="Mo Multi", email="mo@multi.test", user_active=True))
    s.add(Customer(cm_uuid=MULTI, name="Multi Co", status="Active", mrr_cents=30000,
                   stripe_customer_id="cus_multi", sfdc_account_id="001000000000003AAA",
                   match_method="stripe_id"))
    for i, prod in enumerate(("Group Plan", "Plus Plan")):
        s.add(Subscription(cm_uuid=f"sub_multi_{i}", customer_uuid=MULTI, plan_name=prod,
                           product=prod, is_current=True, status="active", quantity=2,
                           mrr_cents=15000, start_date=datetime(2025, 1, 15)))
    # a ChartMogul revenue bucket that is not a real customer
    s.add(Customer(cm_uuid="cus_partner", name="Non EHR MRR: Payment Processing", status="Active",
                   mrr_cents=999900, stripe_customer_id="cus_pp", match_method="unmatched"))
    s.add(Subscription(cm_uuid="sub_partner", customer_uuid="cus_partner", plan_name="Baraka Wellness",
                       product="Baraka Wellness", is_current=True, status="active", quantity=1,
                       mrr_cents=999900, start_date=datetime(2024, 2, 1)))
    s.add(Subscription(cm_uuid="sub_partner2", customer_uuid="cus_partner", plan_name="Group Plan - PP",
                       product="Group Plan", is_current=True, status="active", quantity=1,
                       mrr_cents=1000, start_date=datetime(2024, 2, 1)))
    for month, mrr in ((date(2023, 12, 31), 0), (date(2024, 1, 31), 200000),
                       (date(2025, 6, 30), 265000), (date(2025, 12, 31), 265000)):
        s.add(ProductMrr(product="Group Plan", month=month, mrr_cents=mrr))
        s.add(ProductMrr(product="Plus Plan", month=month, mrr_cents=mrr // 4))
    # Healthie product usage (Metabase) for Acme's org 12345, plus an org nobody maps to
    s.add(HealthieOrgUsage(org_id="12345", providers=12, active_providers=9, admins=2,
                           last_provider_activity=ACME_LOGIN, clients=500,
                           active_clients=420))
    s.add(HealthieOrgUsage(org_id="99999", providers=1, active_providers=1, admins=1))
    s.add(SyncRun(finished_at=datetime.now(), status="success", customers_synced=4,
                  subscriptions_synced=6, accounts_synced=3, contacts_synced=4,
                  matched=3, unmatched=1))

GET_ROUTES = [
    "/", "/?product=Group+Plan&view=former", "/customer/" + ACME, "/customer/" + ORPHAN,
    "/customer/" + GONE, "/api/products", "/api/explore", "/api/explore?product=Group+Plan",
    "/api/trend?product=Group+Plan", "/api/customer/" + ACME + "/history",
    "/api/account/001000000000001AAA", "/health",
]
failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r:.140} (want {want!r:.80})")
    if not ok:
        failures.append(label)


def spec(**kw):
    return {"spec": json.dumps(kw)}


def csv_rows(resp):
    return list(csv.DictReader(io.StringIO(resp.text)))


client = TestClient(main.app)
FETCH = {"X-Requested-With": "fetch"}

print("routes (ALLOW_INSECURE_NO_AUTH=1, local dev):")
os.environ.pop("SESSION_SECRET", None)
os.environ["ALLOW_INSECURE_NO_AUTH"] = "1"
main.DASH_USER = main.DASH_PASS = ""
for route in GET_ROUTES:
    check(route, client.get(route).status_code, 200)
check("/customer/<missing> -> 404", client.get("/customer/nope").status_code, 404)
check("/api/account/<missing> -> 404", client.get("/api/account/001xxx").status_code, 404)
check("static CSS served", client.get("/static/app.css").status_code, 200)
r = client.get("/products", follow_redirects=False)
check("old /products redirects", (r.status_code, r.headers.get("location")), (301, "/explorer"))
r = client.get("/product?product=Group+Plan&view=former", follow_redirects=False)
check("old /product redirects to explorer",
      r.headers.get("location"), "/explorer?product=Group%20Plan&view=former")

print("\nexplorer data:")
prods = {p["product"]: p for p in client.get("/api/products").json()}
check("rollup Group Plan current/former", (prods["Group Plan"]["current"], prods["Group Plan"]["former"]), (2, 1))
check("rollup Group Plan MRR", prods["Group Plan"]["mrr_cents"], 265000)
ex = client.get("/api/explore?product=Group+Plan").json()
check("current view: Acme + Multi", sorted(r["uuid"] for r in ex["rows"]), sorted([ACME, MULTI]))
check("sorted by product MRR", ex["rows"][0]["uuid"], ACME)
check("totals", (ex["totals"]["current"], ex["totals"]["former"], ex["totals"]["product_mrr"]), (2, 1, 265000))
check("contract value summed per account", ex["totals"]["contract_value"], 60000)
ex = client.get("/api/explore?product=Group+Plan&view=former").json()
check("former view: Gone only", [r["uuid"] for r in ex["rows"]], [GONE])
check("former row has end date", ex["rows"][0]["ended"], "2024-06-30")
ex = client.get("/api/explore?product=Group+Plan&product=Plus+Plan").json()
check("multi-product: 3 current, no duplicates", sorted(r["uuid"] for r in ex["rows"]), sorted([ACME, MULTI, ORPHAN]))
multi_row = next(r for r in ex["rows"] if r["uuid"] == MULTI)
check("multi-product MRR sums both products", multi_row["product_mrr"], 30000)
check("unmatched counted", ex["totals"]["unmatched"], 1)
ex = client.get("/api/explore?match=unmatched").json()
check("match=unmatched", [r["uuid"] for r in ex["rows"]], [ORPHAN])

print("\nhealthie org usage (Metabase):")
ex = client.get("/api/explore?product=Group+Plan").json()
acme = next(r for r in ex["rows"] if r["uuid"] == ACME)["account"]
check("usage joined on Org ID", (acme["org_id"], acme["hl_active_providers"], acme["hl_providers"],
                                 acme["hl_admins"], acme["hl_last_activity"]),
      ("12345", 9, 12, 2, ACME_LOGIN.isoformat()))
multi = next(r for r in ex["rows"] if r["uuid"] == MULTI)["account"]
check("account without Org ID has no usage", (multi["org_id"], multi["hl_providers"]), (None, None))
page = client.get("/customer/" + ACME).text
check("customer page shows Providers KPI", "9 <span class=\"small muted\">active</span>" in page, True)
check("customer page without Org ID explains why",
      "no Healthie Org ID in Salesforce" in client.get("/customer/" + MULTI).text, True)
api = client.get("/api/account/001000000000001AAA").json()["account"]
check("account API includes healthie_product", api["healthie_product"],
      {"providers": 12, "active_providers": 9, "admins": 2, "last_provider_activity": ACME_LOGIN.isoformat(),
       "clients": 500, "active_clients": 420})
check("customer page shows Clients KPI", "of 500 in Healthie" in page, True)
check("clients in explorer row", (acme["hl_active_clients"], acme["hl_clients"]), (420, 500))
rows = {r["customer"]: r for r in csv_rows(client.post("/export/customers.csv", data=spec()))}
check("customers CSV has provider columns",
      (rows["Acme Health"]["healthie_org_id"], rows["Acme Health"]["providers_active"],
       rows["Acme Health"]["providers_total"], rows["Acme Health"]["last_provider_activity"],
       rows["Acme Health"]["clients_active"], rows["Acme Health"]["clients_total"]),
      ("12345", "9", "12", ACME_LOGIN.isoformat(), "420", "500"))

from sync import metabase as mb  # noqa: E402
good = {k: 1 for k in mb.PROVIDER_COUNTS_COLUMNS}
try:
    mb._check_columns(1, [{**good, "Provider Email": "x@y.test"}], mb.PROVIDER_COUNTS_COLUMNS)
    check("extra (PII) column refused", "loaded", "refused")
except mb.MetabaseError as exc:
    check("extra (PII) column refused", "outside the allowlist" in str(exc), True)
try:
    mb._check_columns(1, [{"Count": 1}], mb.PROVIDER_COUNTS_COLUMNS)
    check("missing columns refused", "loaded", "refused")
except mb.MetabaseError as exc:
    check("missing columns refused", "missing expected columns" in str(exc), True)
mb._check_columns(1, [good], mb.PROVIDER_COUNTS_COLUMNS)
check("allowlisted columns accepted", True, True)
check("no Metabase creds -> sync skipped, data kept",
      (mb.API_KEY, mb.SESSION) == ("", "") and mb.sync_org_usage() is None, True)
ex = client.get("/api/explore?q=multi").json()
check("search", [r["uuid"] for r in ex["rows"]], [MULTI])
ex = client.get("/api/explore?product=Nonexistent").json()
check("unknown product -> empty", ex["rows"], [])

tr = client.get("/api/trend?product=Group+Plan&product=Plus+Plan").json()
check("MRR trend starts at first month with revenue", tr["months"][0], "2024-01-31")
check("customers trend starts at earliest subscription", tr["customer_months"][0], "2022-01-31")
cm_idx = {m: i for i, m in enumerate(tr["customer_months"])}
check("trend total MRR = sum of products", tr["mrr"][-1], 265000 + 265000 // 4)
check("per-product series for 2 products", [s["product"] for s in tr["series"]], ["Group Plan", "Plus Plan"])
check("customers over time (Dec 2023: Orphan, Gone)", tr["customers"][cm_idx["2023-12-31"]], 2)
check("customers over time (Jan 2024: Acme, Orphan, Gone)", tr["customers"][cm_idx["2024-01-31"]], 3)
check("customers over time (Dec 2025: Acme, Orphan, Multi)", tr["customers"][cm_idx["2025-12-31"]], 3)
check("partner bucket excluded from customer counts", tr["customers"][-1], 3)
tr_p = client.get("/api/trend?product=Group+Plan&include_partner=true").json()
check("partner bucket included on request", tr_p["customers"][-1], 3)

hist = client.get(f"/api/customer/{ACME}/history").json()
check("history events in date order", [e["type"] for e in hist["events"]], ["new_biz", "expansion", "contraction"])
check("history segments incl. ended", sorted((s["product"], s["current"]) for s in hist["segments"]),
      [("AI Scribe", False), ("Group Plan", True)])
page = client.get("/customer/" + ACME).text
check("customer page shows change log", "removed AI Scribe" in page, True)
check("customer page escapes descriptions", "<b>Group</b>" in page, False)
check("customer page has contract value", "$60,000" in page, True)
check("customer page branded", "healthie-mark.png" in page and "Product Explorer" in page, True)

print("\nnon-EHR revenue buckets:")
check("bucket hidden from product picker", "Baraka Wellness" in prods, False)
ex = client.get("/api/explore?view=all").json()
check("bucket hidden from explorer by default", "cus_partner" in [r["uuid"] for r in ex["rows"]], False)
check("toggle count reported", ex["totals"]["partners"], 1)
ex = client.get("/api/explore?view=all&include_partner=true").json()
row = next((r for r in ex["rows"] if r["uuid"] == "cus_partner"), None)
check("bucket shown with toggle, flagged", bool(row and row["partner"]), True)
check("Group Plan current count ignores bucket",
      client.get("/api/explore?product=Group+Plan").json()["totals"]["current"], 2)
check("Group Plan toggle count",
      client.get("/api/explore?product=Group+Plan").json()["totals"]["partners"], 1)
check("Plus Plan has no bucket",
      client.get("/api/explore?product=Plus+Plan").json()["totals"]["partners"], 0)
ex = client.get("/api/explore?product=Group+Plan&include_partner=true").json()
check("toggle adds bucket to Group Plan current", ex["totals"]["current"], 3)

print("\ncontacts + exports:")
c = client.post("/api/contacts", json={"products": ["Group Plan"], "view": "current"}, headers=FETCH).json()
check("active contacts for current Group Plan customers", sorted(x["email"] for x in c["rows"]), ["jane@acme.test", "mo@multi.test"])
c = client.post("/api/contacts", json={"products": ["Group Plan"], "view": "current",
                                       "customer_uuids": [ACME], "active_only": False}, headers=FETCH).json()
check("selected customer, all contacts", sorted(x["email"] for x in c["rows"]), ["gone@acme.test", "jane@acme.test"])
c = client.post("/api/contacts", json={"products": ["Group Plan"], "view": "all", "role": "admins"}, headers=FETCH).json()
check("owners & admins only", sorted(x["email"] for x in c["rows"]), ["admin@gone.test", "jane@acme.test"])

r = client.post("/export/contacts.csv", data=spec(products=["Group Plan"], view="current"))
rows = csv_rows(r)
check("contacts CSV is an attachment", "attachment" in r.headers.get("content-disposition", ""), True)
check("contacts CSV rows", sorted(x["email"] for x in rows), ["jane@acme.test", "mo@multi.test"])
check("contacts CSV has phone + customer", (rows[0]["phone"] or rows[1]["phone"], {x["customer"] for x in rows}),
      ("555-0100", {"Acme Health", "Multi Co"}))
r = client.post("/export/contacts.csv", data=spec(products=["Group Plan"], view="current",
                                                  contact_ids=["003000000000004AAA"]))
check("export selected contacts only", [x["email"] for x in csv_rows(r)], ["mo@multi.test"])
r = client.post("/export/contacts.csv", data=spec(customer_uuids=[ACME], view="all", active_only=False))
check("customer-page export (all contacts)", sorted(x["email"] for x in csv_rows(r)), ["gone@acme.test", "jane@acme.test"])
r = client.post("/export/customers.csv", data=spec(products=["Group Plan"], view="all"))
rows = csv_rows(r)
check("customers CSV rows", sorted(x["customer"] for x in rows), ["Acme Health", "Gone Clinic", "Multi Co"])
check("customers CSV marks former", next(x for x in rows if x["customer"] == "Gone Clinic")["product_status"], "former")
r = client.post("/export/customers.csv", data=spec(products=["Group Plan"], view="all", customer_uuids=[MULTI]))
check("customers CSV honours selection", [x["customer"] for x in csv_rows(r)], ["Multi Co"])
check("bad spec -> 400", client.post("/export/customers.csv", data={"spec": "{nope"}).status_code, 400)

print("\nenterprise add-ons:")
from app import enterprise as ent  # noqa: E402
from app.db import SfdcOpportunity  # noqa: E402

ENT_ACCT, ENT_CUST, PROSPECT = "001000000000009AAA", "cus_uuid_ent", "001000000000010AAA"
with session_scope() as s:
    s.add(SfdcAccount(id=ENT_ACCT, name="Ent Co | Org Id: 777", stripe_customer_id="cus_ent",
                      healthie_org_id="777", current_plan="Enterprise", customer_status="Customer",
                      sfdc_mrr=4000, csm_name="Casey CSM", account_segment="Strategic",
                      implementation_status="Live", has_api_access=True))
    s.add(Customer(cm_uuid=ENT_CUST, name="Ent billing", status="Active", mrr_cents=400000,
                   stripe_customer_id="cus_ent", sfdc_account_id=ENT_ACCT, match_method="stripe_id"))
    s.add(Subscription(cm_uuid="sub_ent_vis", customer_uuid=ENT_CUST, plan_name="Visualize+ Lite",
                       product="Visualize+ Lite", is_current=True, status="active", quantity=1,
                       mrr_cents=50000, start_date=datetime(2026, 1, 1)))
    # unmatched, non-paying Enterprise account (only in "All Enterprise")
    s.add(SfdcAccount(id=PROSPECT, name="Prospect Ent", current_plan="Enterprise", sfdc_mrr=0))
    opp = dict(account_id=ENT_ACCT, account_name="Ent Co")
    s.add(SfdcOpportunity(id="006E1", name="Ent Co - Bridge add-on", stage="Negotiation",
                          is_closed=False, is_won=False, close_date=date(2026, 12, 15),
                          amount=5000, **opp))
    s.add(SfdcOpportunity(id="006E2", name="Ent Co - AI scribe expansion", stage="Closed Lost",
                          is_closed=True, is_won=False, close_date=date(2026, 5, 1),
                          addon_loss_reason="Pricing Sensitivity", revisit_date=date(2027, 1, 1),
                          competitor_text="Freed", **opp))
    s.add(SfdcOpportunity(id="006E3", name="Ent Co - Upgrade", expansion_product="Full Web White Label & Customizations",
                          stage="Closed Won", is_closed=True, is_won=True, close_date=date(2024, 2, 1), **opp))
    s.add(SfdcOpportunity(id="006E4", name="Ent Co semi-whitelabel", stage="Discovery",
                          is_closed=False, is_won=False, close_date=date(2027, 2, 1), **opp))
    ct = dict(account_id=ENT_ACCT, user_active=True)
    s.add(SfdcContact(id="003E1", name="Pat Provider", email="pat@ent.test", user_type="Standard",
                      title='=HYPERLINK("http://x")', **ct))
    s.add(SfdcContact(id="003E2", name="Ada Admin", email="ada@ent.test", is_org_admin=True, **ct))
    s.add(SfdcContact(id="003E3", name="Sam Support", email="sam@ent.test", user_type="Support", **ct))
    s.add(SfdcContact(id="003E4", name="Opted Out", email="out@ent.test", user_type="Standard",
                      email_opt_out=True, **ct))
    s.add(SfdcContact(id="003E5", name="No Email", user_type="Standard", **ct))
    s.add(SfdcContact(id="003E6", name="Inactive Provider", email="old@ent.test", user_type="Standard",
                      account_id=ENT_ACCT, user_active=False))
    s.add(SyncRun(finished_at=datetime.now(), status="success"))  # new snapshot key

check("detect: Expansion_Product__c wins over name",
      ent.detect_addons(SfdcOpportunity(name="Scribe deal", expansion_product="Bridge")), {"bridge"})
check("detect: several add-ons in one name",
      ent.detect_addons(SfdcOpportunity(name="Acme - Scribe + Bridge + Vis+")),
      {"ai_scribe", "bridge", "visualize"})
check("detect: 'white label' -> full, 'semi white label' -> semi only",
      (ent.detect_addons(SfdcOpportunity(name="Acme white label")),
       ent.detect_addons(SfdcOpportunity(name="Acme Semi White Label"))), ({"full_wl"}, {"semi_wl"}))
r = client.get("/enterprise")
check("add-ons page renders", (r.status_code, "<h1>Add-ons</h1>" in r.text), (200, True))
ex = client.get("/api/enterprise").json()
check("paying only by default", [x["name"] for x in ex["rows"]], ["Ent Co"])
cells = {k: v["state"] for k, v in ex["rows"][0]["addons"].items()}
check("states: flag / ChartMogul / open / lost / old win / never",
      (cells["api"], cells["visualize"], cells["bridge"], cells["ai_scribe"], cells["full_wl"],
       cells["semi_wl"], cells["zoom"]),
      ("has", "has", "pipeline", "lost", "none", "pipeline", "none"))
sc = ex["rows"][0]["addons"]["ai_scribe"]
check("lost cell carries reason, revisit date, competitor",
      (sc["reason"], sc["revisit_date"], sc["competitor"]), ("Pricing Sensitivity", "2027-01-01", "Freed"))
check("old won deal no longer billed is noted", ex["rows"][0]["addons"]["full_wl"].get("note"),
      "Won 2024-02-01, not billed now")
summ = {x["key"]: x["counts"] for x in ex["summary"]}
check("summary counts", (summ["bridge"]["pipeline"], summ["ai_scribe"]["lost"], summ["zoom"]["none"]),
      (1, 1, 1))
ex = client.get("/api/enterprise?paying=false").json()
check("all Enterprise includes unmatched non-paying", sorted(x["name"] for x in ex["rows"]),
      ["Ent Co", "Prospect Ent"])
ex = client.get("/api/enterprise?paying=false&addon=bridge:none").json()
check("add-on state filter", [x["name"] for x in ex["rows"]], ["Prospect Ent"])
ex = client.get("/api/enterprise?paying=false&addon=api:not_has&addon=bogus:none").json()
check("not_has filter; unknown add-on ignored", [x["name"] for x in ex["rows"]], ["Prospect Ent"])
check("facets for dropdowns", client.get("/api/enterprise").json()["facets"]["csm"], ["Casey CSM"])
rows = csv_rows(client.post("/export/enterprise.csv", data=spec(paying=False, addons=["bridge:pipeline"])))
check("accounts CSV follows filters", [(x["account"], x["bridge_state"], x["ai_scribe_detail"]) for x in rows],
      [("Ent Co", "pipeline", "Pricing Sensitivity, 2026-05-01")])
rows = csv_rows(client.post("/export/enterprise-contacts.csv", data=spec()))
check("contacts CSV: active providers + admins only, no support/opted-out/no-email",
      sorted((x["name"], x["role"]) for x in rows), [("Ada Admin", "admin"), ("Pat Provider", "provider")])
check("contacts CSV neutralizes spreadsheet formulas",
      next(x["title"] for x in rows if x["name"] == "Pat Provider"), '\'=HYPERLINK("http://x")')
rows = csv_rows(client.post("/export/enterprise-contacts.csv", data=spec(account_ids=[PROSPECT], paying=False)))
check("contacts CSV narrows to selected accounts", rows, [])
check("CSV guard leaves phone numbers alone", (main._csv_cell("+1 555-0100"), main._csv_cell("-3"),
                                                main._csv_cell("@SUM(A1)")), ("+1 555-0100", "-3", "'@SUM(A1)"))

print("\nwin / loss:")
from app import deals as dl  # noqa: E402

check("competitor aliases + placeholders",
      [dl.norm_competitor(x) for x in ("Avon", "Avon Health", "Simple Practice", "SimplePractice",
                                       "ECW", "Other / Unknown", "None (Healthie User)", "Canvas")],
      ["Avon Health", "Avon Health", "SimplePractice", "SimplePractice", "eClinicalWorks", None, None, "Canvas"])
check("loss reason prefixes become their own field",
      [dl.norm_reason(x) for x in ("NR: Timing", "Timing", "R: Lost to Competitor - Price", "Product  Issues")],
      [("Timing", "non-regrettable"), ("Timing", None), ("Lost to Competitor - Price", "regrettable"),
       ("Product Issues", None)])
check("'None (Healthie User)' answers the question; 'Other / Unknown' doesn't",
      (dl.competitor_answered(SfdcOpportunity(competitor="None (Healthie User)")),
       dl.competitor_answered(SfdcOpportunity(competitor="Other / Unknown")),
       dl.competitor_answered(SfdcOpportunity())), (True, False, False))
check("primary competitor falls back to free text, first name",
      (dl.primary_competitor(SfdcOpportunity(competitor="Other / Unknown", competition="Athena, eClinicalWorks")),
       dl.primary_competitor(SfdcOpportunity(competitor="Not Applicable"))), ("athenahealth", None))
ISO = "Initial Sales Opportunity"
with session_scope() as s:
    base = dict(record_type=ISO, is_closed=True, account_id=PROSPECT, account_name="Prospect Ent")
    s.add(SfdcOpportunity(id="006W1", name="P1", is_won=True, stage="Closed Won", close_date=date(2026, 2, 1),
                          year1_acv=12000, competitor="Canvas", why_won="API", **base))
    s.add(SfdcOpportunity(id="006W2", name="P2", is_won=False, stage="Closed Lost", close_date=date(2026, 3, 1),
                          year1_acv=8000, competitor="Canvas", loss_reason="R: Lost to Competitor - Price", **base))
    s.add(SfdcOpportunity(id="006W3", name="P3", is_won=False, stage="Closed Lost", close_date=date(2026, 4, 1),
                          amount=3000, competitor="Avon", secondary_competitor="Canvas",
                          loss_reason="NR: Timing", **base))
    s.add(SfdcOpportunity(id="006W4", name="P4", is_won=False, stage="Closed Lost", close_date=date(2026, 4, 2),
                          competitor="Avon Health", loss_reason="Timing", **base))
    s.add(SfdcOpportunity(id="006W5", name="P5 old", is_won=False, stage="Closed Lost", close_date=date(2024, 6, 1),
                          competitor="Other / Unknown", **base))
    s.add(SfdcOpportunity(id="006W6", name="P6 open", record_type=ISO, is_closed=False, is_won=False,
                          stage="Discovery", close_date=date(2026, 12, 1), year1_acv=5000, competitor="Canvas",
                          account_id=PROSPECT, account_name="Prospect Ent"))
    s.add(SyncRun(finished_at=datetime.now(), status="success"))
r = client.get("/deals")
check("win/loss page renders", (r.status_code, "Win / loss" in r.text), (200, True))
d = client.get(f"/api/deals?record_type={ISO}&date_from=2026-01-01&date_to=2026-12-31").json()
t = d["totals"]
check("totals: win rate, ACV (Year 1 ACV else Amount), open",
      (t["won"], t["lost"], round(t["win_rate"], 2), t["won_value"], t["lost_value"], t["open"]),
      (1, 3, 0.25, 12000, 11000, 1))
comp = {g["competitor"]: g for g in d["competitors"]}
check("by competitor: Avon variants merged; secondary counted",
      (comp["Avon Health"]["lost"], comp["Canvas"]["won"], comp["Canvas"]["lost"], comp["Canvas"]["open"],
       comp["Canvas"]["secondary"]), (2, 1, 1, 1, 1))
reas = {g["reason"]: g for g in d["reasons"]}
check("by reason: NR: Timing + Timing merged; R/NR split",
      (reas["Timing"]["lost"], reas["Timing"]["non_regrettable"], reas["Lost to Competitor - Price"]["regrettable"]),
      (2, 1, 1))
check("date range excludes older deals", "P5 old" in [x["name"] for x in d["rows"]], False)
d2 = client.get(f"/api/deals?record_type={ISO}&date_from=2026-01-01&competitor=Canvas").json()
check("competitor filter matches primary or secondary; breakdown keeps other competitors",
      (sorted(x["name"] for x in d2["rows"]), "Avon Health" in {g["competitor"] for g in d2["competitors"]}),
      (["P1", "P2", "P3", "P6 open"], True))
d3 = client.get(f"/api/deals?record_type={ISO}&outcome=lost&reason=Timing").json()
check("outcome + reason filters", sorted(x["name"] for x in d3["rows"]), ["P3", "P4"])
rows = csv_rows(client.post("/export/deals.csv", data=spec(record_type=ISO, outcome="won")))
check("deals CSV follows filters", [(x["deal"], x["competitor"], x["why_won"]) for x in rows], [("P1", "Canvas", "API")])

print("\ndata quality:")
with session_scope() as s:
    base = dict(account_id=ENT_ACCT, account_name="Ent Co")
    s.add(SfdcOpportunity(id="006Q1", name="Ent Co - Scribe upsell", record_type="Expansion Deal",
                          opp_type="Add-on", stage="Closed Lost", is_closed=True, is_won=False,
                          close_date=date(2026, 6, 1), owner_name="Rita Rep", **base))
    s.add(SfdcOpportunity(id="006Q2", name="Newco", record_type=ISO, stage="Closed Won", is_closed=True,
                          is_won=True, close_date=date(2026, 7, 1), competitor="Canvas",
                          owner_name="Rita Rep", **base))
    s.add(SfdcOpportunity(id="006Q3", name="Stale Co", record_type=ISO, stage="Discovery", is_closed=False,
                          is_won=False, close_date=date(2026, 1, 15), owner_name="Omar Owner", **base))
    s.add(SyncRun(finished_at=datetime.now(), status="success"))
r = client.get("/quality")
check("data quality page renders", (r.status_code, "Data quality" in r.text), (200, True))
dq = client.get("/api/quality?date_from=2026-01-01").json()
work = {x["name"]: x for x in dq["rows"]}
check("add-on lost deal: no add-on reason + no Expansion Product, with a suggestion",
      (sorted(work["Ent Co - Scribe upsell"]["issues"]), work["Ent Co - Scribe upsell"]["hints"].get("expansion_product")),
      (["missing_addon_reason", "missing_expansion_product"], "AI Scribe"))
check("won deal without Year 1 ACV", work["Newco"]["issues"], ["missing_acv"])
check("open deal past its close date", (work["Stale Co"]["issues"], work["Stale Co"]["hints"]["days_overdue"] > 0),
      (["stale_open"], True))
check("clean deals are not on the worklist", "P1" in work, False)
check("date filter keeps 2024 deals out", "P5 old" in work, False)
summ = {x["key"]: x for x in dq["summary"]}
check("rule summary counts and eligibility", (summ["missing_acv"]["count"], summ["missing_acv"]["eligible"]), (1, 2))
check("flag mismatch: API flag set, not billed in ChartMogul",
      [(m["account"], m["addon"], m["problem"]) for m in dq["mismatches"]],
      [("Ent Co", "API access", "Flag is set, but no live ChartMogul subscription")])
own = {o["owner"]: o for o in dq["owners"]}
check("by owner", (own["Rita Rep"]["total"], own["Omar Owner"]["counts"]), (3, {"stale_open": 1}))
dq2 = client.get("/api/quality?date_from=2026-01-01&owner=Omar+Owner").json()
check("owner filter", ([x["name"] for x in dq2["rows"]], dq2["mismatches"]), (["Stale Co"], []))
dq3 = client.get("/api/quality?date_from=2026-01-01&issue=missing_acv").json()
check("issue filter", [x["name"] for x in dq3["rows"]], ["Newco"])
rows = csv_rows(client.post("/export/quality.csv", data=spec(date_from="2026-01-01", owner="Rita Rep")))
check("fix list CSV: one row per fix, with suggestion",
      sorted((x["issue"], x["fix"]) for x in rows),
      [("No Expansion Product", "Set Expansion Product (looks like: AI Scribe)"),
       ("No add-on loss reason", "Set Add-On Close Lost Reason"), ("Won, no Year 1 ACV", "Set Year 1 ACV")])

print("\nsync status, guides, tour:")
from app import data as data_mod  # noqa: E402
page = client.get("/enterprise").text
check("top bar says when the data is from; Tour tab and scripts present",
      ("Data as of" in page, 'id="tour-tab"' in page, "/static/tour.js" in page, 'class="step-num"' in page,
       'id="empty-state"' in page), (True, True, True, True, True))
r = client.get("/")
check("home: six task cards with live numbers", (r.status_code, r.text.count('class="task"'),
      "paying Enterprise accounts" in r.text, "win rate" in r.text, "Referenceable" in r.text), (200, 6, True, True, True))
r = client.get("/?product=Group+Plan&view=former", follow_redirects=False)
check("old explorer links on / redirect to /explorer", (r.status_code, r.headers["location"]),
      (302, "/explorer?product=Group+Plan&view=former"))
check("every task page ends with a 'Use this list' step",
      all('id="step-use"' in client.get(u).text for u in ("/explorer", "/enterprise", "/deals", "/quality")), True)
check("APIs report has_data", (client.get("/api/enterprise").json()["has_data"],
                               client.get("/api/deals").json()["has_data"],
                               client.get("/api/quality").json()["has_data"]), (True, True, True))
with session_scope() as s:
    s.add(SyncRun(status="running", started_at=datetime.utcnow()))
st = data_mod.sync_status(SessionLocal_ := main.SessionLocal())
SessionLocal_.close()
check("a run in progress shows as running; data_as_of = last success",
      (st["running_since"] is not None, st["data_as_of"] is not None, st["last_failed"]), (True, True, False))
check("top bar shows the running sync", "sync running" in client.get("/deals").text, True)
with session_scope() as s:
    s.query(SyncRun).filter(SyncRun.status == "running").update({"started_at": datetime(2026, 1, 1)})
st = data_mod.sync_status(SessionLocal_ := main.SessionLocal())
SessionLocal_.close()
check("a 'running' row older than 3h isn't shown as running", st["running_since"], None)

print("\nenterprise accounts + Salesforce edits:")
from app.db import AccountEdit  # noqa: E402,F401
from sync import sfdc as sfdc_live  # noqa: E402
with session_scope() as s:
    a_ = s.get(SfdcAccount, ENT_ACCT)
    a_.referenceable, a_.contracted_case_study, a_.renewal_date = "Yes", "No", date(2027, 3, 1)
    s.add(SyncRun(finished_at=datetime.now(), status="success"))
r = client.get("/accounts")
check("accounts page renders, read-only without SFDC_EDITORS",
      (r.status_code, "Enterprise accounts" in r.text, "Read-only for you" in r.text), (200, True, True))
ac = client.get("/api/accounts").json()
row = ac["rows"][0]
check("account values + missing count", (row["name"], row["values"]["referenceable"], row["values"]["renewal_date"],
                                         row["values"]["case_study_complete"], row["missing"] > 0),
      ("Ent Co", "Yes", "2027-03-01", None, True))
summ = {x["key"]: x for x in ac["summary"]}
check("completeness summary", (summ["referenceable"]["filled"], summ["referenceable"]["yes"],
                               summ["case_study_complete"]["missing"]), (1, 1, 1))
check("field filter: missing / yes",
      ([x["name"] for x in client.get("/api/accounts?paying=false&field=referenceable&is=missing").json()["rows"]],
       [x["name"] for x in client.get("/api/accounts?paying=false&field=referenceable&is=yes").json()["rows"]]),
      (["Prospect Ent"], ["Ent Co"]))
rows = csv_rows(client.post("/export/accounts.csv", data=spec(paying=False, field="referenceable", is_="yes")))
check("accounts CSV follows filters", [(x["account"], x["referenceable"]) for x in rows], [("Ent Co", "Yes")])

EDIT = {"X-Requested-With": "fetch"}
url_ = f"/api/accounts/{ENT_ACCT}/fields"
r = client.post(url_, json={"field": "case_study_complete", "value": "Yes", "expected": None}, headers=EDIT)
check("edit refused without SFDC_EDITORS", r.status_code, 403)
os.environ["SFDC_EDITORS"] = "someone@gethealthie.com, dev@gethealthie.com"
check("allowlisted user sees edit hint", "save straight to Salesforce" in client.get("/accounts").text, True)
writes, sf_now = [], {"Case_Study_Complete__c": None, "Reference_Date__c": None}
saved_sf = (sfdc_live.get_account, sfdc_live.update_account)
sfdc_live.get_account = lambda aid, names: {n: sf_now.get(n) for n in names}


def fake_update(aid, values):
    writes.append((aid, values))
    sf_now.update(values)


def failing_update(aid, values):
    raise sfdc_live.SfdcError("400: FIELD_CUSTOM_VALIDATION_EXCEPTION")


sfdc_live.update_account = fake_update
try:
    r = client.post(url_, json={"field": "case_study_complete", "value": "Yes", "expected": None}, headers=EDIT)
    check("edit saves to Salesforce and locally", (r.status_code, r.json(), writes,
          client.get("/api/accounts").json()["rows"][0]["values"]["case_study_complete"]),
          (200, {"status": "saved", "value": "Yes"}, [(ENT_ACCT, {"Case_Study_Complete__c": "Yes"})], "Yes"))
    sf_now["Case_Study_Complete__c"] = "No"   # someone changed it in Salesforce meanwhile
    writes.clear()
    r = client.post(url_, json={"field": "case_study_complete", "value": "", "expected": "Yes"}, headers=EDIT)
    check("conflict: changed in Salesforce since sync -> nothing written, current value shown",
          (r.json(), writes), ({"status": "conflict", "value": "No"}, []))
    r = client.post(url_, json={"field": "reference_date", "value": "2026-13-40", "expected": None}, headers=EDIT)
    check("bad date rejected", r.status_code, 400)
    r = client.post(url_, json={"field": "case_study_available", "value": "Yes"}, headers=EDIT)
    check("read-only field rejected", r.status_code, 400)
    r = client.post(url_, json={"field": "website", "value": "x"}, headers=EDIT)
    check("field outside the allowlist rejected", r.status_code, 400)
    r = client.post(url_, json={"field": "case_study_complete", "value": "Yes", "expected": "No"})
    check("missing X-Requested-With rejected (CSRF guard)", r.status_code, 400)
    r = client.post(f"/api/accounts/{PROSPECT}x/fields", json={"field": "referenceable", "value": "Yes"}, headers=EDIT)
    check("unknown account rejected", r.status_code, 400)
    sfdc_live.update_account = failing_update
    r = client.post(url_, json={"field": "reference_date", "value": "2026-11-01", "expected": None}, headers=EDIT)
    check("Salesforce error -> 502 with its message", (r.status_code, "VALIDATION" in r.json()["detail"]), (502, True))
finally:
    sfdc_live.get_account, sfdc_live.update_account = saved_sf
    os.environ.pop("SFDC_EDITORS")
log = client.get("/api/accounts/edits").json()["edits"]
check("change log records who, what, result (incl. conflicts and errors)",
      [(e["user"], e["field"], e["old"], e["new"], e["status"]) for e in log[:3]],
      [("dev@gethealthie.com", "Reference date", None, "2026-11-01", "error"),
       ("dev@gethealthie.com", "Case study complete", "No", None, "conflict"),
       ("dev@gethealthie.com", "Case study complete", None, "Yes", "saved")])
try:
    sfdc_live.update_account(ENT_ACCT, {"Name": "Hacked"})
    check("update_account rejects non-allowlisted API fields", "wrote", "refused")
except sfdc_live.SfdcError as exc:
    check("update_account rejects non-allowlisted API fields", "Not an editable field" in str(exc), True)

print("\nplans + retention:")
from app import segments as seg  # noqa: E402
from app import stickiness as stk  # noqa: E402
check("plan from ChartMogul products",
      [seg.from_products(x) for x in (["Enterprise Base Fee", "Standard Seats"], ["Group+ Plan"],
                                      ["Group Plan", "API Access"], ["Group Plan"], ["Plus Plan", "AI Scribe"],
                                      ["Essentials Plan"], ["Visualize+ Enterprise"], ["AI Scribe"])],
      ["Enterprise", "Group + API", "Group + API", "Group", "Private practice", "Private practice", None, None])
check("seats aren't an Enterprise signal (Group bills Standard Seats too)",
      (seg.from_products(["Group Plan", "Standard Seats"]), seg.from_products(["Standard Seats"])), ("Group", None))
check("plan from Salesforce (fallback): stale 'Plus Trial' is private practice; Group + API flag",
      (seg.from_sfdc("Plus Trial"), seg.from_sfdc("Group", has_api=True), seg.from_sfdc("Canceled Plan")),
      ("Private practice", "Group + API", None))
# Acme bills a current Group Plan; Gone Clinic's Group Plan ended 2024-06-30; Multi Co holds Group + Plus
r = client.get("/api/enterprise?plan=Group")
check("add-ons page: Group plan accounts (from ChartMogul billing)",
      sorted(x["name"] for x in r.json()["rows"]), ["Acme Health", "Multi Co"])
check("add-ons page: Enterprise stays the default", [x["name"] for x in client.get("/api/enterprise").json()["rows"]], ["Ent Co"])
check("add-ons page: all plans", len(client.get("/api/enterprise?plan=All+plans").json()["rows"]) >= 3, True)
check("accounts page: plan filter", sorted(x["name"] for x in client.get("/api/accounts?plan=Group").json()["rows"]),
      ["Acme Health", "Multi Co"])
r = client.get("/retention")
check("retention page renders", (r.status_code, "<h1>Retention</h1>" in r.text), (200, True))
rt = client.get("/api/retention").json()
summ = {x["plan"]: x for x in rt["summary"]}
check("retention: plans compared, incl. all-plans row", list(summ), seg.PLANS + ["All plans"])
check("retention: active Group customers and MRR (Acme 2,500 + Multi 300)",
      (summ["Group"]["active"], summ["Group"]["mrr"]), (2, 280000))
check("retention: 12-month cohort and logo retention for Group",
      (summ["Group"]["cohort_12m"], summ["Group"]["logo_retention_12m"]), (2, 1.0))  # Acme + Multi Co (from Jan 2025)
check("retention: 'using' = provider login in 30 days (Acme logged in a week ago; Multi Co has no usage data)",
      (summ["Group"]["using_share"], summ["Group"]["median_active_providers"], summ["Group"]["usage_coverage"]),
      (1.0, 9, 0.5))
check("retention: trend has 13 month points per plan",
      (len(rt["trend"]["dates"]), sorted(rt["trend"]["series"])), (13, sorted(seg.PLANS)))
c_ = stk._Cust()
c_.subs = [(date(2025, 1, 1), date(2025, 6, 1), "Plus Plan"), (date(2025, 6, 1), None, "Group Plan")]
c_.force_plan, c_.fallback_plan = None, None
c_.act_dates, c_.act_mrr = [date(2025, 1, 1), date(2025, 6, 1)], [5000, 20000]
check("customer plan and MRR follow their history",
      (stk._plan(c_, date(2025, 3, 1)), stk._plan(c_, date(2025, 9, 1)), stk._mrr_at(c_, date(2025, 3, 1)),
       stk._mrr_at(c_, date(2024, 12, 1)), stk._active(c_, date(2024, 12, 1))),
      ("Private practice", "Group", 5000, 0, False))
rows = csv_rows(client.post("/export/retention.csv", data=spec(kind="at_risk", plan="Group")))
check("at-risk CSV (Group): nobody paying-but-inactive in the seed data", rows, [])

print("\nfails closed (no SESSION_SECRET, no insecure override):")
os.environ.pop("ALLOW_INSECURE_NO_AUTH")
check("/ -> 503", client.get("/").status_code, 503)
check("/login -> 503 page", client.get("/login").status_code, 503)
check("/health still public", client.get("/health").status_code, 200)

print("\nGoogle sign-in enabled:")
os.environ["SESSION_SECRET"] = "test-secret"
main.DASH_USER, main.DASH_PASS = "dashuser", "dashpass"
basic = {"Authorization": "Basic " + base64.b64encode(b"dashuser:dashpass").decode()}
bad = {"Authorization": "Basic " + base64.b64encode(b"dashuser:wrong").decode()}
anon = TestClient(main.app)
for route in GET_ROUTES:
    r = anon.get(route, follow_redirects=False)
    want = 200 if route == "/health" else 401 if route.startswith("/api/") else 303
    check(f"signed out {route}", r.status_code, want)
check("signed out POST export -> 401",
      anon.post("/export/contacts.csv", data=spec(), follow_redirects=False).status_code, 401)
check("signed out POST /api/contacts -> 401", anon.post("/api/contacts", json={}).status_code, 401)
check("static assets public (login page needs logo)", anon.get("/static/healthie-lockup.png").status_code, 200)
r = anon.get("/?product=Group+Plan&view=former", follow_redirects=False)
check("redirect keeps destination", r.headers.get("location"),
      "/login?next=%2F%3Fproduct%3DGroup%2BPlan%26view%3Dformer")
check("login page renders Google button",
      'data-client_id="' + auth.client_id() in anon.get("/login").text, True)
check("Basic auth works for /api/*", anon.get("/api/products", headers=basic).status_code, 200)
check("Basic auth NOT accepted for pages",
      anon.get("/", headers=basic, follow_redirects=False).status_code, 303)
check("Basic auth NOT accepted for exports",
      anon.post("/export/contacts.csv", data=spec(), headers=basic).status_code, 401)
check("wrong Basic password", anon.get("/api/products", headers=bad).status_code, 401)

real_verify = auth.verify_google_credential
auth.verify_google_credential = lambda cred: (
    {"email": "pm@gethealthie.com", "name": "PM"} if cred == "good"
    else (_ for _ in ()).throw(ValueError("Use your @gethealthie.com Google account.")))
user = TestClient(main.app)
check("POST /auth/google without fetch header -> 403",
      user.post("/auth/google", json={"credential": "good"}).status_code, 403)
check("wrong-domain account -> 403",
      user.post("/auth/google", json={"credential": "bad"}, headers=FETCH).status_code, 403)
check("good account -> 200",
      user.post("/auth/google", json={"credential": "good"}, headers=FETCH).status_code, 200)
for route in GET_ROUTES:
    check(f"signed in {route}", user.get(route).status_code, 200)
check("signed-in export works", user.post("/export/contacts.csv", data=spec()).status_code, 200)
check("header shows signed-in user", "PM" in user.get("/").text, True)
check("login redirects signed-in user to next",
      user.get("/login?next=/customer/x", follow_redirects=False).headers.get("location"), "/customer/x")
check("open redirect blocked", user.get("/login?next=//evil.com",
                                        follow_redirects=False).headers.get("location"), "/")
user.post("/logout")
check("after logout -> redirect to login", user.get("/", follow_redirects=False).status_code, 303)
auth.verify_google_credential = real_verify

print("\ndomain check (real function):")
check("gethealthie.com allowed", auth.email_allowed("a@gethealthie.com"), True)
check("lookalike domain refused", auth.email_allowed("a@gethealthie.com.evil.com"), False)
check("gmail refused", auth.email_allowed("a@gmail.com"), False)

print("\nSFDC field mapping (offline):")
from sync import sfdc as sfdc_mod  # noqa: E402

_saved = (sfdc_mod._visible_fields, sfdc_mod._query_all, dict(sfdc_mod._optional_cache))
sfdc_mod._optional_cache.clear()
# This user can't see Competitor__c or Stripe_Has_Zoom__c: skipped, not a failure
sfdc_mod._visible_fields = lambda obj: (
    {f.split(".")[0].replace("__r", "__c") for f in sfdc_mod.ACCOUNT_OPTIONAL_FIELDS.values()}
    | set(sfdc_mod.OPPORTUNITY_OPTIONAL_FIELDS.values())) - {"Competitor__c", "Stripe_Has_Zoom__c"}
captured = []


def fake_query_all(soql):
    captured.append(soql)
    if "FROM Opportunity" in soql:
        yield {"Id": "006X", "AccountId": "001X", "Account": {"Name": "Prospect Co"},
               "Name": "Prospect Co - New", "RecordType": {"Name": "Initial Sales Opportunity"},
               "StageName": "Closed Lost", "IsClosed": True, "IsWon": False,
               "CloseDate": "2026-03-01", "Owner": {"Name": "Rep"},
               "Secondary_Competitor__c": "Elation", "Add_On_Close_Lost_Reason__c": None}
    else:
        yield {"Id": "001X", "Name": "Ent Co", "Current_Plan__c": "Enterprise",
               "CSM_v2__r": {"Name": "Casey CSM"}, "Stripe_Has_AI_Scribe__c": True,
               "Stripe_Has_Zoom__c": True, "MRR__c": 5000.0}


sfdc_mod._query_all = fake_query_all
try:
    opp = next(sfdc_mod.fetch_opportunities("2024-01-01"))
    acct = next(sfdc_mod.fetch_accounts("Current_Plan__c = 'Enterprise'"))
finally:
    sfdc_mod._visible_fields, sfdc_mod._query_all = _saved[0], _saved[1]
    sfdc_mod._optional_cache.clear()
    sfdc_mod._optional_cache.update(_saved[2])
check("opp: account name, record type, owner via relationships",
      (opp["account_name"], opp["record_type"], opp["owner_name"], opp["is_won"]),
      ("Prospect Co", "Initial Sales Opportunity", "Rep", False))
check("opp: hidden field skipped in SOQL and None in row",
      (", Competitor__c" in captured[0], opp["competitor"], opp["secondary_competitor"]),
      (False, None, "Elation"))
check("opp: open deals always included", "IsClosed = false OR CloseDate >= 2024-01-01" in captured[0], True)
check("account: enterprise fields + CSM relationship",
      (acct["current_plan"], acct["csm_name"], acct["sfdc_mrr"]), ("Enterprise", "Casey CSM", 5000.0))
check("account: add-on flags are booleans; hidden flag -> False",
      (acct["has_ai_scribe"], acct["has_erx"], acct["has_zoom"]), (True, False, False))

print("\nsync orchestration (stages, ChartMogul down):")
from sync import run_sync as rs  # noqa: E402
from sync import chartmogul as cm_mod  # noqa: E402


def run_main(cm_outcomes, sfdc_outcomes=(None,)):
    """Run rs.main() with fake stages. *_outcomes: exception or None per attempt."""
    calls = {"order": [], "slept": [], "exit": None}
    cm_left, sfdc_left = list(cm_outcomes), list(sfdc_outcomes)

    def stage(name, left):
        def fn(run_id):
            calls["order"].append(name)
            exc = left.pop(0)
            if exc:
                raise exc
        return fn

    saved = (rs._cm_stage, rs._sfdc_stage, rs._rematch, rs.metabase.sync_org_usage, rs.time.sleep)
    rs._cm_stage, rs._sfdc_stage = stage("cm", cm_left), stage("sfdc", sfdc_left)
    rs._rematch = lambda run_id: calls["order"].append("rematch")
    rs.metabase.sync_org_usage = lambda: calls["order"].append("metabase")
    rs.time.sleep = lambda secs: (calls["slept"].append(secs), calls["order"].append("wait"))
    try:
        rs.main()
    except SystemExit as e:
        calls["exit"] = e.code
    finally:
        rs._cm_stage, rs._sfdc_stage, rs._rematch, rs.metabase.sync_org_usage, rs.time.sleep = saved
    with session_scope() as s:
        latest = s.query(SyncRun).order_by(SyncRun.id.desc()).first()
        calls["status"], calls["error"] = latest.status, latest.error or ""
    return calls


transient = cm_mod.ChartMogulTransientError("Gave up on /customers after 9 attempts")
with session_scope() as s:   # a run killed mid-way (e.g. its deployment was replaced)
    s.add(SyncRun(status="running", started_at=datetime(2026, 11, 8, 19, 5)))
c = run_main([transient, None])
with session_scope() as s:
    killed = s.query(SyncRun).filter(SyncRun.started_at == datetime(2026, 11, 8, 19, 5)).one()
    check("next sync marks a killed run as interrupted", (killed.status, killed.error[:11]), ("failed", "Interrupted"))
check("CM down once: Salesforce + Metabase run first, then wait, retry CM, re-match",
      (c["order"], c["status"], c["exit"]),
      (["cm", "sfdc", "metabase", "wait", "cm", "rematch"], "success", None))
c = run_main([transient, transient])
check("CM down twice: partial (Salesforce data still committed), exit 1",
      (c["order"].count("cm"), c["order"].count("sfdc"), c["status"], c["exit"], c["error"][:10]),
      (2, 1, "partial", 1, "ChartMogul"))
c = run_main([ValueError("bug")])
check("CM bug: no retry, Salesforce still runs, partial",
      (c["order"], c["status"], c["exit"]), (["cm", "sfdc", "metabase"], "partial", 1))
c = run_main([None], [RuntimeError("SFDC down")])
check("Salesforce fails, CM ok: partial, no wait", (c["order"], c["status"], c["exit"]),
      (["cm", "sfdc", "metabase"], "partial", 1))
c = run_main([ValueError("a")], [RuntimeError("b")])
check("both fail: failed", (c["status"], c["exit"]), ("failed", 1))
check("a partial run still refreshes the dashboard cache",
      data_mod.REFRESHED, ("success", "partial"))

print("\nincremental ChartMogul refresh:")
os.environ["SYNC_FULL"] = "1"
s_ = main.SessionLocal()
check("SYNC_FULL=1 forces a full refresh", rs._cm_mode(s_)[0], "full")
os.environ.pop("SYNC_FULL")
saved_wd = rs.FULL_WEEKDAY
rs.FULL_WEEKDAY = datetime.now(timezone.utc).weekday()
check("weekly full refresh on SYNC_FULL_WEEKDAY", rs._cm_mode(s_)[:3:2], ("full", "weekly full refresh"))
rs.FULL_WEEKDAY = -1
with session_scope() as s:
    s.add(SyncRun(status="partial", started_at=datetime(2026, 11, 7, 12, 0),
                  cm_mode="incremental", cm_completed_at=datetime(2026, 11, 7, 12, 2)))
mode, since, _ = rs._cm_mode(s_)
check("otherwise incremental, since the last ChartMogul refresh",
      (mode, since.isoformat()), ("incremental", "2026-11-07T12:00:00+00:00"))
s_.close()
rs.FULL_WEEKDAY = saved_wd

with session_scope() as s:
    for u, name in (("cus_inc_a", "Inc A old"), ("cus_inc_b", "Inc B")):
        s.add(Customer(cm_uuid=u, name=name, status="Active", mrr_cents=100))
        s.add(Subscription(cm_uuid=f"sub_{u}", customer_uuid=u, plan_name="Old Plan", product="Old Plan",
                           is_current=True, mrr_cents=100))
        s.add(Activity(customer_uuid=u, date=datetime(2025, 1, 1), type="new_biz", mrr_cents=100))
acme_subs_before = None
with session_scope() as s:
    acme_subs_before = s.query(Subscription).filter(Subscription.customer_uuid == ACME).count()
fake_raw = {"cus_inc_a": {"uuid": "cus_inc_a", "name": "Inc A new", "status": "Active", "mrr": 500},
            "cus_inc_b": {"uuid": "cus_inc_b", "name": "Inc B", "status": "Lead"},
            "cus_inc_new": {"uuid": "cus_inc_new", "name": "Brand New", "status": "Active", "mrr": 900},
            "cus_inc_gone": None}
fake_details = {"cus_inc_a": ([{"uuid": "sub_a2", "plan": "AI Scribe", "status": "active", "mrr": 500}],
                              [{"date": "2026-11-07", "type": "expansion", "activity-mrr": 500}]),
                "cus_inc_new": ([{"uuid": "sub_new", "plan": "Group Plan - X", "status": "active", "mrr": 900}], [])}
asked = {}
saved_cm = (cm_mod.iter_activities_since, cm_mod.fetch_customer, cm_mod.map_customer_details)
cm_mod.iter_activities_since = lambda start: (asked.__setitem__("start", start) or
                                              [{"customer-uuid": u} for u in fake_raw] + [{"customer-uuid": "cus_inc_a"}])
cm_mod.fetch_customer = lambda u: fake_raw[u]
cm_mod.map_customer_details = lambda uuids: ((u, *fake_details[u]) for u in uuids)
try:
    with session_scope() as s:
        n = rs.sync_chartmogul_incremental(s, [], datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))
finally:
    cm_mod.iter_activities_since, cm_mod.fetch_customer, cm_mod.map_customer_details = saved_cm
check("reads activity since the last refresh minus the overlap", asked["start"], "2026-11-05T12:00:00Z")
check("refreshed customers / subscriptions / activities", n, (2, 2, 1))
with session_scope() as s:
    a_ = s.get(Customer, "cus_inc_a")
    check("changed customer replaced (name, subscriptions)",
          (a_.name, [x.plan_name for x in s.query(Subscription).filter(Subscription.customer_uuid == "cus_inc_a")]),
          ("Inc A new", ["AI Scribe"]))
    check("customer no longer Active/Cancelled, or deleted, is removed",
          (s.get(Customer, "cus_inc_b"), s.query(Subscription).filter(Subscription.customer_uuid == "cus_inc_b").count(),
           s.get(Customer, "cus_inc_gone")), (None, 0, None))
    check("new customer added", s.get(Customer, "cus_inc_new").name, "Brand New")
    check("unchanged customers untouched",
          s.query(Subscription).filter(Subscription.customer_uuid == ACME).count(), acme_subs_before)
s_ = main.SessionLocal()
try:
    rs._clear_sfdc(s_)
    check("Salesforce stage clears accounts but keeps customers (unlinked)",
          (s_.query(SfdcAccount).count(), s_.query(Customer).filter(Customer.sfdc_account_id.isnot(None)).count(),
           s_.query(Customer).count() > 0), (0, 0, True))
finally:
    s_.rollback()
    s_.close()

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
raise SystemExit(1 if failures else 0)
