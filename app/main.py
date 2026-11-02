"""Dashboard web service. Start command:  uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
import base64
import binascii
import csv
import io
import os
import secrets

from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import case, desc, func, or_
from sqlalchemy.orm import joinedload, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.db import (
    Customer, SfdcAccount, SfdcContact, Subscription, SyncRun, init_db, SessionLocal,
)

app = FastAPI(title="Healthie ChartMogul x SFDC", docs_url=None, redoc_url=None,
              openapi_url=None)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

SFDC_DOMAIN = os.environ.get("SFDC_DOMAIN", "https://healthie.my.salesforce.com")
LIGHTNING_BASE = SFDC_DOMAIN.replace(".my.salesforce.com", ".lightning.force.com")

# Machine access to /api/* only (scripts, Claude skills): HTTP Basic with these creds.
# People use Google sign-in; Basic auth is never accepted for the HTML pages or CSVs.
DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASS = os.environ.get("DASH_PASS", "")
PUBLIC_PATHS = {"/health", "/login", "/auth/google", "/logout"}


def _basic_ok(request: Request) -> bool:
    if not (DASH_USER and DASH_PASS):
        return False
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(user, DASH_USER) & secrets.compare_digest(password, DASH_PASS)


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """@gethealthie.com Google sign-in over everything except PUBLIC_PATHS.
    Fails closed: no SESSION_SECRET (and no ALLOW_INSECURE_NO_AUTH=1) -> 503."""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    if not auth.configured():
        return Response("Sign-in is not configured (SESSION_SECRET is missing).",
                        status_code=503)
    if auth.current_user(request):
        return await call_next(request)
    if path.startswith("/api/"):
        if _basic_ok(request):
            return await call_next(request)
        return JSONResponse({"detail": "Sign in required."}, status_code=401)
    if request.method == "GET" and not path.endswith(".csv"):
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return Response("Sign in required.", status_code=401)


# Added after the gate so it wraps it (outermost): request.session exists in the gate.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or "insecure-local-only",
    session_cookie="pud_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN")),
)


def _safe_next(target: str | None) -> str:
    """Only same-site relative paths: blocks open redirects like //evil.com."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/products"


@app.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = Query("")):
    if auth.configured() and auth.current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "unconfigured": not auth.configured(), "client_id": auth.client_id(),
        "domain": auth.allowed_domain(), "next": _safe_next(next),
    }, status_code=200 if auth.configured() else 503)


class Credential(BaseModel):
    credential: str


@app.post("/auth/google")
def auth_google(body: Credential, request: Request):
    if not auth.configured():
        raise HTTPException(503, "Sign-in is not configured.")
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Missing request header.")
    try:
        user = auth.verify_google_credential(body.credential)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    request.session["user"] = user
    return {"ok": True}


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.on_event("startup")
def startup():
    init_db()


def _money(cents, currency="USD"):
    symbol = "$" if currency in (None, "USD") else f"{currency} "
    return f"{symbol}{(cents or 0) / 100:,.0f}"


def _dollars(value):
    return "—" if value is None else f"${value:,.0f}"


def _num(value):
    return "—" if value is None else f"{value:,.0f}"


templates.env.filters["money"] = _money
templates.env.filters["dollars"] = _dollars
templates.env.filters["num"] = _num


def latest_contract(account):
    """Most recent closed-won opportunity that carries a contract value."""
    if not account:
        return None
    return next((c for c in account.contracts if c.value), None)


templates.env.globals["latest_contract"] = latest_contract


