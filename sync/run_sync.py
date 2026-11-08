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

Stages commit separately, so a slow or failing ChartMogul never holds back or rolls back
the Salesforce data (and vice versa):
  A. ChartMogul. Incremental by default: the account-wide activity feed since the last
     refresh (minus SYNC_OVERLAP_DAYS) says which customers changed (~30 a day of ~15k);
     only those are refetched. Full refresh on SYNC_FULL_WEEKDAY (default Sunday), when
     SYNC_FULL=1, or with no previous refresh to build on. ~1-2 min vs ~40 min full.
  B. Salesforce: accounts, match, contacts + contracts, opportunities.
  C. Metabase.
If ChartMogul fails transiently, B and C still run, then A is retried once after
SYNC_SNAPSHOT_RETRY_WAIT_S and customers are re-matched to accounts. A run with one failed
stage is "partial": the dashboard shows what did refresh.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from dateutil import parser as dtparse
from sqlalchemy import desc

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
# ChartMogul stage attempts when ChartMogul is down (see main)
SNAPSHOT_ATTEMPTS = int(os.environ.get("SYNC_SNAPSHOT_ATTEMPTS", "2"))
SNAPSHOT_RETRY_WAIT_S = int(os.environ.get("SYNC_SNAPSHOT_RETRY_WAIT_S", "1800"))
# Incremental ChartMogul refresh (see _cm_mode)
FULL_WEEKDAY = int(os.environ.get("SYNC_FULL_WEEKDAY", "6"))     # Mon=0 .. Sun=6; -1 = never
OVERLAP_DAYS = int(os.environ.get("SYNC_OVERLAP_DAYS", "2"))
PRODUCT_MRR_RECENT_MONTHS = 3     # incremental runs refetch this many months of product MRR


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


def _clear_chartmogul(session):
    for model in (Activity, Subscription, Customer, ProductMrr):
        session.query(model).delete()
    session.flush()


def _clear_sfdc(session):
    # Customers persist across runs now; unlink them before their accounts go away
    session.query(Customer).update({Customer.sfdc_account_id: None,
                                    Customer.match_method: "unmatched"},
                                   synchronize_session=False)
    for model in (SfdcOpportunity, SfdcContract, SfdcContact, SfdcAccount):
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


def _store_customer(session, raw: dict, subs: list, acts: list,
                    plan_names: dict) -> tuple[int, int] | None:
    """Add one ChartMogul customer with its subscriptions and activities.
    Returns (subscriptions, activities) stored, or None if filtered out."""
    uuid = raw["uuid"]
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
        return None  # customer has none of the products we care about

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
    return len(rows), len(acts)


def _plan_names(plans: list[dict]) -> dict:
    return {p["external_id"]: p["name"] for p in plans if p.get("external_id")}


def sync_chartmogul(session, plans: list[dict]) -> tuple[int, int, int]:
    """Full refresh: every customer in CUSTOMER_STATUSES (caller clears the tables)."""
    plan_names = _plan_names(plans)
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
        stored = _store_customer(session, by_uuid[uuid], subs, acts, plan_names)
        if stored:
            n_cust += 1
            n_sub += stored[0]
            n_act += stored[1]
        if i % 500 == 0:
            rate = i / (time.monotonic() - t0)
            log.info("ChartMogul progress: %d/%d customers (%.0f/s, ~%.0f min left), "
                     "%d subscriptions, %d activities", i, len(by_uuid), rate,
                     (len(by_uuid) - i) / rate / 60, n_sub, n_act)
            session.flush()

    log.info("Kept %d customers, %d subscriptions, %d activities", n_cust, n_sub, n_act)
    return n_cust, n_sub, n_act


def _status_wanted(status: str | None) -> bool:
    return (status or "").lower() in {s.lower() for s in CUSTOMER_STATUSES}


