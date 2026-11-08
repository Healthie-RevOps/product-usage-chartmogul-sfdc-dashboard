"""Healthie product data from Metabase (visualize.gethealthie.com). Read-only.

    python -m sync.metabase          # refresh org usage on its own (no ChartMogul/SFDC)

Runs *saved questions* only, never ad-hoc SQL. The gateway in front of Metabase rejects
SQL sent from outside the Metabase UI. A saved question also keeps the query reviewable
in Metabase: which fields leave the warehouse is decided there, in one visible place.

DATA POLICY: Healthie data includes patient PHI. Questions used here must return
org-level aggregates only (counts and dates keyed by Healthie Org ID), never rows about
individual providers or clients. Each question has a column allowlist, and the sync
fails before writing anything if Metabase returns any other column (e.g. someone later
adds emails to the question).

Auth, first one set wins:
  METABASE_API_KEY  admin-issued read-only key, limited to the cards' collection
  METABASE_SESSION  browser session cookie `metabase.SESSION` (local testing only:
                    expires, carries the user's full access; never set it on Railway)
"""
import logging
import os
import time
from datetime import date

import requests
from sqlalchemy import insert

from app.db import HealthieOrgUsage, init_db, session_scope

log = logging.getLogger("sync.metabase")

METABASE_URL = os.environ.get("METABASE_URL", "https://visualize.gethealthie.com").rstrip("/")
API_KEY = os.environ.get("METABASE_API_KEY", "").strip()
SESSION = os.environ.get("METABASE_SESSION", "").strip()

# Saved question "Provider counts by Org" (DBT_L30_MART.DIM_PROVIDERS, test and Healthie
# accounts excluded, grouped by Provider Organization ID, not binned).
PROVIDER_COUNTS_CARD = int(os.environ.get("METABASE_PROVIDER_COUNTS_CARD", "11966"))

# Saved question "Client counts by Org" (DBT_L30_MART.DIM_CLIENTS, test users excluded,
# grouped by Client Organization ID). Patient COUNTS only.
CLIENT_COUNTS_CARD = int(os.environ.get("METABASE_CLIENT_COUNTS_CARD", "11967"))

# Metabase column display name -> our column. Anything else returned = refuse to load.
PROVIDER_COUNTS_COLUMNS = {
    "Provider Organization ID": "org_id",
    "Count": "providers",
    "Active Providers": "active_providers",
    "Admins": "admins",
    "Max of Provider Last Active Datetime: Day": "last_provider_activity",
}
CLIENT_COUNTS_COLUMNS = {
    "Client Organization ID": "org_id",
    "Count": "clients",
    "Active Clients": "active_clients",
}

MAX_ATTEMPTS = 5


class MetabaseError(RuntimeError):
    pass


def configured() -> bool:
    return bool(API_KEY or SESSION)


def _headers() -> dict:
    if API_KEY:
        return {"X-API-Key": API_KEY}
    if SESSION:
        return {"X-Metabase-Session": SESSION}
    raise MetabaseError("Set METABASE_API_KEY (or METABASE_SESSION for local testing)")


def run_card(card_id: int) -> list[dict]:
    """Run a saved question and return its rows as dicts keyed by column display name.
    Only saved questions: there is deliberately no way to send SQL from here."""
    url = f"{METABASE_URL}/api/card/{int(card_id)}/query/json"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, headers=_headers(), json={}, timeout=600)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                raise MetabaseError(f"Metabase unreachable: {exc}") from exc
            wait = 2 ** attempt
            log.warning("Metabase %s on card %s, retrying in %ss", type(exc).__name__, card_id, wait)
            time.sleep(wait)
            continue
        if resp.status_code in (401, 403):
            hint = ("session expired or logged out; copy a fresh metabase.SESSION cookie"
                    if not API_KEY else "API key lacks access to this question")
            raise MetabaseError(f"Metabase {resp.status_code} on card {card_id}: {hint}")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                raise MetabaseError(f"Metabase {resp.status_code} on card {card_id}")
            wait = 2 ** attempt
            log.warning("Metabase %s on card %s, retrying in %ss", resp.status_code, card_id, wait)
            time.sleep(wait)
            continue
        if resp.status_code not in (200, 202):
            raise MetabaseError(f"Metabase {resp.status_code} on card {card_id}: {resp.text[:300]}")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise MetabaseError(f"Card {card_id} returned non-JSON: {resp.text[:200]}") from exc
        # Query errors come back as a JSON object instead of a list of rows
        if not isinstance(rows, list):
            raise MetabaseError(f"Card {card_id} failed: {str(rows)[:300]}")
        return rows
    raise MetabaseError("unreachable")