def _last_run(session):
    return session.query(SyncRun).order_by(desc(SyncRun.id)).first()


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = Query("", description="search name/email"),
    plan: str = Query(""),
    status: str = Query("Active"),
    match: str = Query(""),
):
    session = SessionLocal()
    try:
        query = (
            session.query(Customer)
            .options(selectinload(Customer.subscriptions), joinedload(Customer.sfdc_account))
        )
        if q:
            like = f"%{q}%"
            query = query.filter(or_(Customer.name.ilike(like), Customer.email.ilike(like),
                                     Customer.healthie_org_id.ilike(like)))
        if status:
            query = query.filter(Customer.status == status)
        if match == "matched":
            query = query.filter(Customer.sfdc_account_id.isnot(None))
        elif match == "unmatched":
            query = query.filter(Customer.sfdc_account_id.is_(None))
        if plan:
            query = query.join(Subscription).filter(Subscription.product == plan).distinct()

        customers = query.order_by(desc(Customer.mrr_cents)).limit(500).all()

        plans = [p[0] for p in session.query(Subscription.product)
                 .filter(Subscription.product.isnot(None)).distinct()
                 .order_by(Subscription.product)]
        statuses = [s[0] for s in session.query(Customer.status).distinct() if s[0]]

        totals = {
            "customers": session.query(func.count(Customer.cm_uuid))
                                .filter(Customer.status == "Active").scalar() or 0,
            "mrr": session.query(func.sum(Customer.mrr_cents))
                          .filter(Customer.status == "Active").scalar() or 0,
            "matched": session.query(func.count(Customer.cm_uuid))
                              .filter(Customer.sfdc_account_id.isnot(None)).scalar() or 0,
            "unmatched": session.query(func.count(Customer.cm_uuid))
                                .filter(Customer.sfdc_account_id.is_(None)).scalar() or 0,
        }

        return templates.TemplateResponse(request, "index.html", {
            "customers": customers, "plans": plans,
            "statuses": statuses, "totals": totals, "q": q, "plan": plan,
            "status": status, "match": match, "last_run": _last_run(session),
            "lightning_base": LIGHTNING_BASE,
        })
    finally:
        session.close()


@app.get("/customer/{cm_uuid}", response_class=HTMLResponse)
def customer_detail(request: Request, cm_uuid: str):
    session = SessionLocal()
    try:
        cust = (
            session.query(Customer)
            .options(selectinload(Customer.subscriptions),
                     selectinload(Customer.sfdc_account).selectinload(SfdcAccount.contacts),
                     selectinload(Customer.sfdc_account).selectinload(SfdcAccount.contracts))
            .get(cm_uuid)
        )
        return templates.TemplateResponse(request, "customer.html", {
            "c": cust, "lightning_base": LIGHTNING_BASE,
        })
    finally:
        session.close()


def _iso(d):
    return d.isoformat() if d else None


# ---------------------------------------------------------------- products
#
# A "product" is a ChartMogul plan family: "Standard Seats - Weis Markets" and
# "Standard Seats - Lean" are both "Standard Seats" (Subscription.product).
# Current customer = has a live subscription to the product today; former = had one,
# none live now (cancelled the product, or churned entirely).

VIEWS = ("current", "former", "all")


def _product_rows(session, product: str, view: str = "current"):
    """One row per customer that has ever held `product`, filtered by view."""
    # selectinload, not joinedload: joining two collections (contacts x contracts) per
    # account multiplies rows. Standard Seats (~1.9k customers) timed out at 90s that way.
    holders = (session.query(Subscription.customer_uuid)
               .filter(Subscription.product == product).distinct())
    customers = (
        session.query(Customer)
        .filter(Customer.cm_uuid.in_(holders))
        .options(selectinload(Customer.subscriptions),
                 selectinload(Customer.sfdc_account).selectinload(SfdcAccount.contacts),
                 selectinload(Customer.sfdc_account).selectinload(SfdcAccount.contracts))
        .all()
    )
    rows, seen = [], set()
    for cu in customers:
        if cu.cm_uuid in seen:
            continue
        seen.add(cu.cm_uuid)
        subs = [s for s in cu.subscriptions if s.product == product]
        current = any(s.is_current for s in subs)
        if (view == "current" and not current) or (view == "former" and current):
            continue
        starts = [s.start_date for s in subs if s.start_date]
        ends = [s.end_date for s in subs if s.end_date and not s.is_current]
        rows.append({
            "c": cu, "current": current,
            "plans": sorted({s.plan_name for s in subs if s.plan_name}),
            "product_mrr": sum(s.mrr_cents or 0 for s in subs if s.is_current),
            "seats": sum(s.quantity or 0 for s in subs if s.is_current),
            "first_start": min(starts) if starts else None,
            "ended": None if current else (max(ends) if ends else None),
        })
    rows.sort(key=lambda r: (-r["product_mrr"], -(r["c"].mrr_cents or 0),
                             -(r["ended"].timestamp() if r["ended"] else 0)))
    return rows


