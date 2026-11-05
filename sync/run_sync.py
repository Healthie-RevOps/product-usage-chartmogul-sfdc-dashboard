"""Daily sync, product-first. Run as a Railway cron service:  python -m sync.run_sync

  1. ChartMogul: customers in CM_CUSTOMER_STATUSES (default Active,Cancelled — cancelled
     customers keep product history; filtered server-side),
     then each one's subscriptions fetched in parallel. Optional CM_PRODUCT_FILTER keeps
     only the listed product families.
  2. SFDC: only the Accounts whose Stripe Customer ID / Healthie Org ID came back in step 1
     (not all ~212k accounts with a Stripe ID).
  3. Match, then contacts + closed-won contracts for matched accounts.
  4. Healthie org usage from Metabase (sync/metabase.py), if configured. Separate
     transaction, non-fatal.

Match waterfall (recorded per customer as match_method):
  1. stripe_id — ChartMogul external_id (cus_...) == Account.Stripe_Customer_ID__c
  2. org_id   — ChartMogul custom attribute healthie_org_id == Account.Healthie_Org_ID__c
  3. unmatched — surfaced in the dashboard, never silently dropped

Everything runs in one transaction, so the dashboard keeps serving the previous
snapshot until the new one commits.
"""
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

from dateutil import parser as dtparse

from app.db import (
    Activity, Customer, ProductMrr, SfdcAccount, SfdcContact, SfdcContract, Subscription,
    SyncRun, init_db, session_scope,
)
from sync import chartmogul as cm
from sync import metabase
from sync import sfdc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sync")

CUSTOMER_STATUSES = [s.strip() for s in
                     os.environ.get("CM_CUSTOMER_STATUSES", "Active,Cancelled").split(",") if s.strip()]
PRODUCT_FILTER = {p.strip().lower() for p in
                  os.environ.get("CM_PRODUCT_FILTER", "").split(",") if p.strip()}


def _dt(value):
    if not value:
        return None
    try:
        return dtparse.parse(value)
    except (ValueError, TypeError):
        return None


def _d(value):
    dt = _dt(value)
    return dt.date() if dt else None


def _clear(session):
    for model in (SfdcContract, SfdcContact, Activity, Subscription, Customer, SfdcAccount,
                  ProductMrr):
        session.query(model).delete()
    session.flush()


def _is_current(status: str | None, end_date) -> bool:
    """Live today. Trust ChartMogul's status when present; otherwise an end date in the
    future (active subs carry their next renewal date) or no end date at all."""
    if status:
        return status.lower() == "active"
    return end_date is None or end_date > datetime.now(timezone.utc)


def _wanted(product: str | None) -> bool:
    return not PRODUCT_FILTER or (product or "").lower() in PRODUCT_FILTER


