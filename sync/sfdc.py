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


def _chunks(ids: list[str]):
    for i in range(0, len(ids), CHUNK):
        yield ",".join(f"'{a}'" for a in ids[i:i + CHUNK])


def fetch_accounts():
    """Accounts that can possibly join to ChartMogul (have a Stripe ID or Org ID)."""
    usage_cols = ", ".join(ACCOUNT_USAGE_FIELDS.values())
    soql = (
        f"SELECT Id, Name, {ACCOUNT_ORG_ID_FIELD}, {ACCOUNT_STRIPE_ID_FIELD}, "
        f"Owner.Name, Type, {usage_cols} FROM Account "
        f"WHERE {ACCOUNT_ORG_ID_FIELD} != null OR {ACCOUNT_STRIPE_ID_FIELD} != null"
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
