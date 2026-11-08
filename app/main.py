"""Dashboard web service. Start command:  uvicorn app.main:app --host 0.0.0.0 --port $PORT"""
import base64
import binascii
import csv
import io
import os
import re
import secrets
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from app import auth, data, deals, enterprise, quality
from app.db import (
    Customer, HealthieOrgUsage, SfdcAccount, SfdcContract, SyncRun, init_db, SessionLocal,
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
PUBLIC_PREFIXES = ("/static/",)  # logos, CSS, JS: nothing sensitive, needed by /login


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
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
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
    return "/"


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


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
          name="static")


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


def _iso(d):
    return d.isoformat() if d else None


def _page(request, name, ctx, session, status_code=200):
    ctx = {"user": auth.current_user(request), "insecure": auth.insecure(),
           "sync": data.sync_status(session), "lightning_base": LIGHTNING_BASE, **ctx}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def explorer(request: Request):
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        return _page(request, "explorer.html", {"products": snap.products}, session)
    finally:
        session.close()


@app.get("/enterprise", response_class=HTMLResponse)
def enterprise_page(request: Request):
    session = SessionLocal()
    try:
        return _page(request, "enterprise.html", {
            "addons": [{"key": a["key"], "label": a["label"]} for a in enterprise.ADDONS],
        }, session)
    finally:
        session.close()


@app.get("/deals", response_class=HTMLResponse)
def deals_page(request: Request):
    session = SessionLocal()
    try:
        return _page(request, "deals.html", {}, session)
    finally:
        session.close()


@app.get("/quality", response_class=HTMLResponse)
def quality_page(request: Request):
    session = SessionLocal()
    try:
        return _page(request, "quality.html", {"rules": quality.RULES}, session)
    finally:
        session.close()


@app.get("/customer/{cm_uuid}", response_class=HTMLResponse)
def customer_detail(request: Request, cm_uuid: str):
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        c = snap.customers.get(cm_uuid)
        if not c:
            return _page(request, "customer.html", {"c": None}, session, status_code=404)
        acct = snap.accounts.get(c["account_id"]) if c["account_id"] else None
        contracts = (session.query(SfdcContract)
                     .filter(SfdcContract.account_id == c["account_id"])
                     .order_by(desc(SfdcContract.close_date)).all() if acct else [])
        history = data.customer_history(session, snap, cm_uuid)
        contacts = data.contacts_for(session, snap, [cm_uuid], active_only=False)
        return _page(request, "customer.html", {
            "c": c, "acct": acct, "contracts": contracts, "history": history,
            "contacts": contacts,
        }, session)
    finally:
        session.close()


# Old URLs from the first version of the dashboard.
@app.get("/products")
def old_products():
    return RedirectResponse("/", status_code=301)


@app.get("/product")
def old_product(product: str = Query(""), view: str = Query("current")):
    return RedirectResponse(f"/?product={quote(product)}&view={quote(view)}", status_code=301)


# ---------------------------------------------------------------- explorer API

class Spec(BaseModel):
    """A selection: filters, optionally narrowed to explicit customers / contacts."""
    products: list[str] = []
    view: str = "current"
    match: str = "all"
    q: str = ""
    customer_uuids: list[str] | None = None   # None = every customer matching the filters
    contact_ids: list[str] | None = None      # None = every contact matching below
    active_only: bool = True
    role: str = "all"                          # all | admins
    contact_q: str = ""
    include_partner: bool = False             # "Non EHR MRR: ..." revenue buckets


def _spec_customers(snap, spec: Spec) -> list[dict]:
    rows = data.customer_rows(snap, spec.products, spec.view, spec.match, spec.q,
                              spec.include_partner)
    if spec.customer_uuids is not None:
        keep = set(spec.customer_uuids)
        rows = [r for r in rows if r["uuid"] in keep]
    return rows


ROW_LIMIT = 2000
CONTACT_LIMIT = 3000


@app.get("/api/products")
def api_products():
    """Product rollup: per product family - current/former customers, MRR, seats."""
    session = SessionLocal()
    try:
        return JSONResponse(data.snapshot(session).products)
    finally:
        session.close()


@app.get("/api/explore")
def api_explore(product: list[str] = Query([]), view: str = "current", match: str = "all",
                q: str = "", include_partner: bool = False):
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        rows = data.customer_rows(snap, product, view, match, q, include_partner)
        return JSONResponse({"totals": data.totals(snap, rows, product, include_partner),
                             "rows": rows[:ROW_LIMIT], "limit": ROW_LIMIT})
    finally:
        session.close()


