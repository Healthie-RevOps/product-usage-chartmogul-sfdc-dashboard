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

from app import main  # noqa: E402
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
                       plan_name="Group Plan", external_id="sub_stripe_1", status="active",
                       quantity=10, mrr_cents=250000, currency="USD", billing_cycle="month",
                       start_date=datetime(2024, 1, 1)))
    s.add(Customer(cm_uuid="cus_uuid_unmatched", name="Orphan Clinic",
                   email="billing@orphan.test", status="Active", mrr_cents=50000,
                   currency="USD", stripe_customer_id="cus_orphan", match_method="unmatched"))
    s.add(SyncRun(finished_at=datetime.now(), status="success", customers_synced=2,
                  subscriptions_synced=1, accounts_synced=1, contacts_synced=1,
                  matched=1, unmatched=1))

ROUTES = [
    "/", "/?q=acme", "/?match=matched", "/?match=unmatched", "/?plan=Group+Plan",
    "/customer/cus_uuid_matched", "/customer/cus_uuid_unmatched",
    "/api/products", "/api/account/001000000000001AAA", "/health",
    "/products", "/product?plan=Group+Plan", "/product?plan=Nonexistent",
    "/product/users.csv?plan=Group+Plan",
]

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got} (want {want})")
    if not ok:
        failures.append(label)


client = TestClient(main.app)

print("auth disabled (DASH_USER/DASH_PASS unset):")
main.DASH_USER = main.DASH_PASS = ""
for route in ROUTES:
    check(route, client.get(route).status_code, 200)
check("/api/account/<missing> -> 404", client.get("/api/account/001xxx").status_code, 404)

products = client.get("/api/products").json()
check("/api/products rollup", products, [{"plan": "Group Plan", "customers": 1,
                                          "mrr_cents": 250000}])
account = client.get("/api/account/001000000000001AAA").json()
check("account has 2 contacts", len(account["contacts"]), 2)
check("account has 1 customer", len(account["customers"]), 1)
check("customer has 1 product", len(account["customers"][0]["products"]), 1)
check("account has 1 contract", len(account["contracts"]), 1)
check("contract value prefers TCV", account["contracts"][0]["value"], 60000)

csv_lines = client.get("/product/users.csv?plan=Group+Plan").text.strip().splitlines()
check("users.csv: header + 1 active user", len(csv_lines), 2)
check("users.csv row has email + phone", "jane@acme.test" in csv_lines[1]
      and "555-0100" in csv_lines[1], True)
page = client.get("/product?plan=Group+Plan").text
check("product page shows contract value", "$60,000" in page, True)
check("product page excludes inactive user", "gone@acme.test" in page, False)

print("\nauth enabled:")
main.DASH_USER, main.DASH_PASS = "dashuser", "dashpass"
good = {"Authorization": "Basic " + base64.b64encode(b"dashuser:dashpass").decode()}
bad = {"Authorization": "Basic " + base64.b64encode(b"dashuser:wrong").decode()}

for route in ROUTES:
    want = 200 if route == "/health" else 401
    check(f"no creds {route}", client.get(route).status_code, want)
for route in ROUTES:
    check(f"good creds {route}", client.get(route, headers=good).status_code, 200)
check("wrong password", client.get("/", headers=bad).status_code, 401)
check("malformed base64", client.get("/", headers={"Authorization": "Basic !!!"}).status_code, 401)
check("wrong scheme", client.get("/", headers={"Authorization": "Bearer tok"}).status_code, 401)
check("WWW-Authenticate header",
      client.get("/").headers.get("www-authenticate"), 'Basic realm="dashboard"')

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
raise SystemExit(1 if failures else 0)
