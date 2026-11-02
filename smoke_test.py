"""Smoke test: SQLite DB, seeded rows, every route, auth on and off.

    python smoke_test.py
"""
import base64
import os
import tempfile
from datetime import date, datetime

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(), "smoke.db"
).replace("\\", "/")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402
from app.db import (  # noqa: E402
    Customer, SfdcAccount, SfdcContact, SfdcContract, Subscription, SyncRun, init_db,
    session_scope,
)

init_db()

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
    s.add(Customer(cm_uuid="cus_uuid_matched", name="Acme Health", email="ap@acme.test",
                   status="Active", customer_since=datetime(2024, 1, 1), mrr_cents=250000,
                   currency="USD", stripe_customer_id="cus_matched", healthie_org_id="12345",
                   sfdc_account_id="001000000000001AAA", match_method="stripe_id"))
    s.add(Subscription(cm_uuid="sub_uuid_1", customer_uuid="cus_uuid_matched",
                       plan_name="Group Plan - Acme", product="Group Plan",
                       is_current=True, external_id="sub_stripe_1", status="active",
                       quantity=10, mrr_cents=250000, currency="USD", billing_cycle="month",
                       start_date=datetime(2024, 1, 1)))
    s.add(Customer(cm_uuid="cus_uuid_unmatched", name="Orphan Clinic",
                   email="billing@orphan.test", status="Active", mrr_cents=50000,
                   currency="USD", stripe_customer_id="cus_orphan", match_method="unmatched"))
    s.add(SfdcAccount(id="001000000000002AAA", name="Gone Clinic",
                      stripe_customer_id="cus_gone", owner_name="Rep"))
    s.add(SfdcContact(id="003000000000003AAA", account_id="001000000000002AAA",
                      name="Old Admin", email="admin@gone.test", user_active=True))
    s.add(Customer(cm_uuid="cus_uuid_gone", name="Gone Clinic", status="Cancelled",
                   mrr_cents=0, stripe_customer_id="cus_gone",
                   sfdc_account_id="001000000000002AAA", match_method="stripe_id"))
    s.add(Subscription(cm_uuid="sub_uuid_2", customer_uuid="cus_uuid_gone",
                       plan_name="Group Plan - Gone", product="Group Plan", is_current=False,
                       status="cancelled", quantity=3, mrr_cents=0,
                       start_date=datetime(2022, 1, 1), end_date=datetime(2024, 6, 30)))
    s.add(SyncRun(finished_at=datetime.now(), status="success", customers_synced=2,
                  subscriptions_synced=1, accounts_synced=1, contacts_synced=1,
                  matched=1, unmatched=1))

ROUTES = [
    "/", "/?q=acme", "/?match=matched", "/?match=unmatched", "/?plan=Group+Plan&status=",
    "/customer/cus_uuid_matched", "/customer/cus_uuid_unmatched",
    "/api/products", "/api/account/001000000000001AAA", "/health",
    "/products", "/product?product=Group+Plan", "/product?product=Group+Plan&view=former",
    "/product?product=Group+Plan&view=all", "/product?product=Nonexistent",
    "/product/users.csv?product=Group+Plan", "/product/users.csv?product=Group+Plan&view=former",
]

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got} (want {want})")
    if not ok:
        failures.append(label)


client = TestClient(main.app)

print("auth bypassed (ALLOW_INSECURE_NO_AUTH=1, local dev):")
os.environ.pop("SESSION_SECRET", None)
os.environ["ALLOW_INSECURE_NO_AUTH"] = "1"
main.DASH_USER = main.DASH_PASS = ""
for route in ROUTES:
    check(route, client.get(route).status_code, 200)
check("/api/account/<missing> -> 404", client.get("/api/account/001xxx").status_code, 404)

products = client.get("/api/products").json()
check("/api/products rollup", products, [{"product": "Group Plan", "current": 1, "former": 1,
                                          "mrr_cents": 250000, "seats": 10,
                                          "active_users": 1}])
account = client.get("/api/account/001000000000001AAA").json()
check("account has 2 contacts", len(account["contacts"]), 2)
check("account has 1 customer", len(account["customers"]), 1)
check("customer has 1 product", len(account["customers"][0]["products"]), 1)
check("account has 1 contract", len(account["contracts"]), 1)
check("contract value prefers TCV", account["contracts"][0]["value"], 60000)