def _active_users(rows):
    """(contact, customer) pairs for active Healthie users on these customers' accounts.
    Org owners first, then admins, then alphabetical."""
    seen, out = set(), []
    for r in rows:
        cu = r["c"]
        if not cu.sfdc_account:
            continue
        for ct in cu.sfdc_account.contacts:
            if ct.user_active and ct.id not in seen:
                seen.add(ct.id)
                out.append((ct, cu))
    out.sort(key=lambda x: (not x[0].is_org_owner, not x[0].is_org_admin,
                            (x[0].name or "").lower()))
    return out


def _product_rollup(session):
    """Per product: current customers, former customers, current MRR, seats, active users."""
    live = Subscription.is_current.is_(True)
    per_cust = (
        session.query(
            Subscription.product.label("product"),
            Subscription.customer_uuid.label("customer_uuid"),
            func.max(case((live, 1), else_=0)).label("cur"),
            func.sum(case((live, Subscription.mrr_cents), else_=0)).label("mrr"),
            func.sum(case((live, Subscription.quantity), else_=0)).label("seats"),
        )
        .filter(Subscription.product.isnot(None))
        .group_by(Subscription.product, Subscription.customer_uuid)
        .subquery()
    )
    rows = (
        session.query(per_cust.c.product, func.sum(per_cust.c.cur), func.count(),
                      func.sum(per_cust.c.mrr), func.sum(per_cust.c.seats))
        .group_by(per_cust.c.product)
        .order_by(desc(func.sum(per_cust.c.mrr)), desc(func.count()))
        .all()
    )
    active_users = dict(
        session.query(per_cust.c.product, func.count(func.distinct(SfdcContact.id)))
        .join(Customer, Customer.cm_uuid == per_cust.c.customer_uuid)
        .join(SfdcContact, SfdcContact.account_id == Customer.sfdc_account_id)
        .filter(per_cust.c.cur == 1, SfdcContact.user_active.is_(True))
        .group_by(per_cust.c.product)
        .all()
    )
    return [{
        "product": r[0], "current": int(r[1] or 0), "former": int(r[2] - (r[1] or 0)),
        "mrr_cents": int(r[3] or 0), "seats": int(r[4] or 0),
        "active_users": active_users.get(r[0], 0),
    } for r in rows]


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    session = SessionLocal()
    try:
        return templates.TemplateResponse(request, "products.html", {
            "products": _product_rollup(session), "last_run": _last_run(session),
        })
    finally:
        session.close()


@app.get("/product", response_class=HTMLResponse)
def product_detail(request: Request, product: str = Query(...),
                   view: str = Query("current")):
    view = view if view in VIEWS else "current"
    session = SessionLocal()
    try:
        all_rows = _product_rows(session, product, "all")
        rows = [r for r in all_rows if view == "all" or r["current"] == (view == "current")]
        users = _active_users(rows)
        accounts = {r["c"].sfdc_account.id: r["c"].sfdc_account
                    for r in rows if r["c"].sfdc_account}
        contract_total = sum((latest_contract(a).value if latest_contract(a) else 0)
                             for a in accounts.values())
        totals = {
            "current": sum(1 for r in all_rows if r["current"]),
            "former": sum(1 for r in all_rows if not r["current"]),
            "shown": len(rows),
            "mrr": sum(r["product_mrr"] for r in all_rows),
            "matched": sum(1 for r in rows if r["c"].sfdc_account),
            "active_users": len(users),
            "contract_value": contract_total,
        }
        return templates.TemplateResponse(request, "product.html", {
            "product": product, "view": view, "rows": rows[:1000],
            "users": users[:500], "users_total": len(users), "totals": totals,
            "lightning_base": LIGHTNING_BASE, "last_run": _last_run(session),
        })
    finally:
        session.close()


