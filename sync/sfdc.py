"""Salesforce read-only client.

Auth: OAuth 2.0 Client Credentials Flow against a Connected App whose
"Run As" user is a read-only integration user. No user interaction, no
refresh-token storage — the right pattern for a headless Railway service.

READ-ONLY BY DESIGN. This module intentionally has no write methods.
Product/pricing detail stays out of SFDC (storage + philosophy).

Field API names are env-configurable because labels != API names.
Verify once with:
  sf sobject describe -s Account | grep -iE "healthie_org|stripe_customer"
"""
import logging
import os

import requests

log = logging.getLogger("sync.sfdc")

SFDC_DOMAIN = os.environ.get("SFDC_DOMAIN", "https://healthie.my.salesforce.com")
CLIENT_ID = os.environ.get("SFDC_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SFDC_CLIENT_SECRET", "")
API_VERSION = os.environ.get("SFDC_API_VERSION", "v61.0")

ACCOUNT_ORG_ID_FIELD = os.environ.get("SFDC_ACCOUNT_ORG_ID_FIELD", "Healthie_Org_ID__c")
ACCOUNT_STRIPE_ID_FIELD = os.environ.get("SFDC_ACCOUNT_STRIPE_ID_FIELD", "Stripe_Customer_ID__c")


class SfdcError(RuntimeError):
    pass


_token_cache: dict = {}


def _token() -> str:
    if _token_cache.get("access_token"):
        return _token_cache["access_token"]
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SfdcError("SFDC_CLIENT_ID / SFDC_CLIENT_SECRET not set")
    resp = requests.post(
        f"{SFDC_DOMAIN}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SfdcError(f"Auth failed ({resp.status_code}): {resp.text[:300]}")
    _token_cache.update(resp.json())
    return _token_cache["access_token"]


def _query_all(soql: str):
    """Run a SOQL query and follow nextRecordsUrl pagination."""
    headers = {"Authorization": f"Bearer {_token()}"}
    url = f"{SFDC_DOMAIN}/services/data/{API_VERSION}/query"
    resp = requests.get(url, params={"q": soql}, headers=headers, timeout=60)
    while True:
        if resp.status_code == 401:
            _token_cache.clear()
            headers = {"Authorization": f"Bearer {_token()}"}
            resp = requests.get(url, params={"q": soql}, headers=headers, timeout=60)
        if resp.status_code >= 400:
            raise SfdcError(f"{resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        yield from data.get("records", [])
        next_url = data.get("nextRecordsUrl")
        if not next_url:
            break
        resp = requests.get(f"{SFDC_DOMAIN}{next_url}", headers=headers, timeout=60)


def fetch_accounts():
    """Accounts that can possibly join to ChartMogul (have a Stripe ID or Org ID)."""
    soql = (
        f"SELECT Id, Name, {ACCOUNT_ORG_ID_FIELD}, {ACCOUNT_STRIPE_ID_FIELD}, "
        f"Owner.Name, Type FROM Account "
        f"WHERE {ACCOUNT_ORG_ID_FIELD} != null OR {ACCOUNT_STRIPE_ID_FIELD} != null"
    )
    for rec in _query_all(soql):
        yield {
            "id": rec["Id"],
            "name": rec.get("Name"),
            "healthie_org_id": _as_str(rec.get(ACCOUNT_ORG_ID_FIELD)),
            "stripe_customer_id": rec.get(ACCOUNT_STRIPE_ID_FIELD),
            "owner_name": (rec.get("Owner") or {}).get("Name"),
            "account_type": rec.get("Type"),
        }


def fetch_contacts(account_ids: list[str]):
    """Contacts for the matched accounts, chunked to stay under SOQL limits."""
    CHUNK = 400
    for i in range(0, len(account_ids), CHUNK):
        chunk = account_ids[i:i + CHUNK]
        ids = ",".join(f"'{a}'" for a in chunk)
        soql = (
            "SELECT Id, AccountId, Name, Email, Title FROM Contact "
            f"WHERE AccountId IN ({ids})"
        )
        for rec in _query_all(soql):
            yield {
                "id": rec["Id"],
                "account_id": rec.get("AccountId"),
                "name": rec.get("Name"),
                "email": rec.get("Email"),
                "title": rec.get("Title"),
            }


def _as_str(v):
    if v is None:
        return None
    # Org IDs sometimes come back as numbers from SFDC number fields
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