csv_lines = client.get("/product/users.csv?product=Group+Plan").text.strip().splitlines()
check("users.csv: header + 1 active user", len(csv_lines), 2)
check("users.csv row has email + phone", "jane@acme.test" in csv_lines[1]
      and "555-0100" in csv_lines[1], True)
page = client.get("/product?product=Group+Plan").text
check("product page shows contract value", "$60,000" in page, True)
check("product page excludes inactive user", "gone@acme.test" in page, False)
check("current view excludes former customer", "Gone Clinic" in page, False)
former = client.get("/product?product=Group+Plan&view=former").text
check("former view lists former customer", "Gone Clinic" in former, True)
check("former view shows end date", "2024-06-30" in former, True)
check("former view excludes current customer", "Acme Health" in former, False)
fcsv = client.get("/product/users.csv?product=Group+Plan&view=former").text
check("former CSV has former user", "admin@gone.test" in fcsv and ",former," in fcsv, True)
check("unmatched list shows any status", "Gone Clinic" in client.get("/?status=").text, True)

print("\nfails closed (no SESSION_SECRET, no insecure override):")
os.environ.pop("ALLOW_INSECURE_NO_AUTH")
check("/products -> 503", client.get("/products").status_code, 503)
check("/login -> 503 page", client.get("/login").status_code, 503)
check("/health still public", client.get("/health").status_code, 200)

print("\nGoogle sign-in enabled:")
os.environ["SESSION_SECRET"] = "test-secret"
main.DASH_USER, main.DASH_PASS = "dashuser", "dashpass"
basic = {"Authorization": "Basic " + base64.b64encode(b"dashuser:dashpass").decode()}
bad = {"Authorization": "Basic " + base64.b64encode(b"dashuser:wrong").decode()}
anon = TestClient(main.app)

for route in ROUTES:
    r = anon.get(route, follow_redirects=False)
    if route == "/health":
        want = 200
    elif route.startswith("/api/") or ".csv" in route:
        want = 401
    else:
        want = 303
    check(f"signed out {route}", r.status_code, want)
r = anon.get("/product?product=Group+Plan&view=former", follow_redirects=False)
check("redirect keeps destination", r.headers.get("location"),
      "/login?next=%2Fproduct%3Fproduct%3DGroup%2BPlan%26view%3Dformer")
check("login page renders Google button",
      'data-client_id="' + auth.client_id() in anon.get("/login").text, True)
check("Basic auth works for /api/*", anon.get("/api/products", headers=basic).status_code, 200)
check("Basic auth NOT accepted for pages",
      anon.get("/products", headers=basic, follow_redirects=False).status_code, 303)
check("Basic auth NOT accepted for CSV",
      anon.get("/product/users.csv?product=Group+Plan", headers=basic).status_code, 401)
check("wrong Basic password", anon.get("/api/products", headers=bad).status_code, 401)

# Google token verification is network-bound; stub it and test the session flow.
real_verify = auth.verify_google_credential
auth.verify_google_credential = lambda cred: (
    {"email": "pm@gethealthie.com", "name": "PM"} if cred == "good"
    else (_ for _ in ()).throw(ValueError("Use your @gethealthie.com Google account.")))
user = TestClient(main.app)
fetch = {"X-Requested-With": "fetch"}
check("POST /auth/google without fetch header -> 403",
      user.post("/auth/google", json={"credential": "good"}).status_code, 403)
check("wrong-domain account -> 403",
      user.post("/auth/google", json={"credential": "bad"}, headers=fetch).status_code, 403)
check("good account -> 200",
      user.post("/auth/google", json={"credential": "good"}, headers=fetch).status_code, 200)
for route in ROUTES:
    check(f"signed in {route}", user.get(route).status_code, 200)
check("header shows signed-in email", "pm@gethealthie.com" in user.get("/products").text, True)
check("login redirects signed-in user to next",
      user.get("/login?next=/products", follow_redirects=False).headers.get("location"),
      "/products")
check("open redirect blocked", user.get("/login?next=//evil.com",
                                        follow_redirects=False).headers.get("location"),
      "/products")
user.post("/logout")
check("after logout -> redirect to login",
      user.get("/products", follow_redirects=False).status_code, 303)
auth.verify_google_credential = real_verify

print("\ndomain check (real function):")
check("gethealthie.com allowed", auth.email_allowed("a@gethealthie.com"), True)
check("lookalike domain refused", auth.email_allowed("a@gethealthie.com.evil.com"), False)
check("gmail refused", auth.email_allowed("a@gmail.com"), False)

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
raise SystemExit(1 if failures else 0)
