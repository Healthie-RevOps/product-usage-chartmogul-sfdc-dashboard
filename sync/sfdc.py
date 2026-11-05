"""Salesforce read-only client.

Auth, in order of preference:
  1. OAuth 2.0 Client Credentials against a Connected App whose "Run As" user is a
     read-only integration user (SFDC_CLIENT_ID / SFDC_CLIENT_SECRET). The target state.
  2. SOAP username/password login (SFDC_USERNAME / SFDC_PASSWORD / SFDC_SECURITY_TOKEN).
     Interim: currently the shared +netlify user that other RevOps Railway apps use.

READ-ONLY BY DESIGN. This module intentionally has no write methods.
Product/pricing detail stays out of SFDC (storage + philosophy).

Field API names are env-configurable because labels != API names.
Verify once with:
  sf sobject describe -s Account | grep -iE "healthie_org|stripe_customer"
"""
import logging
import os
import re
import time
from xml.sax.saxutils import escape

import requests

log = logging.getLogger("sync.sfdc")

SFDC_DOMAIN = os.environ.get("SFDC_DOMAIN", "https://healthie.my.salesforce.com").rstrip("/")
CLIENT_ID = os.environ.get("SFDC_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SFDC_CLIENT_SECRET", "")
USERNAME = os.environ.get("SFDC_USERNAME", "")
PASSWORD = os.environ.get("SFDC_PASSWORD", "")
SECURITY_TOKEN = os.environ.get("SFDC_SECURITY_TOKEN", "")
API_VERSION = os.environ.get("SFDC_API_VERSION", "v61.0")

ACCOUNT_ORG_ID_FIELD = os.environ.get("SFDC_ACCOUNT_ORG_ID_FIELD", "Healthie_Org_ID__c")
ACCOUNT_STRIPE_ID_FIELD = os.environ.get("SFDC_ACCOUNT_STRIPE_ID_FIELD", "Stripe_Customer_ID__c")


class SfdcError(RuntimeError):
    pass


_token_cache: dict = {}


def _login_client_credentials() -> dict:
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
    data = resp.json()
    return {"access_token": data["access_token"],
            "instance_url": data.get("instance_url") or SFDC_DOMAIN}


def _login_password() -> dict:
    """SOAP login() with username + password+security token. No Connected App needed.
    Interim auth until the read-only integration user exists (see README)."""
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"
              xmlns:urn="urn:partner.soap.sforce.com">
  <env:Body><urn:login>
    <urn:username>{escape(USERNAME)}</urn:username>
    <urn:password>{escape(PASSWORD + SECURITY_TOKEN)}</urn:password>
  </urn:login></env:Body>
</env:Envelope>"""
    resp = requests.post(
        f"{SFDC_DOMAIN}/services/Soap/u/{API_VERSION.lstrip('v')}",
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
        timeout=30,
    )
    if resp.status_code != 200:
        fault = re.search(r"<faultstring>(.*?)</faultstring>", resp.text)
        raise SfdcError(f"SOAP login failed ({resp.status_code}): "
                        f"{fault.group(1) if fault else resp.text[:300]}")
    session_id = re.search(r"<sessionId>(.*?)</sessionId>", resp.text)
    server_url = re.search(r"<serverUrl>(https://[^/<]+)", resp.text)
    if not session_id:
        raise SfdcError("SOAP login returned no sessionId")
    return {"access_token": session_id.group(1),
            "instance_url": server_url.group(1) if server_url else SFDC_DOMAIN}


def _session() -> dict:
    """Prefer the Connected App (client credentials); fall back to username/password."""
    if not _token_cache:
        if CLIENT_ID and CLIENT_SECRET:
            _token_cache.update(_login_client_credentials())
        elif USERNAME and PASSWORD:
            _token_cache.update(_login_password())
        else:
            raise SfdcError("Set SFDC_CLIENT_ID/SFDC_CLIENT_SECRET, or "
                            "SFDC_USERNAME/SFDC_PASSWORD/SFDC_SECURITY_TOKEN")
    return _token_cache


def _get(url: str, params: dict | None = None) -> dict:
    """GET with one re-auth on 401 and backoff on network errors / 5xx. Retries the
    *same* request, so pagination is safe."""
    reauthed = False
    for attempt in range(6):
        wait = min(2 ** attempt, 30)
        headers = {"Authorization": f"Bearer {_session()['access_token']}"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("SFDC %s, retrying in %ss", type(exc).__name__, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 401 and not reauthed:
            _token_cache.clear()
            reauthed = True
            continue
        if resp.status_code >= 500:
            log.warning("SFDC %s, retrying in %ss", resp.status_code, wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise SfdcError(f"{resp.status_code}: {resp.text[:400]}")
        return resp.json()
    raise SfdcError(f"Gave up on {url} after retries")


def _query_all(soql: str):
    """Run a SOQL query and follow nextRecordsUrl pagination."""
    base = _session()["instance_url"]
    data = _get(f"{base}/services/data/{API_VERSION}/query", {"q": soql})
    while True:
        yield from data.get("records", [])
        next_url = data.get("nextRecordsUrl")
        if not next_url:
            break
        data = _get(f"{_session()['instance_url']}{next_url}")


# Account usage fields (verified 2026-11-02 via describe). Mostly Vitally-fed.
ACCOUNT_USAGE_FIELDS = {
    "active_standard_users": "Active_Standard_Users_Account__c",
    "active_clients": "Healthie_Active_Clients_Account__c",
    "num_providers": "Number_Of_Providers__c",
    "adoption_score": "Product_Adoption_Score__c",
    "health_score": "Customer_Health_Score__c",
    "sfdc_arr": "ARR__c",
}

CHUNK = 300  # ids per IN (...) clause; keeps SOQL well under the 100k-char limit


def _visible_fields(sobject: str) -> set[str]:
    """Field API names the authenticated user can read on `sobject`."""
    base = _session()["instance_url"]
    desc = _get(f"{base}/services/data/{API_VERSION}/sobjects/{sobject}/describe")
    return {f["name"] for f in desc["fields"]}


_usage_cache: dict = {}


def _usage_fields() -> dict:
    """ACCOUNT_USAGE_FIELDS minus any the logged-in user can't see (checked once)."""
    if not _usage_cache:
        visible = _visible_fields("Account")
        _usage_cache.update({c: f for c, f in ACCOUNT_USAGE_FIELDS.items() if f in visible})
        hidden = sorted(set(ACCOUNT_USAGE_FIELDS.values()) - set(_usage_cache.values()))
        if hidden:
            log.warning("Account usage fields not visible to this SFDC user, skipped: %s",
                        hidden)
    return _usage_cache