def sync_chartmogul_incremental(session, plans: list[dict], since: datetime) -> tuple[int, int, int]:
    """Refetch only customers with ChartMogul activity since `since` (minus the overlap);
    everyone else keeps last night's rows. A customer that left CUSTOMER_STATUSES (or was
    deleted) is removed."""
    start = (since - timedelta(days=OVERLAP_DAYS)).astimezone(timezone.utc)
    changed = sorted({a.get("customer-uuid") for a in
                      cm.iter_activities_since(start.strftime("%Y-%m-%dT%H:%M:%SZ"))
                      if a.get("customer-uuid")})
    log.info("ChartMogul incremental: %d customers with activity since %s",
             len(changed), start.date().isoformat())
    if not changed:
        return 0, 0, 0
    with ThreadPoolExecutor(max_workers=cm.WORKERS) as pool:
        raws = dict(zip(changed, pool.map(cm.fetch_customer, changed)))
    for i in range(0, len(changed), 500):
        chunk = changed[i:i + 500]
        session.query(Activity).filter(Activity.customer_uuid.in_(chunk)).delete(synchronize_session=False)
        session.query(Subscription).filter(Subscription.customer_uuid.in_(chunk)).delete(synchronize_session=False)
        session.query(Customer).filter(Customer.cm_uuid.in_(chunk)).delete(synchronize_session=False)
    session.flush()
    keep = [u for u in changed if raws.get(u) and _status_wanted(raws[u].get("status"))]
    plan_names = _plan_names(plans)
    n_cust = n_sub = n_act = 0
    for uuid, subs, acts in cm.map_customer_details(keep):
        stored = _store_customer(session, raws[uuid], subs, acts, plan_names)
        if stored:
            n_cust += 1
            n_sub += stored[0]
            n_act += stored[1]
    log.info("ChartMogul incremental: refreshed %d customers (%d subscriptions, %d activities), "
             "removed %d no longer Active/Cancelled", n_cust, n_sub, n_act, len(changed) - len(keep))
    return n_cust, n_sub, n_act


PRODUCT_MRR_START = os.environ.get("CM_HISTORY_START", "2016-01-01")


def _months_ago(n: int) -> date:
    d = date.today().replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def sync_product_mrr(session, plans: list[dict], since_month: date | None = None) -> int:
    """Monthly MRR per product from ChartMogul's metrics API (filtered to its plans).
    since_month: refresh only months from there on (incremental); None = full history
    (caller cleared the table)."""
    if since_month:
        session.query(ProductMrr).filter(ProductMrr.month >= since_month).delete(synchronize_session=False)
    by_product: dict[str, list[str]] = {}
    for p in plans:
        product = cm.product_family(p.get("name"))
        if product and _wanted(product):
            by_product.setdefault(product, []).append(p["uuid"])
    end = date.today().isoformat()
    start = since_month.isoformat() if since_month else PRODUCT_MRR_START
    n = 0
    products = sorted(by_product)
    # One metrics call per product; independent, so fetch concurrently (~2.5 min -> ~20s)
    with ThreadPoolExecutor(max_workers=cm.WORKERS) as pool:
        all_series = pool.map(lambda prod: cm.fetch_product_mrr(by_product[prod], start, end), products)
        fetched = list(zip(products, all_series))
    for product, series in fetched:
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


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _cm_mode(session) -> tuple[str, datetime | None, str]:
    """("full" | "incremental", since, why)."""
    if os.environ.get("SYNC_FULL", "").strip().lower() in ("1", "true", "yes"):
        return "full", None, "SYNC_FULL is set"
    if datetime.now(timezone.utc).weekday() == FULL_WEEKDAY:
        return "full", None, "weekly full refresh"
    if session.query(Customer.cm_uuid).first() is None:
        return "full", None, "no ChartMogul data yet"
    last = (session.query(SyncRun).filter(SyncRun.cm_completed_at.isnot(None))
            .order_by(desc(SyncRun.id)).first()
            # runs from before stages existed: a successful run refreshed ChartMogul fully
            or session.query(SyncRun).filter(SyncRun.status == "success")
            .order_by(desc(SyncRun.id)).first())
    if not last or not last.started_at:
        return "full", None, "no previous ChartMogul refresh"
    return "incremental", _aware(last.started_at), ""