@app.get("/api/trend")
def api_trend(product: list[str] = Query([]), include_partner: bool = False):
    session = SessionLocal()
    try:
        return JSONResponse(data.product_trend(session, data.snapshot(session), product,
                                               include_partner))
    finally:
        session.close()


@app.post("/api/contacts")
def api_contacts(spec: Spec):
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        uuids = [r["uuid"] for r in _spec_customers(snap, spec)]
        rows = data.contacts_for(session, snap, uuids, spec.active_only, spec.role,
                                 spec.contact_q)
        return JSONResponse({"total": len(rows), "rows": rows[:CONTACT_LIMIT],
                             "limit": CONTACT_LIMIT, "customers": len(uuids)})
    finally:
        session.close()


@app.get("/api/customer/{cm_uuid}/history")
def api_customer_history(cm_uuid: str):
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        if cm_uuid not in snap.customers:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(data.customer_history(session, snap, cm_uuid))
    finally:
        session.close()


# ---------------------------------------------------------------- CSV exports
# Form POSTs (a `spec` JSON field) so a selection of thousands of IDs fits. The session
# cookie is SameSite=Lax, so other sites can't trigger these with the user's session.

_FORMULA_START = re.compile(r"^(?:[=@\t\r]|[+-](?![\d\s(]))")


def _csv_cell(v):
    """Neutralize spreadsheet formulas in text from SFDC/ChartMogul (=HYPERLINK(...) etc.).
    Leaves numbers and phone-like values ("+1 555...", "-3") alone."""
    if v is None:
        return ""
    if isinstance(v, str) and _FORMULA_START.match(v):
        return "'" + v
    return v