def _chunks(ids: list[str]):
    for i in range(0, len(ids), CHUNK):
        yield ",".join(f"'{a}'" for a in ids[i:i + CHUNK])


def fetch_accounts_for(stripe_ids, org_ids):
    """Only the Accounts whose Stripe Customer ID or Healthie Org ID came back from
    ChartMogul. Deduplicated by Id (an account can match on both keys)."""
    seen = set()
    for field, keys in ((ACCOUNT_STRIPE_ID_FIELD, stripe_ids), (ACCOUNT_ORG_ID_FIELD, org_ids)):
        keys = sorted({str(k).replace("'", "") for k in keys if k})
        for ids in _chunks(keys):
            for row in fetch_accounts(f"{field} IN ({ids})"):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    yield row


def fetch_accounts(where: str | None = None):
    """Accounts matching `where`; default: any account with a Stripe ID or Org ID."""
    # Field-level security differs per user; drop optional usage fields this one can't see
    # rather than failing the whole sync.
    usage = _usage_fields()
    usage_cols = "".join(f", {f}" for f in usage.values())
    where = where or f"{ACCOUNT_ORG_ID_FIELD} != null OR {ACCOUNT_STRIPE_ID_FIELD} != null"
    soql = (
        f"SELECT Id, Name, {ACCOUNT_ORG_ID_FIELD}, {ACCOUNT_STRIPE_ID_FIELD}, "
        f"Owner.Name, Type{usage_cols} FROM Account WHERE {where}"
    )
    for rec in _query_all(soql):
        row = {
            "id": rec["Id"],
            "name": rec.get("Name"),
            "healthie_org_id": _as_str(rec.get(ACCOUNT_ORG_ID_FIELD)),
            "stripe_customer_id": rec.get(ACCOUNT_STRIPE_ID_FIELD),
            "owner_name": (rec.get("Owner") or {}).get("Name"),
            "account_type": rec.get("Type"),
        }
        for col, field in ACCOUNT_USAGE_FIELDS.items():
            row[col] = rec.get(field)
        yield row


def fetch_contacts(account_ids: list[str]):
    """Contacts (incl. Healthie user activity flags) for the given accounts.
    Call this with *matched* accounts only — the org has 500k+ contacts."""
    for ids in _chunks(account_ids):
        soql = (
            "SELECT Id, AccountId, Name, Email, Title, Phone, MobilePhone, "
            "Healthie_User_ID__c, Healthie_User_Active__c, Healthie_User_Type__c, "
            "Healthie_User_Last_Active__c, Healthie_Org_Admin__c, Healthie_Org_Owner__c "
            f"FROM Contact WHERE AccountId IN ({ids})"
        )
        for rec in _query_all(soql):
            yield {
                "id": rec["Id"],
                "account_id": rec.get("AccountId"),
                "name": rec.get("Name"),
                "email": rec.get("Email"),
                "title": rec.get("Title"),
                "phone": rec.get("Phone"),
                "mobile_phone": rec.get("MobilePhone"),
                "healthie_user_id": rec.get("Healthie_User_ID__c"),
                "user_active": bool(rec.get("Healthie_User_Active__c")),
                "user_type": rec.get("Healthie_User_Type__c"),
                "last_active": rec.get("Healthie_User_Last_Active__c"),
                "is_org_admin": bool(rec.get("Healthie_Org_Admin__c")),
                "is_org_owner": bool(rec.get("Healthie_Org_Owner__c")),
            }


def fetch_contracts(account_ids: list[str]):
    """Closed-won Opportunities with contract terms for the given accounts."""
    for ids in _chunks(account_ids):
        soql = (
            "SELECT Id, AccountId, Name, Type, CloseDate, Amount, Total_Contract_Value__c, "
            "Year_1_ACV__c, Contract_Start_Date__c, Contract_End_Date__c, "
            "Contract_Term_months__c FROM Opportunity "
            f"WHERE IsWon = true AND AccountId IN ({ids})"
        )
        for rec in _query_all(soql):
            yield {
                "id": rec["Id"],
                "account_id": rec.get("AccountId"),
                "name": rec.get("Name"),
                "opp_type": rec.get("Type"),
                "close_date": rec.get("CloseDate"),
                "amount": rec.get("Amount"),
                "total_contract_value": rec.get("Total_Contract_Value__c"),
                "year1_acv": rec.get("Year_1_ACV__c"),
                "contract_start": rec.get("Contract_Start_Date__c"),
                "contract_end": rec.get("Contract_End_Date__c"),
                "term_months": rec.get("Contract_Term_months__c"),
            }


def _as_str(v):
    if v is None:
        return None
    # Org IDs sometimes come back as numbers from SFDC number fields
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