def _cm_stage(run_id: int) -> None:
    """Stage A: ChartMogul customers, subscriptions, activities, product MRR."""
    with session_scope() as session:
        mode, since, why = _cm_mode(session)
        plans = list(cm.iter_plans())
        log.info("Listed %d ChartMogul plans", len(plans))
        if mode == "full":
            log.info("ChartMogul: full refresh (%s)", why)
            _clear_chartmogul(session)
            n_cust, n_sub, _ = sync_chartmogul(session, plans)
            sync_product_mrr(session, plans)
        else:
            n_cust, n_sub, _ = sync_chartmogul_incremental(session, plans, since)
            sync_product_mrr(session, plans, since_month=_months_ago(PRODUCT_MRR_RECENT_MONTHS))
        run = session.get(SyncRun, run_id)
        run.cm_mode = mode
        run.cm_completed_at = datetime.now(timezone.utc)
        run.customers_synced, run.subscriptions_synced = n_cust, n_sub


def _sfdc_stage(run_id: int) -> None:
    """Stage B: Salesforce accounts, matching, contacts + contracts, opportunities. All or
    nothing, so the dashboard keeps the previous Salesforce data if anything fails."""
    with session_scope() as session:
        _clear_sfdc(session)
        n_acct = sync_sfdc_accounts(session)
        n_match, n_unmatch = match(session)
        session.flush()
        n_contact, n_contract = sync_sfdc_details(session)
        n_opp = sync_sfdc_opportunities(session)
        run = session.get(SyncRun, run_id)
        run.accounts_synced, run.contacts_synced = n_acct, n_contact
        run.contracts_synced, run.opportunities_synced = n_contract, n_opp
        run.matched, run.unmatched = n_match, n_unmatch


def _rematch(run_id: int) -> None:
    """After a late ChartMogul refresh: link (new) customers to the accounts we have."""
    with session_scope() as session:
        n_match, n_unmatch = match(session)
        run = session.get(SyncRun, run_id)
        run.matched, run.unmatched = n_match, n_unmatch


def _run_stage(name: str, fn, run_id: int, errors: dict) -> bool:
    t0 = time.monotonic()
    try:
        fn(run_id)
    except Exception as exc:
        log.exception("%s stage failed", name)
        errors[name] = exc
        return False
    errors.pop(name, None)
    log.info("%s stage done in %.0fs", name, time.monotonic() - t0)
    return True


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

    errors: dict[str, Exception] = {}
    cm_ok = _run_stage("ChartMogul", _cm_stage, run_id, errors)
    sfdc_ok = _run_stage("Salesforce", _sfdc_stage, run_id, errors)

    # Healthie product usage (Metabase). Independent of the other stages; its own
    # transaction; on failure the previous data stays.
    try:
        metabase.sync_org_usage()
    except Exception:
        log.exception("Metabase org usage sync failed; keeping previous data")

    # ChartMogul has gone down for 20+ minutes at a stretch: wait and retry it, only for
    # transient errors (a bug won't fix itself), after the other stages are already done.
    attempt = 1
    while not cm_ok and isinstance(errors.get("ChartMogul"), cm.ChartMogulTransientError) \
            and attempt < SNAPSHOT_ATTEMPTS:
        attempt += 1
        log.warning("ChartMogul unavailable; retrying its stage in %d min (attempt %d of %d)",
                    SNAPSHOT_RETRY_WAIT_S // 60, attempt, SNAPSHOT_ATTEMPTS)
        time.sleep(SNAPSHOT_RETRY_WAIT_S)
        cm_ok = _run_stage("ChartMogul", _cm_stage, run_id, errors)
        if cm_ok and sfdc_ok:
            _run_stage("Re-match", _rematch, run_id, errors)

    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        run.finished_at = datetime.now(timezone.utc)
        if not errors:
            run.status = "success"
        else:
            run.status = "failed" if not (cm_ok or sfdc_ok) else "partial"
            run.error = "; ".join(f"{k}: {v}" for k, v in errors.items())[:4000]
    if errors:
        log.error("Sync %s: %s", "failed" if not (cm_ok or sfdc_ok) else "partly failed",
                  ", ".join(errors))
        sys.exit(1)  # the cron run still shows as failed
    log.info("Sync complete")


if __name__ == "__main__":
    main()