def sync_chartmogul(session, plans: list[dict]) -> tuple[int, int, int]:
    plan_names = {p["external_id"]: p["name"] for p in plans if p.get("external_id")}
    if PRODUCT_FILTER:
        families = {cm.product_family(p.get("name")) for p in plans}
        unknown = sorted(p for p in PRODUCT_FILTER
                         if p not in {(f or "").lower() for f in families})
        if unknown:
            log.warning("CM_PRODUCT_FILTER entries matching no ChartMogul plan: %s", unknown)
        log.info("Product filter: %s", sorted(PRODUCT_FILTER))

    raws = list(cm.iter_customers(CUSTOMER_STATUSES))
    log.info("Listed %d ChartMogul customers with status %s", len(raws), CUSTOMER_STATUSES)

    by_uuid = {r["uuid"]: r for r in raws}
    n_cust = n_sub = n_act = 0
    t0 = time.monotonic()
    for i, (uuid, subs, acts) in enumerate(cm.map_customer_details(list(by_uuid)), 1):
        rows = []
        for sub in subs:
            plan = sub.get("plan") or sub.get("plan_name")
            product = cm.product_family(plan)
            if not _wanted(product):
                continue
            end = _dt(sub.get("end-date") or sub.get("end_date"))
            if end and end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            rows.append(Subscription(
                cm_uuid=sub["uuid"],
                customer_uuid=uuid,
                plan_name=plan,
                product=product,
                external_id=sub.get("external_id"),
                status=sub.get("status"),
                is_current=_is_current(sub.get("status"), end),
                quantity=sub.get("quantity"),
                mrr_cents=sub.get("mrr") or 0,
                currency=sub.get("currency") or "USD",
                billing_cycle=sub.get("billing-cycle") or sub.get("billing_cycle"),
                start_date=_dt(sub.get("start-date") or sub.get("start_date")),
                end_date=end,
            ))
        if PRODUCT_FILTER and not rows:
            continue  # customer has none of the products we care about

        raw = by_uuid[uuid]
        session.add(Customer(
            cm_uuid=uuid,
            name=raw.get("name") or raw.get("company") or raw.get("email"),
            email=raw.get("email"),
            status=raw.get("status"),
            customer_since=_dt(raw.get("customer-since") or raw.get("customer_since")),
            mrr_cents=raw.get("mrr") or 0,
            currency=raw.get("currency") or "USD",
            stripe_customer_id=cm.extract_stripe_id(raw),
            healthie_org_id=cm.extract_org_id(raw),
            cm_url=raw.get("chartmogul-url") or raw.get("chartmogul_url"),
        ))
        session.add_all(rows)
        n_cust += 1
        n_sub += len(rows)

        for a in acts:
            plan = plan_names.get(a.get("plan-external-id"))
            session.add(Activity(
                customer_uuid=uuid,
                date=_dt(a.get("date")),
                type=a.get("type"),
                mrr_cents=a.get("activity-mrr"),
                movement_cents=a.get("activity-mrr-movement"),
                plan_external_id=a.get("plan-external-id"),
                plan_name=plan,
                product=cm.product_family(plan),
                subscription_external_id=a.get("subscription-external-id"),
                description=a.get("description"),
            ))
        n_act += len(acts)

        if i % 500 == 0:
            rate = i / (time.monotonic() - t0)
            log.info("ChartMogul progress: %d/%d customers (%.0f/s, ~%.0f min left), "
                     "%d subscriptions, %d activities", i, len(by_uuid), rate,
                     (len(by_uuid) - i) / rate / 60, n_sub, n_act)
            session.flush()

    log.info("Kept %d customers, %d subscriptions, %d activities", n_cust, n_sub, n_act)
    return n_cust, n_sub, n_act


PRODUCT_MRR_START = os.environ.get("CM_HISTORY_START", "2016-01-01")


def sync_product_mrr(session, plans: list[dict]) -> int:
    """Monthly MRR per product from ChartMogul's metrics API (filtered to its plans)."""
    by_product: dict[str, list[str]] = {}
    for p in plans:
        product = cm.product_family(p.get("name"))
        if product and _wanted(product):
            by_product.setdefault(product, []).append(p["uuid"])
    end = date.today().isoformat()
    n = 0
    for product, uuids in sorted(by_product.items()):
        series = cm.fetch_product_mrr(uuids, PRODUCT_MRR_START, end)
        if not any(v["mrr"] for v in series.values()):
            continue  # never had revenue
        for day, v in series.items():
            session.add(ProductMrr(
                product=product, month=_d(day), mrr_cents=v["mrr"],
                new_biz_cents=v["mrr-new-business"], expansion_cents=v["mrr-expansion"],
                contraction_cents=v["mrr-contraction"], churn_cents=v["mrr-churn"],
                reactivation_cents=v["mrr-reactivation"]))
            n += 1
    session.flush()
    log.info("Product MRR history: %d products, %d monthly rows", len(by_product), n)
    return n