def _check_columns(card_id: int, rows: list[dict], allowed: dict) -> None:
    if not rows:
        raise MetabaseError(f"Card {card_id} returned no rows")
    cols = set(rows[0])
    extra, missing = cols - set(allowed), set(allowed) - cols
    if extra:
        raise MetabaseError(
            f"Card {card_id} returned columns outside the allowlist {sorted(extra)}. "
            "Refusing to load: review the question in Metabase (org-level aggregates only).")
    if missing:
        raise MetabaseError(f"Card {card_id} is missing expected columns {sorted(missing)}; "
                            "was the question edited?")


def _int(v):
    return int(v) if v not in (None, "") else None


def _date(v):
    return date.fromisoformat(str(v)[:10]) if v else None


def _org(v) -> str | None:
    org = _int(v)
    return str(org) if org is not None else None  # SFDC Healthie_Org_ID__c is a string


def fetch_client_counts() -> dict[str, dict]:
    rows = run_card(CLIENT_COUNTS_CARD)
    _check_columns(CLIENT_COUNTS_CARD, rows, CLIENT_COUNTS_COLUMNS)
    out = {}
    for r in rows:
        rec = {CLIENT_COUNTS_COLUMNS[k]: v for k, v in r.items()}
        org = _org(rec["org_id"])
        if org:
            out[org] = {"clients": _int(rec["clients"]) or 0,
                        "active_clients": _int(rec["active_clients"]) or 0}
    return out


def fetch_provider_counts() -> list[dict]:
    rows = run_card(PROVIDER_COUNTS_CARD)
    _check_columns(PROVIDER_COUNTS_CARD, rows, PROVIDER_COUNTS_COLUMNS)
    out = []
    for r in rows:
        rec = {PROVIDER_COUNTS_COLUMNS[k]: v for k, v in r.items()}
        org = _org(rec["org_id"])
        if org is None:
            continue  # providers with no org
        out.append({
            "org_id": org,
            "providers": _int(rec["providers"]) or 0,
            "active_providers": _int(rec["active_providers"]) or 0,
            "admins": _int(rec["admins"]) or 0,
            "last_provider_activity": _date(rec["last_provider_activity"]),
        })
    return out


def sync_org_usage() -> int | None:
    """Replace healthie_org_usage with a fresh pull. Separate transaction from the main
    sync. Without credentials, or on failure, the previous data is left untouched."""
    if not configured():
        log.info("Metabase not configured (no METABASE_API_KEY/METABASE_SESSION); "
                 "keeping existing org usage data")
        return None
    t0 = time.monotonic()
    # Both questions must succeed before anything is replaced.
    providers = {r["org_id"]: r for r in fetch_provider_counts()}
    clients = fetch_client_counts()
    empty_p = {"providers": 0, "active_providers": 0, "admins": 0, "last_provider_activity": None}
    empty_c = {"clients": 0, "active_clients": 0}
    rows = [{"org_id": org, **(providers.get(org) or empty_p), **(clients.get(org) or empty_c)}
            for org in sorted(providers.keys() | clients.keys())]
    with session_scope() as session:
        session.query(HealthieOrgUsage).delete()
        for i in range(0, len(rows), 5000):
            session.execute(insert(HealthieOrgUsage), rows[i:i + 5000])
    log.info("Healthie org usage: %d orgs (%d with providers, %d with clients) from Metabase "
             "cards %d + %d in %.0fs", len(rows), len(providers), len(clients),
             PROVIDER_COUNTS_CARD, CLIENT_COUNTS_CARD, time.monotonic() - t0)
    return len(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_db()
    sync_org_usage()
