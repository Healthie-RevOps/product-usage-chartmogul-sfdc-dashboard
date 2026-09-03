"""Dashboard web service. Start command:  uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
import base64
import binascii
import os
import secrets
from collections import defaultdict

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import joinedload

from app.db import Customer, SfdcAccount, Subscription, SyncRun, init_db, SessionLocal

app = FastAPI(title="Healthie ChartMogul x SFDC")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

SFDC_DOMAIN = os.environ.get("SFDC_DOMAIN", "https://healthie.my.salesforce.com")
LIGHTNING_BASE = SFDC_DOMAIN.replace(".my.salesforce.com", ".lightning.force.com")


DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASS = os.environ.get("DASH_PASS", "")
PUBLIC_PATHS = {"/health"}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic auth over everything but /health. No creds configured = open (local dev)."""
    if not (DASH_USER and DASH_PASS) or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    unauthorized = Response(
        status_code=401,
        content="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
    )

    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return unauthorized
    try:
        user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return unauthorized

    ok_user = secrets.compare_digest(user, DASH_USER)
    ok_pass = secrets.compare_digest(password, DASH_PASS)
    if not (ok_user and ok_pass):
        return unauthorized

    return await call_next(request)


@app.on_event("startup")
def startup():
    init_db()


def _money(cents, currency="USD"):
    symbol = "$" if currency in (None, "USD") else f"{currency} "
    return f"{symbol}{(cents or 0) / 100:,.0f}"


templates.env.filters["money"] = _money


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
            .options(joinedload(Customer.subscriptions), joinedload(Customer.sfdc_account))
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
            query = query.join(Subscription).filter(Subscription.plan_name == plan)

        customers = query.order_by(desc(Customer.mrr_cents)).limit(500).all()

        plans = [p[0] for p in session.query(Subscription.plan_name)
                 .filter(Subscription.plan_name.isnot(None)).distinct()
                 .order_by(Subscription.plan_name)]
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
            .options(joinedload(Customer.subscriptions),
                     joinedload(Customer.sfdc_account).joinedload(SfdcAccount.contacts))
            .get(cm_uuid)
        )
        return templates.TemplateResponse(request, "customer.html", {
            "c": cust, "lightning_base": LIGHTNING_BASE,
        })
    finally:
        session.close()


@app.get("/api/products")
def api_products():
    """Product mix rollup: per plan — customer count and MRR."""
    session = SessionLocal()
    try:
        rows = (
            session.query(
                Subscription.plan_name,
                func.count(func.distinct(Subscription.customer_uuid)),
                func.sum(Subscription.mrr_cents),
            )
            .join(Customer, Customer.cm_uuid == Subscription.customer_uuid)
            .filter(Customer.status == "Active")
            .group_by(Subscription.plan_name)
            .order_by(desc(func.sum(Subscription.mrr_cents)))
            .all()
        )
        return JSONResponse([
            {"plan": r[0], "customers": r[1], "mrr_cents": int(r[2] or 0)} for r in rows
        ])
    finally:
        session.close()


@app.get("/api/account/{sfdc_account_id}")
def api_account(sfdc_account_id: str):
    """Everything we know for one SFDC account — usable from other tooling/skills."""
    session = SessionLocal()
    try:
        acct = (
            session.query(SfdcAccount)
            .options(joinedload(SfdcAccount.customers).joinedload(Customer.subscriptions),
                     joinedload(SfdcAccount.contacts))
            .get(sfdc_account_id)
        )
        if not acct:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "account": {"id": acct.id, "name": acct.name, "owner": acct.owner_name,
                        "healthie_org_id": acct.healthie_org_id},
            "contacts": [{"name": c.name, "email": c.email, "title": c.title}
                         for c in acct.contacts],
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