@app.get("/product/users.csv")
def product_users_csv(product: str = Query(...), view: str = Query("current")):
    """Active users at customers of `product` (current/former/all): name, email, phone."""
    view = view if view in VIEWS else "current"
    session = SessionLocal()
    try:
        rows = _product_rows(session, product, view)
        by_cust = {r["c"].cm_uuid: r for r in rows}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["name", "email", "phone", "mobile_phone", "title", "user_type",
                    "org_owner", "org_admin", "account", "sfdc_account_id",
                    "sfdc_contact_id", "product", "product_status", "product_mrr_usd",
                    "product_ended", "customer_status"])
        for ct, cu in _active_users(rows):
            r = by_cust[cu.cm_uuid]
            w.writerow([ct.name, ct.email, ct.phone, ct.mobile_phone, ct.title, ct.user_type,
                        ct.is_org_owner, ct.is_org_admin, cu.sfdc_account.name,
                        cu.sfdc_account.id, ct.id, product,
                        "current" if r["current"] else "former", r["product_mrr"] / 100,
                        r["ended"].date().isoformat() if r["ended"] else "", cu.status])
        safe = "".join(ch if ch.isalnum() else "_" for ch in product)[:60]
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="users_{safe}_{view}.csv"'})
    finally:
        session.close()


@app.get("/api/products")
def api_products():
    """Product rollup: per product family - current/former customers, MRR, seats."""
    session = SessionLocal()
    try:
        return JSONResponse(_product_rollup(session))
    finally:
        session.close()


@app.get("/api/account/{sfdc_account_id}")
def api_account(sfdc_account_id: str):
    """Everything we know for one SFDC account — usable from other tooling/skills."""
    session = SessionLocal()
    try:
        acct = (
            session.query(SfdcAccount)
            .options(selectinload(SfdcAccount.customers).selectinload(Customer.subscriptions),
                     selectinload(SfdcAccount.contacts))
            .get(sfdc_account_id)
        )
        if not acct:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "account": {"id": acct.id, "name": acct.name, "owner": acct.owner_name,
                        "healthie_org_id": acct.healthie_org_id,
                        "usage": {"active_standard_users": acct.active_standard_users,
                                  "active_clients": acct.active_clients,
                                  "providers": acct.num_providers,
                                  "health_score": acct.health_score}},
            "contacts": [{"name": c.name, "email": c.email, "title": c.title,
                          "phone": c.phone, "mobile_phone": c.mobile_phone,
                          "healthie_user_active": c.user_active, "user_type": c.user_type,
                          "org_owner": c.is_org_owner, "org_admin": c.is_org_admin}
                         for c in acct.contacts],
            "contracts": [{"opportunity_id": k.id, "name": k.name, "value": k.value,
                           "total_contract_value": k.total_contract_value,
                           "year1_acv": k.year1_acv,
                           "close_date": _iso(k.close_date),
                           "start": _iso(k.contract_start), "end": _iso(k.contract_end)}
                          for k in acct.contracts],
            "customers": [{
                "cm_uuid": cu.cm_uuid, "name": cu.name, "status": cu.status,
                "mrr_cents": cu.mrr_cents,
                "products": [{"plan": s.plan_name, "quantity": s.quantity,
                              "mrr_cents": s.mrr_cents, "cycle": s.billing_cycle}
                             for s in cu.subscriptions],
            } for cu in acct.customers],
        })
    finally:
        session.close()


@app.get("/health")
def health():
    return {"ok": True}