def _csv_response(header: list[str], rows, filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_csv_cell(v) for v in r])
    return Response(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


def _read_spec(raw: str) -> Spec:
    try:
        return Spec.model_validate_json(raw or "{}")
    except ValueError as exc:
        raise HTTPException(400, f"Bad selection: {exc}")


def _slug(spec: Spec) -> str:
    base = "_".join(spec.products[:3]) if spec.products else "all_products"
    if len(spec.products) > 3:
        base += f"_plus{len(spec.products) - 3}"
    return "".join(ch if ch.isalnum() else "_" for ch in base)[:60] + f"_{spec.view}"


@app.post("/export/contacts.csv")
def export_contacts(spec: str = Form("{}")):
    s = _read_spec(spec)
    session = SessionLocal()
    try:
        snap = data.snapshot(session)
        customers = {r["uuid"]: r for r in _spec_customers(snap, s)}
        rows = data.contacts_for(session, snap, list(customers), s.active_only, s.role,
                                 s.contact_q, contact_ids=s.contact_ids)
        products = ", ".join(s.products)
        return _csv_response(
            ["name", "email", "phone", "mobile_phone", "title", "healthie_user_active",
             "user_type", "org_owner", "org_admin", "account", "customer", "customer_status",
             "products_filter", "customer_products", "customer_product_mrr_usd",
             "customer_total_mrr_usd", "sfdc_contact_id", "sfdc_account_id", "contact_url"],
            ([c["name"], c["email"], c["phone"], c["mobile"], c["title"], c["active"],
              c["user_type"], c["owner"], c["admin"], c["account"], c["customer"],
              c["customer_status"], products,
              "; ".join(p["product"] + ("" if p["current"] else " (former)")
                        for p in customers[c["customer_uuid"]]["products"]),
              customers[c["customer_uuid"]]["product_mrr"] / 100,
              customers[c["customer_uuid"]]["mrr"] / 100,
              c["id"], c["account_id"],
              f"{LIGHTNING_BASE}/lightning/r/Contact/{c['id']}/view"] for c in rows),
            f"contacts_{_slug(s)}_{date.today().isoformat()}.csv")
    finally:
        session.close()


@app.post("/export/customers.csv")
def export_customers(spec: str = Form("{}")):
    s = _read_spec(spec)
    session = SessionLocal()
    try:
        rows = _spec_customers(data.snapshot(session), s)
        return _csv_response(
            ["customer", "billing_name", "email", "status", "products", "product_status", "product_mrr_usd",
             "seats", "total_mrr_usd", "since", "ended", "sfdc_account", "owner", "health",
             "active_users", "contacts", "contract_value_usd", "contract_end",
             "healthie_org_id", "providers_active", "providers_total", "provider_admins",
             "last_provider_activity", "clients_active", "clients_total",
             "chartmogul_uuid", "sfdc_account_id"],
            ([r["name"], r["billing_name"], r["email"], r["status"],
              "; ".join(p["product"] + ("" if p["current"] else " (former)")
                        for p in r["products"]),
              "current" if r["current"] else "former", r["product_mrr"] / 100, r["seats"],
              r["mrr"] / 100, r["since"], r["ended"],
              *((r["account"]["name"], r["account"]["owner"], r["account"]["health"],
                 r["account"]["active_users"], r["account"]["contacts"],
                 r["account"]["contract_value"], r["account"]["contract_end"],
                 r["account"]["org_id"], r["account"]["hl_active_providers"],
                 r["account"]["hl_providers"], r["account"]["hl_admins"],
                 r["account"]["hl_last_activity"], r["account"]["hl_active_clients"],
                 r["account"]["hl_clients"])
                if r["account"] else ("",) * 14),
              r["uuid"], r["account"]["id"] if r["account"] else ""] for r in rows),
            f"customers_{_slug(s)}_{date.today().isoformat()}.csv")
    finally:
        session.close()


# ---------------------------------------------------------------- enterprise add-ons

class EntSpec(BaseModel):
    """Enterprise view filters. addons: ["ai_scribe:none", ...]; account_ids narrows to
    explicitly selected accounts (None = every account matching the filters)."""
    paying: bool = True
    status: str = ""
    csm: str = ""
    segment: str = ""
    impl: str = ""
    q: str = ""
    addons: list[str] = []
    account_ids: list[str] | None = None


def _ent_rows(session, s: EntSpec) -> tuple[list[dict], list[dict]]:
    base = enterprise.base_rows(session, data.snapshot(session))
    rows = enterprise.filter_rows(base, s.paying, s.status, s.csm, s.segment, s.impl, s.q,
                                  enterprise.parse_addon_filters(s.addons), s.account_ids)
    return base, rows


@app.get("/api/enterprise")
def api_enterprise(paying: bool = True, status: str = "", csm: str = "", segment: str = "",
                   impl: str = "", q: str = "", addon: list[str] = Query([])):
    session = SessionLocal()
    try:
        base, rows = _ent_rows(session, EntSpec(paying=paying, status=status, csm=csm,
                                                segment=segment, impl=impl, q=q, addons=addon))
        return JSONResponse({"has_data": bool(base), "sync": data.sync_status(session),
                             "rows": rows, "summary": enterprise.summary(rows),
                             "totals": enterprise.totals(rows), "facets": enterprise.facets(base),
                             "addons": [{"key": a["key"], "label": a["label"]}
                                        for a in enterprise.ADDONS]})
    finally:
        session.close()


def _read_ent_spec(raw: str) -> EntSpec:
    try:
        return EntSpec.model_validate_json(raw or "{}")
    except ValueError as exc:
        raise HTTPException(400, f"Bad selection: {exc}")


def _cell_text(c: dict) -> str:
    if c["state"] == "has":
        return c.get("source") or "has"
    if c["state"] == "pipeline":
        return f"{c.get('stage') or 'open'}, close {c.get('close_date') or '?'}"
    if c["state"] == "lost":
        return f"{c.get('reason') or 'no reason given'}, {c.get('close_date') or '?'}"
    return c.get("note") or ""


@app.post("/export/enterprise.csv")
def export_enterprise(spec: str = Form("{}")):
    s = _read_ent_spec(spec)
    session = SessionLocal()
    try:
        _, rows = _ent_rows(session, s)
        addons = enterprise.ADDONS
        return _csv_response(
            ["account", "customer_status", "sfdc_mrr_usd", "csm", "segment",
             "implementation_status", "renewal_date", "kickoff_date", "website",
             "healthie_org_id", "healthie_org_count", "active_providers",
             "active_standard_users_sfdc", "has_active_client", "has_standard_seats",
             "has_support_seats"]
            + [f"{a['key']}_state" for a in addons] + [f"{a['key']}_detail" for a in addons]
            + ["sfdc_account_id", "sfdc_url"],
            ([r["name"], r["customer_status"], r["mrr"], r["csm"], r["segment"],
              r["implementation_status"], r["renewal_date"], r["kickoff_date"], r["website"],
              r["org_id"], r["org_count"], r["providers"], r["active_standard_users"],
              r["flags"]["has_active_client"], r["flags"]["has_standard_seats"],
              r["flags"]["has_support_seats"]]
             + [r["addons"][a["key"]]["state"] for a in addons]
             + [_cell_text(r["addons"][a["key"]]) for a in addons]
             + [r["id"], f"{LIGHTNING_BASE}/lightning/r/Account/{r['id']}/view"]
             for r in rows),
            f"enterprise_addons_{date.today().isoformat()}.csv")
    finally:
        session.close()


@app.post("/export/enterprise-contacts.csv")
def export_enterprise_contacts(spec: str = Form("{}")):
    """Providers and admins (not support staff, never clients) at the accounts in view,
    excluding anyone opted out of email."""
    s = _read_ent_spec(spec)
    session = SessionLocal()
    try:
        _, rows = _ent_rows(session, s)
        addons = enterprise.ADDONS
        items = enterprise.campaign_contacts(session, rows)
        return _csv_response(
            ["email", "name", "title", "role", "account", "csm", "segment", "sfdc_mrr_usd"]
            + [f"{a['key']}_state" for a in addons]
            + ["sfdc_contact_id", "sfdc_account_id"],
            ([x["contact"].email, x["contact"].name, x["contact"].title,
              "owner" if x["contact"].is_org_owner else "admin" if x["contact"].is_org_admin
              else "provider",
              x["account"]["name"], x["account"]["csm"], x["account"]["segment"],
              x["account"]["mrr"]]
             + [x["account"]["addons"][a["key"]]["state"] for a in addons]
             + [x["contact"].id, x["contact"].account_id] for x in items),
            f"enterprise_contacts_{date.today().isoformat()}.csv")
    finally:
        session.close()


# ---------------------------------------------------------------- win/loss

class DealSpec(BaseModel):
    record_type: str = ""
    date_from: str = ""
    date_to: str = ""
    outcome: str = ""            # "" | won | lost | open | closed
    owner: str = ""
    competitor: str = ""
    reason: str = ""
    incumbent: str = ""
    q: str = ""


def _deal_rows(session, s: DealSpec) -> tuple[list[dict], list[dict]]:
    base = deals.base_rows(session, data.snapshot(session))
    rows = deals.filter_rows(base, s.record_type, s.date_from, s.date_to, s.outcome, s.owner,
                             s.competitor, s.reason, s.incumbent, s.q)
    return base, rows


DEAL_ROW_LIMIT = 1500


@app.get("/api/deals")
def api_deals(record_type: str = "", date_from: str = "", date_to: str = "", outcome: str = "",
              owner: str = "", competitor: str = "", reason: str = "", incumbent: str = "",
              q: str = ""):
    session = SessionLocal()
    try:
        s = DealSpec(record_type=record_type, date_from=date_from, date_to=date_to,
                     outcome=outcome, owner=owner, competitor=competitor, reason=reason,
                     incumbent=incumbent, q=q)
        base, rows = _deal_rows(session, s)
        # breakdowns ignore their own filter, so clicking a competitor/reason keeps the rest visible
        comp_rows = rows if not s.competitor else _deal_rows(session, s.model_copy(update={"competitor": ""}))[1]
        reason_rows = rows if not s.reason else _deal_rows(session, s.model_copy(update={"reason": ""}))[1]
        return JSONResponse({
            "has_data": bool(base), "sync": data.sync_status(session),
            "totals": deals.totals(rows), "competitors": deals.by_competitor(comp_rows),
            "reasons": deals.by_reason(reason_rows), "facets": deals.facets(base),
            "rows": rows[:DEAL_ROW_LIMIT], "total_rows": len(rows), "limit": DEAL_ROW_LIMIT,
        })
    finally:
        session.close()


@app.post("/export/deals.csv")
def export_deals(spec: str = Form("{}")):
    try:
        s = DealSpec.model_validate_json(spec or "{}")
    except ValueError as exc:
        raise HTTPException(400, f"Bad selection: {exc}")
    session = SessionLocal()
    try:
        _, rows = _deal_rows(session, s)
        return _csv_response(
            ["deal", "account", "record_type", "type", "stage", "outcome", "close_date",
             "year1_acv_usd", "amount_usd", "competitor", "secondary_competitor", "incumbent",
             "loss_reason", "regrettable", "loss_reason_detail", "why_won", "current_situation",
             "owner", "sfdc_opportunity_id", "sfdc_url"],
            ([r["name"], r["account"], r["record_type"], r["type"], r["stage"], r["outcome"],
              r["close_date"], r["year1_acv"], r["amount"], r["competitor"], r["secondary"],
              r["incumbent"], r["reason"], r["regrettable"], r["reason_detail"], r["why_won"],
              r["situation"], r["owner"], r["id"],
              f"{LIGHTNING_BASE}/lightning/r/Opportunity/{r['id']}/view"] for r in rows),
            f"deals_{date.today().isoformat()}.csv")
    finally:
        session.close()


# ---------------------------------------------------------------- data quality

class QualitySpec(BaseModel):
    date_from: str = ""
    owner: str = ""
    issue: str = ""
    q: str = ""


def _quality(session, s: QualitySpec):
    deal_base, mismatch_base = quality.base(session, data.snapshot(session))
    scoped = quality.filter_deals(deal_base, s.date_from, s.owner, "", s.q)
    mism = quality.filter_mismatches(mismatch_base, s.owner, s.q)
    issue = s.issue if s.issue in quality.RULE_KEYS else ""
    work = [r for r in scoped if r["issues"] and (not issue or issue in r["issues"])]
    if issue and issue != "flag_mismatch":
        mism_shown = []
    else:
        mism_shown = mism
    if issue == "flag_mismatch":
        work = []
    return deal_base, mismatch_base, scoped, mism, work, mism_shown


QUALITY_ROW_LIMIT = 1500


@app.get("/api/quality")
def api_quality(date_from: str = "", owner: str = "", issue: str = "", q: str = ""):
    session = SessionLocal()
    try:
        deal_base, mismatch_base, scoped, mism, work, mism_shown = _quality(
            session, QualitySpec(date_from=date_from, owner=owner, issue=issue, q=q))
        owners = sorted({r["owner"] for r in deal_base if r["owner"]}
                        | {m["csm"] for m in mismatch_base if m["csm"]}, key=str.lower)
        return JSONResponse({
            "has_data": bool(deal_base), "sync": data.sync_status(session),
            "summary": quality.rule_summary(scoped, mism), "owners": quality.by_owner(scoped),
            "rows": work[:QUALITY_ROW_LIMIT], "total_rows": len(work), "limit": QUALITY_ROW_LIMIT,
            "mismatches": mism_shown, "facets": {"owner": owners},
        })
    finally:
        session.close()


@app.post("/export/quality.csv")
def export_quality(spec: str = Form("{}")):
    """One row per (deal, issue) plus one per account flag mismatch: a fix list to send
    to owners."""
    try:
        s = QualitySpec.model_validate_json(spec or "{}")
    except ValueError as exc:
        raise HTTPException(400, f"Bad selection: {exc}")
    session = SessionLocal()
    try:
        *_, work, mism_shown = _quality(session, s)
        rules = {r["key"]: r for r in quality.RULES}
        wanted = s.issue if s.issue in quality.RULE_KEYS else ""

        def lines():
            for r in work:
                for i in r["issues"]:
                    if wanted and i != wanted:
                        continue
                    fix = rules[i]["fix"]
                    if i == "missing_expansion_product" and r["hints"].get("expansion_product"):
                        fix += f" (looks like: {r['hints']['expansion_product']})"
                    yield [r["owner"], rules[i]["label"], fix, r["name"], r["account"],
                           r["record_type"], r["stage"], r["close_date"],
                           f"{LIGHTNING_BASE}/lightning/r/Opportunity/{r['id']}/view"]
            for m in mism_shown:
                yield [m["csm"], rules["flag_mismatch"]["label"],
                       f"{m['problem']}: {m['addon']}"
                       + (f" ({', '.join(m['products'])})" if m["products"] else ""),
                       "", m["account"], "", "", "",
                       f"{LIGHTNING_BASE}/lightning/r/Account/{m['account_id']}/view"]

        return _csv_response(["owner", "issue", "fix", "deal", "account", "record_type", "stage",
                              "close_date", "sfdc_url"], lines(),
                             f"data_quality_{date.today().isoformat()}.csv")
    finally:
        session.close()


# ---------------------------------------------------------------- account API

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
        hl = (session.get(HealthieOrgUsage, acct.healthie_org_id.strip())
              if acct.healthie_org_id else None)
        return JSONResponse({
            "account": {"id": acct.id, "name": acct.name, "owner": acct.owner_name,
                        "healthie_org_id": acct.healthie_org_id,
                        "usage": {"active_standard_users": acct.active_standard_users,
                                  "active_clients": acct.active_clients,
                                  "providers": acct.num_providers,
                                  "health_score": acct.health_score},
                        "healthie_product": ({
                            "providers": hl.providers, "active_providers": hl.active_providers,
                            "admins": hl.admins,
                            "last_provider_activity": _iso(hl.last_provider_activity),
                            "clients": hl.clients, "active_clients": hl.active_clients,
                        } if hl else None)},
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
                "products": [{"plan": s.plan_name, "product": s.product,
                              "current": s.is_current, "quantity": s.quantity,
                              "mrr_cents": s.mrr_cents, "cycle": s.billing_cycle}
                             for s in cu.subscriptions],
            } for cu in acct.customers],
        })
    finally:
        session.close()


@app.get("/health")
def health():
    return {"ok": True}