def sync_sfdc_accounts(session) -> int:
    keys = session.query(Customer.stripe_customer_id, Customer.healthie_org_id).all()
    stripe_ids = {k[0] for k in keys if k[0]}
    org_ids = {k[1] for k in keys if k[1]}
    n = 0
    for a in sfdc.fetch_accounts_for(stripe_ids, org_ids):
        session.add(SfdcAccount(**a))
        n += 1
    session.flush()
    log.info("Fetched %d SFDC accounts for %d Stripe IDs / %d Org IDs",
             n, len(stripe_ids), len(org_ids))
    return n


def match(session) -> tuple[int, int]:
    accounts = session.query(SfdcAccount).all()
    by_stripe = {a.stripe_customer_id: a.id for a in accounts if a.stripe_customer_id}
    by_org = {a.healthie_org_id: a.id for a in accounts if a.healthie_org_id}

    matched = unmatched = 0
    for cust in session.query(Customer).all():
        acct_id, method = None, "unmatched"
        if cust.stripe_customer_id and cust.stripe_customer_id in by_stripe:
            acct_id, method = by_stripe[cust.stripe_customer_id], "stripe_id"
        elif cust.healthie_org_id and cust.healthie_org_id in by_org:
            acct_id, method = by_org[cust.healthie_org_id], "org_id"
        cust.sfdc_account_id = acct_id
        cust.match_method = method
        matched += acct_id is not None
        unmatched += acct_id is None

    log.info("Match results: %d matched, %d unmatched", matched, unmatched)
    return matched, unmatched


def sync_sfdc_details(session) -> tuple[int, int]:
    """Contacts + contracts, only for accounts joined to a ChartMogul customer."""
    account_ids = [r[0] for r in session.query(Customer.sfdc_account_id)
                   .filter(Customer.sfdc_account_id.isnot(None)).distinct()]
    log.info("Fetching contacts + contracts for %d matched accounts", len(account_ids))

    n_contacts = 0
    for c in sfdc.fetch_contacts(account_ids):
        c["last_active"] = _dt(c["last_active"])
        session.add(SfdcContact(**c))
        n_contacts += 1
        if n_contacts % 5000 == 0:
            session.flush()

    n_contracts = 0
    for o in sfdc.fetch_contracts(account_ids):
        for k in ("close_date", "contract_start", "contract_end"):
            o[k] = _d(o[k])
        session.add(SfdcContract(**o))
        n_contracts += 1

    log.info("Fetched %d contacts, %d won contracts", n_contacts, n_contracts)
    return n_contacts, n_contracts


def main():
    init_db()
    with session_scope() as session:
        run = SyncRun()
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        with session_scope() as session:
            _clear(session)
            plans = list(cm.iter_plans())
            log.info("Listed %d ChartMogul plans", len(plans))
            n_cust, n_sub, _ = sync_chartmogul(session, plans)
            sync_product_mrr(session, plans)
            n_acct = sync_sfdc_accounts(session)
            n_match, n_unmatch = match(session)
            session.flush()
            n_contact, n_contract = sync_sfdc_details(session)
            run = session.get(SyncRun, run_id)
            run.status = "success"
            run.finished_at = datetime.now(timezone.utc)
            run.accounts_synced, run.contacts_synced = n_acct, n_contact
            run.contracts_synced = n_contract
            run.customers_synced, run.subscriptions_synced = n_cust, n_sub
            run.matched, run.unmatched = n_match, n_unmatch
        log.info("Sync complete")
    except Exception as exc:
        log.exception("Sync failed")
        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error = str(exc)[:4000]
        sys.exit(1)

    # Healthie product usage (Metabase). Own transaction; a failure here is logged but
    # never fails the ChartMogul/SFDC snapshot, and the previous usage data stays.
    try:
        metabase.sync_org_usage()
    except Exception:
        log.exception("Metabase org usage sync failed; keeping previous data")


if __name__ == "__main__":
    main()
