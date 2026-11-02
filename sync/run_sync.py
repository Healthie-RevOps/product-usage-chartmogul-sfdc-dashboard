"""Daily sync: ChartMogul customers/subscriptions + SFDC accounts/contacts,
then the join. Run as a Railway cron service:  python -m sync.run_sync

Match waterfall (recorded per customer as match_method):
  1. stripe_id — ChartMogul external_id (cus_...) == Account.Stripe_Customer_ID__c
  2. org_id   — ChartMogul custom attribute healthie_org_id == Account.Healthie_Org_ID__c
  3. unmatched — surfaced in the dashboard, never silently dropped
"""
import logging
import sys
from datetime import datetime, timezone

from dateutil import parser as dtparse

from app.db import (
    Customer, SfdcAccount, SfdcContact, SfdcContract, Subscription, SyncRun, init_db,
    session_scope,
)
from sync import chartmogul as cm
from sync import sfdc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sync")


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


def sync_sfdc_accounts(session) -> int:
    accounts = list(sfdc.fetch_accounts())
    log.info("Fetched %d SFDC accounts", len(accounts))

    session.query(SfdcContract).delete()
    session.query(SfdcContact).delete()
    session.query(SfdcAccount).delete()
    for a in accounts:
        session.add(SfdcAccount(**a))
    session.flush()
    return len(accounts)


def sync_sfdc_details(session) -> tuple[int, int]:
    """Contacts + contracts, only for accounts joined to a ChartMogul customer.
    (Pulling contacts for every account with a Stripe ID would be 500k+ rows.)"""
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


def sync_chartmogul(session) -> tuple[int, int]:
    session.query(Subscription).delete()
    session.query(Customer).delete()

    n_cust = n_sub = 0
    for raw in cm.iter_customers():
        cust = Customer(
            cm_uuid=raw["uuid"],
            name=raw.get("name") or raw.get("company") or raw.get("email"),
            email=raw.get("email"),
            status=raw.get("status"),
            customer_since=_dt(raw.get("customer-since") or raw.get("customer_since")),
            mrr_cents=raw.get("mrr") or 0,
            currency=raw.get("currency") or "USD",
            stripe_customer_id=cm.extract_stripe_id(raw),
            healthie_org_id=cm.extract_org_id(raw),
            cm_url=raw.get("chartmogul_url") or raw.get("cm_url"),
        )
        session.add(cust)
        n_cust += 1

        # Skip subscription calls for customers with no revenue history
        if raw.get("status") in ("Lead",):
            continue
        for sub in cm.iter_subscriptions(raw["uuid"]):
            session.add(Subscription(
                cm_uuid=sub["uuid"],
                customer_uuid=raw["uuid"],
                plan_name=sub.get("plan") or sub.get("plan_name"),
                external_id=sub.get("external_id"),
                status=sub.get("status"),
                quantity=sub.get("quantity"),
                mrr_cents=sub.get("mrr") or 0,
                currency=sub.get("currency") or "USD",
                billing_cycle=sub.get("billing-cycle") or sub.get("billing_cycle"),
                start_date=_dt(sub.get("start-date") or sub.get("start_date")),
                end_date=_dt(sub.get("end-date") or sub.get("end_date")),
            ))
            n_sub += 1

        if n_cust % 250 == 0:
            log.info("ChartMogul progress: %d customers, %d subscriptions", n_cust, n_sub)
            session.flush()

    log.info("Fetched %d customers, %d subscriptions", n_cust, n_sub)
    return n_cust, n_sub


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


def main():
    init_db()
    with session_scope() as session:
        run = SyncRun()
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        with session_scope() as session:
            n_acct = sync_sfdc_accounts(session)
            n_cust, n_sub = sync_chartmogul(session)
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
    except Exception as exc:
        log.exception("Sync failed")
        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error = str(exc)[:4000]
        sys.exit(1)


if __name__ == "__main__":
    main()
