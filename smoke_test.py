"""Smoke test: SQLite DB, seeded rows, every route, exports, history, auth on and off.

    python smoke_test.py
"""
import base64
import csv
import io
import json
import os
import tempfile
from datetime import date, datetime

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(), "smoke.db"
).replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402
from app.db import (  # noqa: E402
    Activity, Customer, ProductMrr, SfdcAccount, SfdcContact, SfdcContract, Subscription,
    SyncRun, init_db, session_scope,
)

init_db()

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
    for month, mrr in ((date(2023, 12, 31), 0), (date(2024, 1, 31), 200000),
                       (date(2025, 6, 30), 265000), (date(2025, 12, 31), 265000)):
        s.add(ProductMrr(product="Group Plan", month=month, mrr_cents=mrr))
        s.add(ProductMrr(product="Plus Plan", month=month, mrr_cents=mrr // 4))
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
check("old /products redirects", (r.status_code, r.headers.get("location")), (301, "/"))
r = client.get("/product?product=Group+Plan&view=former", follow_redirects=False)
check("old /product redirects to explorer",
      r.headers.get("location"), "/?product=Group%20Plan&view=former")

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
ex = client.get("/api/explore?q=multi").json()
check("search", [r["uuid"] for r in ex["rows"]], [MULTI])
ex = client.get("/api/explore?product=Nonexistent").json()
check("unknown product -> empty", ex["rows"], [])

tr = client.get("/api/trend?product=Group+Plan&product=Plus+Plan").json()
check("trend starts at first month with revenue or customers", tr["months"][0], "2023-12-31")
check("trend total MRR = sum of products", tr["mrr"][-1], 265000 + 265000 // 4)
check("per-product series for 2 products", [s["product"] for s in tr["series"]], ["Group Plan", "Plus Plan"])
check("customers over time (Dec 2023: Orphan, Gone)", tr["customers"][0], 2)
check("customers over time (Jan 2024: Acme, Orphan, Gone)", tr["customers"][1], 3)
check("customers over time (Dec 2025: Acme, Orphan, Multi)", tr["customers"][-1], 3)

hist = client.get(f"/api/customer/{ACME}/history").json()
check("history events in date order", [e["type"] for e in hist["events"]], ["new_biz", "expansion", "contraction"])
check("history segments incl. ended", sorted((s["product"], s["current"]) for s in hist["segments"]),
      [("AI Scribe", False), ("Group Plan", True)])
page = client.get("/customer/" + ACME).text
check("customer page shows change log", "removed AI Scribe" in page, True)
check("customer page escapes descriptions", "<b>Group</b>" in page, False)
check("customer page has contract value", "$60,000" in page, True)
check("customer page branded", "healthie-mark.png" in page and "Product Explorer" in page, True)

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

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
raise SystemExit(1 if failures else 0)
