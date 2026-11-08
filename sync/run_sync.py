"""Daily sync, product-first. Run as a Railway cron service:  python -m sync.run_sync

  1. ChartMogul: customers in CM_CUSTOMER_STATUSES (default Active,Cancelled — cancelled
     customers keep product history; filtered server-side),
     then each one's subscriptions fetched in parallel. Optional CM_PRODUCT_FILTER keeps
     only the listed product families.
  2. SFDC: only the Accounts whose Stripe Customer ID / Healthie Org ID came back in step 1
     (not all ~212k accounts with a Stripe ID).
  3. Match, then contacts + closed-won contracts for synced accounts. Enterprise-plan
     accounts are synced too, even with no ChartMogul match.
  3b. SFDC opportunities: all open + closed since SFDC_OPPS_CLOSED_SINCE, any account.
  4. Healthie org usage from Metabase (sync/metabase.py), if configured. Separate
     transaction, non-fatal, and runs even if steps 1-3 failed.

If ChartMogul is unavailable, steps 1-3 are retried once after 30 min
(SYNC_SNAPSHOT_ATTEMPTS / SYNC_SNAPSHOT_RETRY_WAIT_S).

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
    Activity, Customer, ProductMrr, SfdcAccount, SfdcContact, SfdcContract, SfdcOpportunity,
    Subscription, SyncRun, init_db, session_scope,
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
# Closed opportunities older than this are skipped (open ones are always synced)
OPPS_CLOSED_SINCE = os.environ.get("SFDC_OPPS_CLOSED_SINCE", "2024-01-01")
# Whole-snapshot retries when ChartMogul is down (see _snapshot_with_retry)
SNAPSHOT_ATTEMPTS = int(os.environ.get("SYNC_SNAPSHOT_ATTEMPTS", "2"))
SNAPSHOT_RETRY_WAIT_S = int(os.environ.get("SYNC_SNAPSHOT_RETRY_WAIT_S", "1800"))


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
    for model in (SfdcOpportunity, SfdcContract, SfdcContact, Activity, Subscription, Customer,
                  SfdcAccount, ProductMrr):
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
    seen = set()
    for a in sfdc.fetch_accounts_for(stripe_ids, org_ids):
        _add_account(session, a)
        seen.add(a["id"])
    log.info("Fetched %d SFDC accounts for %d Stripe IDs / %d Org IDs",
             len(seen), len(stripe_ids), len(org_ids))

    # Enterprise accounts are the focus of the enterprise views; include the ones whose
    # Stripe/Org ID doesn't match a ChartMogul customer too.
    extra = 0
    if sfdc.account_field_visible("current_plan"):
        for a in sfdc.fetch_accounts("Current_Plan__c = 'Enterprise'"):
            if a["id"] not in seen:
                _add_account(session, a)
                seen.add(a["id"])
                extra += 1
        log.info("Added %d Enterprise-plan SFDC accounts with no ChartMogul match", extra)
    session.flush()
    return len(seen)


def _add_account(session, a: dict) -> None:
    for k in ("renewal_date", "kickoff_date"):
        a[k] = _d(a.get(k))
    session.add(SfdcAccount(**a))


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
    """Contacts + contracts for synced accounts (matched to ChartMogul, or Enterprise)."""
    account_ids = [r[0] for r in session.query(SfdcAccount.id)]
    log.info("Fetching contacts + contracts for %d accounts", len(account_ids))

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


def sync_sfdc_opportunities(session) -> int:
    n = 0
    for o in sfdc.fetch_opportunities(OPPS_CLOSED_SINCE):
        for k in ("close_date", "revisit_date"):
            o[k] = _d(o[k])
        o["created_date"] = _dt(o["created_date"])
        session.add(SfdcOpportunity(**o))
        n += 1
        if n % 5000 == 0:
            session.flush()
    log.info("Fetched %d SFDC opportunities (open, or closed since %s)", n, OPPS_CLOSED_SINCE)
    return n


def _snapshot(run_id: int) -> None:
    """ChartMogul + SFDC in one transaction: all or nothing, so the dashboard keeps
    serving the previous snapshot if any step fails."""
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
        n_opp = sync_sfdc_opportunities(session)
        run = session.get(SyncRun, run_id)
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        run.accounts_synced, run.contacts_synced = n_acct, n_contact
        run.contracts_synced, run.opportunities_synced = n_contract, n_opp
        run.customers_synced, run.subscriptions_synced = n_cust, n_sub
        run.matched, run.unmatched = n_match, n_unmatch


def _snapshot_with_retry(run_id: int) -> None:
    """ChartMogul's API has timed out for 20+ minutes at a stretch (every night at
    09:00 UTC in early Nov 2026, fine an hour later). Retry the whole snapshot after a
    wait, only for those transient ChartMogul errors; anything else fails right away."""
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        try:
            _snapshot(run_id)
            return
        except cm.ChartMogulTransientError as exc:
            if attempt == SNAPSHOT_ATTEMPTS:
                raise
            log.warning("ChartMogul unavailable (%s); retrying the snapshot in %d min "
                        "(attempt %d of %d)", exc, SNAPSHOT_RETRY_WAIT_S // 60,
                        attempt + 1, SNAPSHOT_ATTEMPTS)
            time.sleep(SNAPSHOT_RETRY_WAIT_S)


def main():
    init_db()
    with session_scope() as session:
        # Only one sync runs at a time, so any run still marked "running" was killed
        # (deployment replaced, out of memory, ...). Without this it shows "running" forever.
        for stale in session.query(SyncRun).filter(SyncRun.status == "running"):
            stale.status = "failed"
            stale.finished_at = datetime.now(timezone.utc)
            stale.error = "Interrupted: the sync process stopped before finishing"
        run = SyncRun()
        session.add(run)
        session.flush()
        run_id = run.id

    failed = False
    try:
        _snapshot_with_retry(run_id)
        log.info("Sync complete")
    except Exception as exc:
        failed = True
        log.exception("Sync failed")
        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error = str(exc)[:4000]

    # Healthie product usage (Metabase). Independent of ChartMogul/SFDC, so it runs even
    # when the snapshot failed. Own transaction; on failure the previous data stays.
    try:
        metabase.sync_org_usage()
    except Exception:
        log.exception("Metabase org usage sync failed; keeping previous data")

    if failed:
        sys.exit(1)  # after Metabase, so the cron run still shows as failed


if __name__ == "__main__":
    main()
