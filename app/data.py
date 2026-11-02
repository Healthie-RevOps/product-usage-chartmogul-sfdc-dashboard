"""Read model for the explorer.

The data changes once a night, so the web service keeps an in-memory snapshot of the
small tables (customers, subscriptions, account stats; ~15k / ~25k / ~13k rows) and
rebuilds it when a newer successful sync run appears. Contacts (~50k+) and activities
are queried on demand for the customers in view.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import desc, func

from app.db import (
    Activity, Customer, ProductMrr, SfdcAccount, SfdcContact, SfdcContract, Subscription,
    SyncRun,
)

VIEWS = ("current", "former", "all")
MATCHES = ("all", "matched", "unmatched")

# ChartMogul "customers" that are really revenue buckets (partner / payments income booked
# against a placeholder plan), e.g. "Non EHR MRR: Payment Processing", "Non EHR MRR: ZocDoc".
# Hidden from the explorer by default; the toggle brings them back.
PARTNER_PREFIXES = ("non ehr mrr",)


def is_partner(name: str | None) -> bool:
    return (name or "").strip().lower().startswith(PARTNER_PREFIXES)


@dataclass
class Sub:
    customer_uuid: str
    product: str | None
    plan: str | None
    current: bool
    mrr: int
    qty: int
    start: datetime | None
    end: datetime | None


@dataclass
class Snapshot:
    run_id: int | None
    customers: dict[str, dict] = field(default_factory=dict)
    accounts: dict[str, dict] = field(default_factory=dict)
    subs_by_customer: dict[str, list[Sub]] = field(default_factory=lambda: defaultdict(list))
    subs_by_product: dict[str, list[Sub]] = field(default_factory=lambda: defaultdict(list))
    customers_by_account: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    products: list[dict] = field(default_factory=list)
    partners: set[str] = field(default_factory=set)


_lock = threading.Lock()
_snap: Snapshot | None = None


def _latest_success_id(session) -> int | None:
    return session.query(func.max(SyncRun.id)).filter(SyncRun.status == "success").scalar()


def snapshot(session) -> Snapshot:
    global _snap
    run_id = _latest_success_id(session)
    if _snap is not None and _snap.run_id == run_id and run_id is not None:
        return _snap
    with _lock:
        if _snap is None or _snap.run_id != run_id or run_id is None:
            _snap = _build(session, run_id)
    return _snap


def invalidate():
    global _snap
    _snap = None


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _build(session, run_id) -> Snapshot:
    s = Snapshot(run_id=run_id)

    active_users = dict(session.query(SfdcContact.account_id, func.count())
                        .filter(SfdcContact.user_active.is_(True))
                        .group_by(SfdcContact.account_id))
    contact_counts = dict(session.query(SfdcContact.account_id, func.count())
                          .group_by(SfdcContact.account_id))
    latest: dict[str, tuple] = {}
    for c in (session.query(SfdcContract)
              .order_by(SfdcContract.account_id, desc(SfdcContract.close_date))):
        if c.account_id not in latest and c.value:
            latest[c.account_id] = (c.value, c.contract_end)
    for a in session.query(SfdcAccount):
        value, end = latest.get(a.id, (None, None))
        s.accounts[a.id] = {
            "id": a.id, "name": a.name, "owner": a.owner_name, "health": a.health_score,
            "active_clients": a.active_clients, "active_standard_users": a.active_standard_users,
            "providers": a.num_providers,
            "active_users": active_users.get(a.id, 0), "contacts": contact_counts.get(a.id, 0),
            "contract_value": value, "contract_end": end.isoformat() if end else None,
        }

    for c in session.query(Customer):
        billing = c.name or c.email or c.cm_uuid
        acct = s.accounts.get(c.sfdc_account_id) if c.sfdc_account_id else None
        if is_partner(billing):
            s.partners.add(c.cm_uuid)
        s.customers[c.cm_uuid] = {
            # Lead with the Salesforce account (the company); ChartMogul's name is usually
            # the Stripe billing contact (a person). Only ~12% of matched names agree.
            "uuid": c.cm_uuid, "name": (acct["name"] if acct and acct["name"] else billing),
            "billing_name": billing, "partner": c.cm_uuid in s.partners, "email": c.email,
            "status": c.status, "mrr": c.mrr_cents or 0, "currency": c.currency or "USD",
            "account_id": c.sfdc_account_id, "match": c.match_method,
            "since": c.customer_since.date().isoformat() if c.customer_since else None,
            "cm_url": c.cm_url,
        }
        if c.sfdc_account_id:
            s.customers_by_account[c.sfdc_account_id].append(c.cm_uuid)

    for row in session.query(Subscription.customer_uuid, Subscription.product,
                             Subscription.plan_name, Subscription.is_current,
                             Subscription.mrr_cents, Subscription.quantity,
                             Subscription.start_date, Subscription.end_date):
        sub = Sub(row[0], row[1], row[2], bool(row[3]), row[4] or 0, row[5] or 0,
                  _aware(row[6]), _aware(row[7]))
        s.subs_by_customer[sub.customer_uuid].append(sub)
        if sub.product:
            s.subs_by_product[sub.product].append(sub)

    for product, subs in s.subs_by_product.items():
        holders: dict[str, bool] = {}
        mrr = seats = 0
        for sub in subs:
            if sub.customer_uuid in s.partners:
                continue
            holders[sub.customer_uuid] = holders.get(sub.customer_uuid, False) or sub.current
            if sub.current:
                mrr += sub.mrr
                seats += sub.qty
        if not holders:
            continue  # only ever held by partner-revenue buckets
        current = sum(1 for v in holders.values() if v)
        s.products.append({"product": product, "current": current,
                           "former": len(holders) - current, "mrr_cents": mrr, "seats": seats})
    s.products.sort(key=lambda p: (-p["mrr_cents"], -p["current"], p["product"]))
    return s


# ------------------------------------------------------------------ explorer rows

def customer_rows(snap: Snapshot, products: list[str], view: str = "current",
                  match: str = "all", q: str = "", include_partner: bool = False) -> list[dict]:
    """Customers matching the filters, sorted by product MRR then total MRR.

    With products selected, a customer is in scope if they ever held one of them;
    'current' = holds at least one selected product today. With none selected, every
    customer is in scope and 'current' = any live subscription."""
    view = view if view in VIEWS else "current"
    match = match if match in MATCHES else "all"
    wanted = set(products)
    q = (q or "").strip().lower()

    if wanted:
        uuids = {sub.customer_uuid for p in wanted for sub in snap.subs_by_product.get(p, [])}
    else:
        uuids = set(snap.customers)

    rows = []
    for uuid in uuids:
        c = snap.customers.get(uuid)
        if not c or (c["partner"] and not include_partner):
            continue
        subs = [x for x in snap.subs_by_customer.get(uuid, [])
                if not wanted or x.product in wanted]
        current = any(x.current for x in subs)
        if (view == "current" and not current) or (view == "former" and current):
            continue
        acct = snap.accounts.get(c["account_id"]) if c["account_id"] else None
        if match == "matched" and not acct:
            continue
        if match == "unmatched" and acct:
            continue
        if q and q not in (c["name"] or "").lower() and q not in (c["email"] or "").lower() \
                and q not in (c["billing_name"] or "").lower():
            continue
        held: dict[str, bool] = {}
        for x in subs:
            if x.product:
                held[x.product] = held.get(x.product, False) or x.current
        starts = [x.start for x in subs if x.start]
        ends = [x.end for x in subs if x.end and not x.current]
        rows.append({
            "uuid": uuid, "name": c["name"], "billing_name": c["billing_name"],
            "partner": c["partner"], "email": c["email"], "status": c["status"],
            "current": current,
            "products": [{"product": p, "current": cur} for p, cur in
                         sorted(held.items(), key=lambda kv: (not kv[1], kv[0]))],
            "product_mrr": sum(x.mrr for x in subs if x.current),
            "seats": sum(x.qty for x in subs if x.current),
            "mrr": c["mrr"],
            "since": min(starts).date().isoformat() if starts else c["since"],
            "ended": None if current else (max(ends).date().isoformat() if ends else None),
            "account": ({k: acct[k] for k in ("id", "name", "owner", "health",
                                               "active_users", "contacts", "contract_value",
                                               "contract_end", "active_clients")}
                        if acct else None),
        })
    rows.sort(key=lambda r: (-r["product_mrr"], -r["mrr"], r["name"].lower()))
    return rows


def totals(snap: Snapshot, rows: list[dict], products: list[str],
           include_partner: bool = False) -> dict:
    accounts = {r["account"]["id"]: r["account"] for r in rows if r["account"]}
    wanted = set(products)
    skip = set() if include_partner else snap.partners
    if wanted:
        holders: dict[str, bool] = {}
        for p in wanted:
            for sub in snap.subs_by_product.get(p, []):
                if sub.customer_uuid in skip:
                    continue
                holders[sub.customer_uuid] = holders.get(sub.customer_uuid, False) or sub.current
        current = sum(1 for v in holders.values() if v)
        former = len(holders) - current
    else:
        pool = [u for u in snap.customers if u not in skip]
        current = sum(1 for u in pool if any(x.current for x in snap.subs_by_customer.get(u, [])))
        former = len(pool) - current
    partner_in_scope = sum(
        1 for u in snap.partners
        if not wanted or any(x.product in wanted for x in snap.subs_by_customer.get(u, [])))
    return {
        "partners": partner_in_scope, "partners_included": include_partner,
        "shown": len(rows), "current": current, "former": former,
        "product_mrr": sum(r["product_mrr"] for r in rows),
        "contract_value": sum(a["contract_value"] or 0 for a in accounts.values()),
        "active_users": sum(a["active_users"] for a in accounts.values()),
        "unmatched": sum(1 for r in rows if not r["account"]),
    }


# ------------------------------------------------------------------ trends

def _month_ends(start: date, end: date) -> list[date]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        nm_y, nm_m = (y + 1, 1) if m == 12 else (y, m + 1)
        last = date(nm_y, nm_m, 1).toordinal() - 1
        out.append(min(date.fromordinal(last), end))
        y, m = nm_y, nm_m
    return out


def _active_counts(months: list[date], intervals: dict[str, list[tuple]]) -> list[int]:
    """How many customers have at least one interval covering each month (sweep + diff
    array over merged per-customer intervals; end=None means still active)."""
    from bisect import bisect_left, bisect_right
    n = len(months)
    diff = [0] * (n + 1)
    far = date.max
    for ivs in intervals.values():
        merged: list[list] = []
        for st, en in sorted(ivs, key=lambda iv: iv[0]):
            en = en or far
            if merged and st <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], en)
            else:
                merged.append([st, en])
        for st, en in merged:
            i0 = bisect_left(months, st)            # first month-end on/after start
            i1 = bisect_right(months, en) - 1       # last month-end on/before end
            if i0 <= i1:
                diff[i0] += 1
                diff[i1 + 1] -= 1
    out, run = [], 0
    for i in range(n):
        run += diff[i]
        out.append(run)
    return out


def product_trend(session, snap: Snapshot, products: list[str],
                  include_partner: bool = False) -> dict:
    """Monthly MRR (ChartMogul metrics, per product) and customers holding the selected
    products at each month end (from subscription start/end dates)."""
    q = session.query(ProductMrr.product, ProductMrr.month, ProductMrr.mrr_cents,
                      ProductMrr.new_biz_cents, ProductMrr.expansion_cents,
                      ProductMrr.contraction_cents, ProductMrr.churn_cents,
                      ProductMrr.reactivation_cents)
    if products:
        q = q.filter(ProductMrr.product.in_(products))
    per_product: dict[str, dict[date, int]] = defaultdict(dict)
    movements: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for prod, month, mrr, nb, ex, co, ch, re in q:
        per_product[prod][month] = mrr or 0
        mv = movements[month]
        mv[0] += nb or 0; mv[1] += ex or 0; mv[2] += co or 0; mv[3] += ch or 0; mv[4] += re or 0

    months = sorted({m for series in per_product.values() for m in series})
    total = [sum(per_product[p].get(m, 0) for p in per_product) for m in months]
    if months:
        lead = next((i for i, t in enumerate(total) if t), len(months))
        months, total = months[lead:], total[lead:]

    # customers holding any selected product at each month end
    skip = set() if include_partner else snap.partners
    subs = ([s for p in products for s in snap.subs_by_product.get(p, [])] if products
            else [s for lst in snap.subs_by_customer.values() for s in lst])
    subs = [s for s in subs if s.customer_uuid not in skip]
    intervals: dict[str, list[tuple]] = defaultdict(list)
    for s in subs:
        if s.start:
            intervals[s.customer_uuid].append((s.start.date(), None if s.current else
                                               (s.end.date() if s.end else s.start.date())))
    # customers-over-time comes from subscription dates, so it works even before any
    # revenue history has been synced
    starts = [iv[0] for ivs in intervals.values() for iv in ivs]
    cust_months = _month_ends(max(min(starts), date(2016, 1, 1)), date.today()) if starts else []
    customers = _active_counts(cust_months, intervals)

    series = []
    if 1 < len(products) <= 4:
        for p in products:
            series.append({"product": p, "mrr": [per_product.get(p, {}).get(m, 0) for m in months]})
    return {
        "months": [m.isoformat() for m in months],
        "mrr": total,
        "customer_months": [m.isoformat() for m in cust_months],
        "customers": customers,
        "series": series,
        "movements": [{"month": m.isoformat(), "new_biz": movements[m][0],
                       "expansion": movements[m][1], "contraction": movements[m][2],
                       "churn": movements[m][3], "reactivation": movements[m][4]}
                      for m in months],
    }


# ------------------------------------------------------------------ contacts

def contacts_for(session, snap: Snapshot, customer_uuids: list[str], active_only: bool = True,
                 role: str = "all", q: str = "", contact_ids: list[str] | None = None) -> list[dict]:
    """Salesforce contacts on the accounts of the given customers."""
    acct_to_customer: dict[str, str] = {}
    for u in customer_uuids:
        c = snap.customers.get(u)
        if c and c["account_id"] and c["account_id"] not in acct_to_customer:
            acct_to_customer[c["account_id"]] = u
    if not acct_to_customer:
        return []
    ids = list(acct_to_customer)
    rows = []
    for i in range(0, len(ids), 1000):
        query = session.query(SfdcContact).filter(SfdcContact.account_id.in_(ids[i:i + 1000]))
        if active_only:
            query = query.filter(SfdcContact.user_active.is_(True))
        if role == "admins":
            query = query.filter((SfdcContact.is_org_owner.is_(True)) |
                                 (SfdcContact.is_org_admin.is_(True)))
        if contact_ids is not None:
            query = query.filter(SfdcContact.id.in_(contact_ids))
        rows.extend(query.all())
    q = (q or "").strip().lower()
    out = []
    for ct in rows:
        if q and q not in (ct.name or "").lower() and q not in (ct.email or "").lower() \
                and q not in (ct.title or "").lower():
            continue
        cu = snap.customers[acct_to_customer[ct.account_id]]
        acct = snap.accounts.get(ct.account_id) or {}
        out.append({
            "id": ct.id, "name": ct.name, "email": ct.email, "phone": ct.phone,
            "mobile": ct.mobile_phone, "title": ct.title, "user_type": ct.user_type,
            "active": bool(ct.user_active), "owner": bool(ct.is_org_owner),
            "admin": bool(ct.is_org_admin), "account_id": ct.account_id,
            "account": acct.get("name"), "customer_uuid": cu["uuid"], "customer": cu["name"],
            "customer_status": cu["status"],
        })
    out.sort(key=lambda r: (not r["owner"], not r["admin"], not r["active"],
                            (r["customer"] or "").lower(), (r["name"] or "").lower()))
    return out


# ------------------------------------------------------------------ customer history

def customer_history(session, snap: Snapshot, uuid: str) -> dict:
    acts = (session.query(Activity).filter(Activity.customer_uuid == uuid)
            .order_by(Activity.date).all())
    events = [{
        "date": a.date.date().isoformat() if a.date else None, "type": a.type,
        "mrr": a.mrr_cents or 0, "movement": a.movement_cents or 0,
        "product": a.product, "plan": a.plan_name, "description": a.description,
    } for a in acts if a.date]
    segments = []
    for s in sorted(snap.subs_by_customer.get(uuid, []),
                    key=lambda x: (x.product or "", x.start or datetime.min.replace(tzinfo=timezone.utc))):
        segments.append({
            "product": s.product or "(unknown)", "plan": s.plan, "current": s.current,
            "mrr": s.mrr, "qty": s.qty,
            "start": s.start.date().isoformat() if s.start else None,
            "end": None if s.current else (s.end.date().isoformat() if s.end else None),
        })
    return {"events": events, "segments": segments, "today": date.today().isoformat()}
